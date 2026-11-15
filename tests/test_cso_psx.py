"""Tests for xlttool/cso.py (ISO <-> CSO) and xlttool/psx.py (PS1 BIN/CUE -> EBOOT),
with small images made here."""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xlttool import cso, games, psx  # noqa: E402
from test_games import FIX, PNG, make_pbp, make_sfo  # noqa: E402


def raw_sector(lba: int, data: bytes) -> bytes:
    """A mode 2 form 1 raw sector: sync, header, subheader, 2048 bytes, EDC/ECC space."""
    head = b"\x00" + b"\xff" * 10 + b"\x00" + bytes([0, 2, lba % 75, 2]) + b"\0" * 8
    return (head + data.ljust(2048, b"\0")).ljust(2352, b"\0")


def dir_record(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    rec = bytearray(33 + len(name) + (len(name) + 1) % 2)
    rec[0] = len(rec)
    struct.pack_into("<I", rec, 2, lba)
    struct.pack_into("<I", rec, 10, size)
    rec[25] = 2 if is_dir else 0
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def make_ps1_bin(path: str, sectors: int = 400):
    cnf = b"BOOT = cdrom:\\SLUS_005.94;1\r\nTCB = 4\r\n"
    pvd = bytearray(2048)
    pvd[0], pvd[1:6] = 1, b"CD001"
    root = dir_record(b"\0", 18, 2048, True) + dir_record(b"\1", 18, 2048, True) + \
        dir_record(b"SYSTEM.CNF;1", 20, len(cnf), False)
    pvd[156:156 + 34] = dir_record(b"\0", 18, 2048, True)
    with open(path, "wb") as f:
        for lba in range(sectors):
            data = {16: bytes(pvd), 18: root, 20: cnf}.get(lba, bytes([lba % 251]) * 64)
            f.write(raw_sector(lba, data))


class Cso(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as t:
            iso = os.path.join(t, "a.iso")
            with open(iso, "wb") as f:  # compressible, incompressible and empty blocks
                f.write(b"A" * 2048 * 700 + os.urandom(2048 * 300) + b"\0" * 2048 * 25)
            cso.iso_to_cso(iso, os.path.join(t, "a.cso"), threads=3)
            with open(os.path.join(t, "a.cso"), "rb") as f:
                h = f.read(24)
            self.assertEqual((h[:4], struct.unpack_from("<Q", h, 8)[0], h[20]), (b"CISO", 1025 * 2048, 1))
            self.assertLess(os.path.getsize(os.path.join(t, "a.cso")), os.path.getsize(iso))
            cso.cso_to_iso(os.path.join(t, "a.cso"), os.path.join(t, "b.iso"))
            with open(iso, "rb") as a, open(os.path.join(t, "b.iso"), "rb") as b:
                self.assertEqual(a.read(), b.read())
            self.assertFalse([n for n in os.listdir(t) if n.endswith(".part")])

    @unittest.skipUnless(os.path.isdir(FIX), "XLT's fixtures not made")
    def test_game_reads(self):
        with tempfile.TemporaryDirectory() as t:
            cso.iso_to_cso(os.path.join(FIX, "game.iso"), os.path.join(t, "g.cso"))
            g = games.read_disc(os.path.join(t, "g.cso"), t)
            self.assertEqual((g.kind, g.title), ("cso", "Test Game Subtitle"))

    def test_not_an_iso(self):
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "x.iso"), "wb") as f:
                f.write(b"x" * 1000)
            with self.assertRaises(ValueError):
                cso.iso_to_cso(os.path.join(t, "x.iso"), os.path.join(t, "x.cso"))


class Ps1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name
        os.environ["XDG_DATA_HOME"] = os.path.join(self.t, "data")

    def tearDown(self):
        del os.environ["XDG_DATA_HOME"]
        self.tmp.cleanup()

    def donor(self) -> str:
        """A PS1 EBOOT as popstation writes one (single disc), with pretend DATA.PSP and STARTDAT."""
        sfo = make_sfo({"TITLE": "Donor", "DISC_ID": "SCES00344", "CATEGORY": "ME"})
        body = make_pbp(sfo)
        offs = list(struct.unpack_from("<8I", body, 8))
        data_psp = b"~PSP" + b"\x11" * 500
        psar = (offs[6] + len(data_psp) + 0xFFFF) & ~0xFFFF
        offs[7] = psar
        out = bytearray(body[:offs[6]]) + data_psp
        out += b"\0" * (psar - len(out))
        psiso = bytearray(b"PSISOIMG0000" + struct.pack("<I", 0x100040)).ljust(0x100040, b"\0")
        out += psiso + b"STARTDAT" + b"\x22" * 100
        struct.pack_into("<8I", out, 8, *offs)
        p = os.path.join(self.t, "donor.pbp")
        with open(p, "wb") as f:
            f.write(out)
        return p

    def test_parts_and_eboot(self):
        self.assertFalse(psx.have_parts())
        with self.assertRaises(ValueError):
            psx.take_parts(os.path.join(FIX, "hb.pbp") if os.path.isdir(FIX) else __file__)
        psx.take_parts(self.donor())
        self.assertTrue(psx.have_parts())
        with open(os.path.join(psx.parts_dir(), "STARTDAT.BIN"), "rb") as f:
            self.assertEqual(f.read(), b"STARTDAT" + b"\x22" * 100)

        img = os.path.join(self.t, "Old Game (USA) (Track 1).bin")
        make_ps1_bin(img)
        cue = os.path.join(self.t, "Old Game (USA).cue")
        with open(cue, "w") as f:
            f.write(f'FILE "{os.path.basename(img)}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                    f'FILE "Old Game (USA) (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n')
        with open(os.path.join(self.t, "Old Game (USA) (Track 2).bin"), "wb") as f:
            f.write(b"\x01\x02" * (2352 * 225 // 2))  # 75 sectors of pregap... and music
        self.assertEqual(psx.data_track(cue), img)
        self.assertEqual(psx.audio_tracks(cue), 1)
        self.assertEqual(psx.disc_id(img), "SLUS00594")
        self.assertEqual(psx.clean_title("Old Game (USA) [!]"), "Old Game")

        out = os.path.join(self.t, "EBOOT.PBP")
        psx.make_eboot(img, out, "Old Game", "SLUS-00594", icon0=PNG)
        with open(img, "rb") as f:
            src = f.read()
        back = psx.read_disc(out)
        self.assertEqual(back[:len(src)], src)
        self.assertEqual(len(back) % psx.CHUNK, 0)
        g = games.read_pbp(out, self.t)
        self.assertEqual((g.kind, g.title, g.game_id), ("ps1", "Old Game", "SLUS00594"))
        with open(out, "rb") as f:
            data = f.read()
        psar = struct.unpack_from("<I", data, 36)[0]
        self.assertEqual(psar % 0x10000, 0)
        self.assertEqual(data[psar:psar + 12], b"PSISOIMG0000")
        self.assertEqual(data[psar + 0x400:psar + 0x40B], b"_SLUS_00594")
        self.assertTrue(data.endswith(b"STARTDAT" + b"\x22" * 100))
        length = struct.unpack_from("<I", data, psar + 12)[0]
        self.assertEqual(data[psar + length:psar + length + 8], b"STARTDAT")
        self.assertEqual(struct.unpack_from("<I", data, psar + 0x1220)[0], length + 0x2D31)
        # its DATA.PSP is the donor's
        offs = struct.unpack_from("<8I", data, 8)
        self.assertEqual(data[offs[6]:offs[6] + 4], b"~PSP")

    def test_cd_music(self):
        """A one-file cue with a music track: the data track alone in the EBOOT, the TOC
        from the cue, the music (from a stand-in encoder) after the disc in the table."""
        psx.take_parts(self.donor())
        img = os.path.join(self.t, "Music Game.bin")
        make_ps1_bin(img, 300)
        with open(img, "ab") as f:
            f.write(b"\x05\x06" * (2352 * 100 // 2))  # sectors 300..399: music
        cue = os.path.join(self.t, "Music Game.cue")
        with open(cue, "w") as f:
            f.write('FILE "Music Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                    '  TRACK 02 AUDIO\n    INDEX 00 00:04:00\n    INDEX 01 00:04:25\n')
        lay = psx.disc_layout(cue)
        self.assertEqual([(t["first"], t["start"], t["stop"]) for t in lay], [(0, 0, 299), (300, 325, 399)])
        self.assertEqual(psx.data_size(cue), 300 * 2352)
        toc = psx.cue_toc(lay)
        self.assertEqual(toc[30:40], bytes([0x41, 0, 1, 0, 2, 1, 0, 0, 2, 0]))
        self.assertEqual(toc[40:50], bytes([0x01, 0, 2, 0, 4, 0x25, 0, 0, 6, 0x25]))  # 00:04:25, +2 s
        self.assertEqual(toc[27:30], bytes([0, 5, 0x26]))  # lead-out: sector 401 = 00:05:26
        enc = os.path.join(self.t, "atracdenc")
        with open(enc, "w") as f:  # writes a 0x60-byte header and the input's size, as text
            f.write("#!/bin/sh\nin=\"$3\"; out=\"$5\"\nhead -c 96 /dev/zero > \"$out\"\n"
                    "printf 'AT3:%s' $(wc -c < \"$in\") >> \"$out\"\n")
        os.chmod(enc, 0o755)
        os.environ["XLTTOOL_ATRACDENC"] = enc
        try:
            music = psx.encode_music(cue)
        finally:
            del os.environ["XLTTOOL_ATRACDENC"]
        self.assertEqual(music, [b"AT3:%d" % (44 + 75 * 2352)])  # from INDEX 01 to the end
        out = os.path.join(self.t, "EBOOT.PBP")
        psx.make_eboot(img, out, "Music Game", "SLUS00594", size=psx.data_size(cue), toc=toc, music=music)
        with open(out, "rb") as f:
            data = f.read()
        psar = struct.unpack_from("<I", data, 36)[0]
        self.assertEqual(data[psar + 0x800:psar + 0x800 + len(toc)], toc)
        off, ln = struct.unpack_from("<II", data, psar + 0xC00)
        self.assertEqual(data[psar + psx.DISC_START + off:psar + psx.DISC_START + off + ln], music[0])
        with open(img, "rb") as f:
            src = f.read(300 * 2352)
        self.assertEqual(psx.read_disc(out)[:len(src)], src)
        self.assertNotIn(b"\x05\x06\x05\x06", psx.read_disc(out))  # no music sectors in the disc part

    def test_multi_disc(self):
        """Two discs in one EBOOT: PSTITLEIMG from the donor, each disc as it was."""
        from xlttool import convert
        with self.assertRaises(RuntimeError):  # no donor yet
            psx.make_eboot([psx.Disc("a", "SLUS00594"), psx.Disc("b", "SLUS00595")], "x", "T")
        donor = self.donor()
        psx.take_parts(donor)
        self.assertFalse(psx.have_title_part())  # a single-disc donor has no PSTITLEIMG
        with open(donor, "r+b") as f:  # make it a multi-disc donor: PSTITLEIMG header first
            data = bytearray(f.read())
            psar = struct.unpack_from("<I", data, 36)[0]
            title = bytearray(b"PSTITLEIMG000000".ljust(0x400, b"\0"))
            title[0x214] = 0x29  # a byte of the donor's own, to see it is kept
            struct.pack_into("<I", title, 0x10, 0x400 + 0x100040)
            struct.pack_into("<I", title, 0x200, 0x400)
            data[psar:psar] = title
            f.seek(0)
            f.write(data)
        psx.take_parts(donor)
        self.assertTrue(psx.have_title_part())
        a, b = os.path.join(self.t, "Game (Disc 1).bin"), os.path.join(self.t, "Game (Disc 2).bin")
        make_ps1_bin(a, 200)
        make_ps1_bin(b, 300)
        self.assertEqual(convert.group_discs([b, os.path.join(self.t, "x.txt"), a]),
                         [[a, b], [os.path.join(self.t, "x.txt")]])
        out = os.path.join(self.t, "EBOOT.PBP")
        psx.make_eboot([psx.Disc(a, "SLUS00594"), psx.Disc(b, "SLUS00595")], out, "Game")
        for n, src in enumerate((a, b)):
            with open(src, "rb") as f:
                img = f.read()
            self.assertEqual(psx.read_disc(out, n)[:len(img)], img)
        with open(out, "rb") as f:
            data = f.read()
        psar = struct.unpack_from("<I", data, 36)[0]
        t = data[psar:psar + 0x400]
        self.assertEqual((t[:16], t[0x214], t[0x264:0x26F], t[0x30C:0x310]),
                         (b"PSTITLEIMG000000", 0x29, b"_SLUS_00594", b"Game"))
        d1, d2, d3 = struct.unpack_from("<3I", t, 0x200)
        self.assertEqual((d1 % 0x8000, d2 % 0x8000, d3), (0, 0, 0))
        self.assertEqual(data[psar + d2 + 0x400:psar + d2 + 0x40B], b"_SLUS_00595")
        sd = struct.unpack_from("<I", t, 0x10)[0]
        self.assertEqual(data[psar + sd:psar + sd + 8], b"STARTDAT")
        self.assertEqual(struct.unpack_from("<I", t, 0x284)[0], sd + 0x2D31)

    def test_needs_parts(self):
        img = os.path.join(self.t, "a.bin")
        make_ps1_bin(img, 20)
        with self.assertRaises(RuntimeError):
            psx.make_eboot(img, os.path.join(self.t, "E.PBP"), "A", "SLUS00594")


if __name__ == "__main__":
    unittest.main()
