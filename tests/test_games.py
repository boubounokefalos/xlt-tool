"""Tests for xlttool/games.py: the game readers against XLT's own test images
(ISO, CSO v1, CSO v1 with alignment, CSO v2, ZSO, a homebrew PBP and a game
update, made by ~/xlt/tests/make_fixtures.py), and PBPs and saves made here.

    python3 -I -m unittest discover -s tests      (from the repository)
XLT's fixtures: $XLT_FIXTURES, default ~/xlt/build/fixtures (made by XLT's tests/run.sh).
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from xlttool import games  # noqa: E402

FIX = os.environ.get("XLT_FIXTURES") or os.path.expanduser("~/xlt/build/fixtures")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24


def make_sfo(values: dict) -> bytes:
    keys, data, entries = b"", b"", []
    for k, v in values.items():
        ko = len(keys)
        keys += k.encode() + b"\0"
        if isinstance(v, int):
            raw, fmt = struct.pack("<I", v), 0x0404
        else:
            raw, fmt = v.encode() + b"\0", 0x0204
        raw_pad = raw.ljust((len(raw) + 3) // 4 * 4, b"\0")
        entries.append((ko, fmt, len(raw), len(raw_pad), len(data)))
        data += raw_pad
    keys = keys.ljust((len(keys) + 3) // 4 * 4, b"\0")
    kstart = 20 + 16 * len(entries)
    head = b"\0PSF" + struct.pack("<IIII", 0x101, kstart, kstart + len(keys), len(entries))
    return head + b"".join(struct.pack("<HHIII", *e) for e in entries) + keys + data


def make_pbp(sfo: bytes, icon: bytes = PNG, pic1: bytes = b"") -> bytes:
    parts = [sfo, icon, b"", b"", pic1, b"", b"ELF", b""]
    offs, at = [], 40
    for p in parts:
        offs.append(at)
        at += len(p)
    return b"\0PBP" + struct.pack("<I8I", 0x10000, *offs) + b"".join(parts)


@unittest.skipUnless(os.path.isdir(FIX), f"XLT's fixtures not made ({FIX})")
class XltFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.icons = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_images(self):
        for name, fmt in (("game.iso", "iso"), ("game.cso", "cso"), ("game_a2.cso", "cso"),
                          ("game_v2.cso", "cso"), ("game.zso", "zso")):
            with self.subTest(name):
                g = games.read_disc(os.path.join(FIX, name), self.icons)
                self.assertIsNotNone(g)
                self.assertEqual(g.kind, fmt)
                self.assertEqual(g.title, "Test Game Subtitle")
                with open(g.icon, "rb") as f:
                    self.assertEqual(f.read(8), PNG[:8])

    def test_same_bytes_from_every_format(self):
        with games.Disc(os.path.join(FIX, "game.iso")) as d:
            want = d.read(0, d.total)
        for name in ("game.cso", "game_a2.cso", "game_v2.cso", "game.zso"):
            with self.subTest(name), games.Disc(os.path.join(FIX, name)) as d:
                self.assertEqual(d.read(0, len(want)), want)

    def test_pbps(self):
        g = games.read_pbp(os.path.join(FIX, "hb.pbp"), self.icons)
        self.assertIsNotNone(g)
        self.assertEqual((g.kind, g.title), ("homebrew", "My Homebrew"))
        self.assertIsNone(games.read_pbp(os.path.join(FIX, "update.pbp"), self.icons))


class Made(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel: str, data: bytes) -> str:
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_sfo(self):
        sfo = games.parse_sfo(make_sfo({"TITLE": "A™ Game\nTwo", "DISC_ID": "ULES00151", "PARENTAL_LEVEL": 5}))
        self.assertEqual(sfo["PARENTAL_LEVEL"], 5)
        self.assertEqual(games.clean_title(sfo["TITLE"]), "A Game Two")
        self.assertEqual(games.region_of(sfo["DISC_ID"]), "Europe")
        self.assertEqual(games.region_of("SLPS01234"), "Japan")

    def test_lz4(self):
        # literals "abc", then a match of 6 at offset 3 (overlapping), then the last literals
        block = bytes([0x32]) + b"abc" + bytes([3, 0]) + bytes([0x50]) + b"xyzzy"
        self.assertEqual(games.lz4_block(block, 100), b"abcabcabcxyzzy")
        long_lit = bytes([0xF0, 5]) + b"q" * 20
        self.assertEqual(games.lz4_block(long_lit, 100), b"q" * 20)

    def test_scan(self):
        ps1 = make_pbp(make_sfo({"TITLE": "Old Game", "DISC_ID": "SLUS00594", "CATEGORY": "ME"}), pic1=PNG)
        hb = make_pbp(make_sfo({"TITLE": "Emu", "CATEGORY": "MG"}))
        upd = make_pbp(make_sfo({"TITLE": "Update", "DISC_ID": "ULUS10041", "CATEGORY": "PG"}))
        self.write("PS1/SLUS00594/EBOOT.PBP", ps1)
        self.write("PS1/SLUS00594/KEYS.BIN", b"k" * 16)
        self.write("PS1/SLUS00594/inner/EBOOT.PBP", hb)  # inside a game: not a game of its own
        self.write("Homebrew/Emu/EBOOT.PBP", hb)
        self.write("Updates/ULUS10041/EBOOT.PBP", upd)
        self.write("loose.pbp", ps1)
        self.write(".hidden/Emu/EBOOT.PBP", hb)
        if os.path.isdir(FIX):
            with open(os.path.join(FIX, "game.cso"), "rb") as f:
                self.write("ISO/sub/game.cso", f.read())
        lib = games.Library(cache=os.path.join(self.root, "cache"))
        found = {os.path.relpath(g.path, self.root): g for g in lib.scan([self.root])}
        want = {"PS1/SLUS00594", "Homebrew/Emu", "loose.pbp"} | ({"ISO/sub/game.cso"} if os.path.isdir(FIX) else set())
        self.assertEqual(set(found), want)
        g = found["PS1/SLUS00594"]
        self.assertEqual((g.kind, g.game_id, g.region, g.save_prefix()), ("ps1", "SLUS00594", "USA", "SLUS00594"))
        self.assertEqual(g.size, len(ps1) + 16 + len(hb))  # the whole folder
        self.assertTrue(g.pic)
        # again, from the index: the same games
        again = games.Library(cache=os.path.join(self.root, "cache"))
        self.assertEqual({os.path.relpath(x.path, self.root) for x in again.scan([self.root])}, want)

    @unittest.skipUnless(os.path.isdir(FIX), "XLT's fixtures not made")
    def test_image_beside_an_eboot(self):
        """A folder with an EBOOT.PBP is a game, not searched deeper; an image beside it is still a game."""
        hb = make_pbp(make_sfo({"TITLE": "Emu", "CATEGORY": "MG"}))
        self.write("EBOOT.PBP", hb)
        self.write("deeper/other.pbp", hb)
        with open(os.path.join(FIX, "game.cso"), "rb") as f:
            self.write("game.cso", f.read())
        found = {g.title for g in games.Library(cache=os.path.join(self.root, "cache")).scan([self.root])}
        self.assertEqual(found, {"Emu", "Test Game Subtitle"})

    def test_saves(self):
        os.environ["XDG_DATA_HOME"] = os.path.join(self.root, "data")
        try:
            p = games.new_backup_dir("ULUS10041", "ULUS10041DATA00", when=1700000000)
            with open(os.path.join(p, "PARAM.SFO"), "wb") as f:
                f.write(make_sfo({"SAVEDATA_TITLE": "Chapter 3", "SAVEDATA_DETAIL": "Level 12"}))
            games.new_backup_dir("ULUS10041", "ULUS10041DATA00", when=1700100000)
            games.new_backup_dir("ULES00151", "ULES00151SYS", when=1700000000)
            b = games.save_backups("ULUS10041")
            self.assertEqual(len(b), 2)
            self.assertEqual(b[1].title, "Chapter 3")
            self.assertGreater(b[0].when, b[1].when)
            self.assertEqual(len(games.save_backups()), 3)
        finally:
            del os.environ["XDG_DATA_HOME"]


if __name__ == "__main__":
    unittest.main()
