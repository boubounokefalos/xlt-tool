"""Tests for xlttool/pspfiles.py (the Files view's work) with the pretend PSP of test_psplib."""
import os
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xlttool import pspfiles, xltlink  # noqa: E402
from test_psplib import FakeLink  # noqa: E402


class Rules(unittest.TestCase):
    def test_places(self):
        self.assertEqual(pspfiles.place_of("ms0:/VIDEO/a.mp4"), "VIDEO")
        self.assertEqual(pspfiles.place_of("ms0:/psp/game/X"), "PSP/GAME")
        self.assertEqual(pspfiles.place_of("ms0:/PSP/SAVEDATA"), "PSP/SAVEDATA")
        self.assertEqual(pspfiles.place_of("ms0:/SEPLUGINS/x.prx"), "")
        self.assertEqual(pspfiles.place_of("ms0:/VIDEOS"), "")

    def test_change(self):
        self.assertFalse(pspfiles.can_change("ms0:/VIDEO"))          # a place itself
        self.assertTrue(pspfiles.can_change("ms0:/VIDEO/a/b.mp4"))
        self.assertFalse(pspfiles.can_change("ms0:/SEPLUGINS/x"))
        self.assertTrue(pspfiles.can_change("ms0:/PSP/SAVEDATA/ULUS10041DATA00"))
        self.assertTrue(pspfiles.can_change("ms0:/PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN"))
        self.assertFalse(pspfiles.can_change("ms0:/PSP/SAVEDATA/A/B/C"))

    def test_saves(self):
        root, save = "ms0:/PSP/SAVEDATA", "ms0:/PSP/SAVEDATA/X"
        self.assertFalse(pspfiles.can_add_files(root))
        self.assertTrue(pspfiles.can_add_folders(root))
        self.assertTrue(pspfiles.can_add_files(save))
        self.assertFalse(pspfiles.can_add_folders(save))
        self.assertTrue(pspfiles.can_add_files("ef0:/MUSIC"))


class Work(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.psp = os.path.join(self.tmp.name, "psp")
        self.pc = os.path.join(self.tmp.name, "pc")
        os.makedirs(os.path.join(self.psp, "MUSIC"))
        os.makedirs(os.path.join(self.psp, "PSP", "SAVEDATA"))
        os.makedirs(self.pc)
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp.name, "data")
        self.link = FakeLink(self.psp)

    def tearDown(self):
        del os.environ["XDG_DATA_HOME"]
        self.tmp.cleanup()

    def write(self, root, rel, data=b"x"):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_upload_folder(self):
        self.write(self.pc, "Album/01 Song.mp3", b"a" * 100)
        self.write(self.pc, "Album/Disc 2/02 Café.mp3", b"b" * 50)
        song = self.write(self.pc, "single.mp3", b"c" * 10)
        plan = pspfiles.plan_upload(self.link, [os.path.join(self.pc, "Album"), song], "ms0:/MUSIC")
        self.assertEqual(plan.total, 160)
        seen = []
        out = pspfiles.upload(self.link, plan, progress=lambda f, n: seen.append(f))
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(seen[-1], 1.0)
        self.assertTrue(os.path.exists(os.path.join(self.psp, "MUSIC/Album/Disc 2/02 Cafe.mp3")))
        # the same again: already there, left out unless replaced
        again = pspfiles.plan_upload(self.link, [song], "ms0:/MUSIC")
        self.assertEqual(again.files, [])
        self.assertEqual(again.skipped[0][1], "already there")
        self.write(self.pc, "single.mp3", b"new")
        rep = pspfiles.plan_upload(self.link, [song], "ms0:/MUSIC", replace=True)
        pspfiles.upload(self.link, rep, replace=True)
        with open(os.path.join(self.psp, "MUSIC/single.mp3"), "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_upload_saves(self):
        f = self.write(self.pc, "loose.bin")
        self.write(self.pc, "ULUS10041DATA00/DATA.BIN")
        self.write(self.pc, "ULUS10041DATA00/sub/deep.bin")
        plan = pspfiles.plan_upload(self.link, [f, os.path.join(self.pc, "ULUS10041DATA00")], "ms0:/PSP/SAVEDATA")
        whys = [w for _, w in plan.skipped]
        self.assertIn("files go inside a save folder, not beside them", whys)
        self.assertIn("a save folder holds files only", whys)
        self.assertEqual([r for _, r in plan.files], ["ms0:/PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN"])

    def test_cancel(self):
        self.write(self.pc, "a.mp3")
        plan = pspfiles.plan_upload(self.link, [os.path.join(self.pc, "a.mp3")], "ms0:/MUSIC")
        with self.assertRaises(InterruptedError):
            pspfiles.upload(self.link, plan, cancel=lambda: True)

    def test_download_rename_remove(self):
        self.write(self.psp, "MUSIC/Album/01.mp3", b"1" * 7)
        self.write(self.psp, "MUSIC/Album/In/02.mp3", b"2" * 5)
        self.write(self.psp, "MUSIC/x.mp3", b"3")
        items = [(f"ms0:/MUSIC/{e.name}", e) for e in self.link.list("ms0:/MUSIC")]
        made = pspfiles.download(self.link, items, self.pc)
        self.assertEqual(sorted(os.path.basename(m) for m in made), ["Album", "x.mp3"])
        self.assertEqual(os.path.getsize(os.path.join(self.pc, "Album/In/02.mp3")), 5)
        made2 = pspfiles.download(self.link, items[:1], self.pc)  # taken name: "Album 2"
        self.assertEqual(os.path.basename(made2[0]), "Album 2")

        new = pspfiles.rename(self.link, "ms0:/MUSIC/x.mp3", "y.mp3")
        self.assertEqual(new, "ms0:/MUSIC/y.mp3")
        with self.assertRaises(xltlink.XltsError):
            pspfiles.rename(self.link, "ms0:/MUSIC/y.mp3", "Album")

        items = [(f"ms0:/MUSIC/{e.name}", e) for e in self.link.list("ms0:/MUSIC")]
        done = pspfiles.remove(self.link, items)
        self.assertEqual(done["files"], 3)
        self.assertEqual(os.listdir(os.path.join(self.psp, "MUSIC")), [])

    def test_back_up_saves(self):
        self.write(self.psp, "PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN", b"save")
        self.write(self.psp, "PSP/SAVEDATA/ULUS10041DATA00/ICON0.PNG", b"png")
        kept = pspfiles.back_up_saves(self.link, ["ms0:/PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN",
                                                  "ms0:/PSP/SAVEDATA/ULUS10041DATA00/ICON0.PNG",
                                                  "ms0:/MUSIC/not-a-save.mp3"])
        self.assertEqual(len(kept), 1)  # one save folder, backed up once, whole
        self.assertEqual(sorted(os.listdir(kept[0])), ["DATA.BIN", "ICON0.PNG"])
        self.assertIn(os.path.join("ULUS10041", "ULUS10041DATA00"), kept[0])


class RefusingPsp(xltlink.Link):
    """xltlink.Link's own code over a pretend PSP that, as an E1004 once did, refuses
    (80010016) to make anything whose name has a two-byte Shift-JIS character."""

    def __init__(self):
        self.renamed, self.made, self.crc, self.size = {}, [], 0, 0
        self.ep_out = self

    def write(self, block, _timeout):  # the file's data (ep_out)
        self.crc = zlib.crc32(block, self.crc)
        self.size += len(block)

    def _call(self, op, payload=b"", arg=0, what="", ok=(xltlink.OK,), timeout=0):
        if op == xltlink.PUT_END:
            return 0, 0, b""
        if op == xltlink.RENAME:
            path = payload.split(b"\0")[1]
        elif op == xltlink.PUT:
            path = payload[xltlink.PUT_S.size:].rstrip(b"\0")
            self.crc = self.size = 0
        else:
            path = payload.rstrip(b"\0")
        last = path.rsplit(b"/", 1)[-1]
        if any(b >= 0x80 for b in last):
            raise xltlink.XltsError(xltlink.E_IO, xltlink.EINVAL, what)
        self.made.append(path.decode())
        return 0, 1 << 16, b""

    def _reply(self, op, timeout=0):
        return 0, 0, xltlink.PUTDONE_S.pack(self.crc, 0, self.size)


class AsciiFallback(unittest.TestCase):
    def test_names(self):
        self.assertEqual(xltlink.ascii_name("Χρήστος Δάντης - Κομμάτια.mp3"), "Christos Dantis - Kommatia.mp3")
        self.assertEqual(xltlink.ascii_name("ΧΡΗΣΤΟΣ"), "CHRISTOS")
        self.assertEqual(xltlink.ascii_name("Μπουζούκι Ευτυχία"), "Bouzouki Evtychia")
        self.assertEqual(xltlink.ascii_name("Привет.mp3"), "Privet.mp3")
        self.assertEqual(xltlink.ascii_name("a:b"), "a_b")
        self.assertEqual(xltlink.psp_name("Χρήστος"), "Χρηστοσ")  # Shift-JIS first: no ς there

    def test_send_folder(self):
        with tempfile.TemporaryDirectory() as t:
            album = os.path.join(t, "Δάντης")
            os.makedirs(album)
            for n in ("01 Ένα.mp3", "02 Two.mp3"):
                with open(os.path.join(album, n), "wb") as f:
                    f.write(b"song")
            psp = RefusingPsp()
            plan = pspfiles.plan_upload(psp, [album], "ms0:/MUSIC", existing=set())
            written = pspfiles.upload(psp, plan)
        self.assertEqual(psp.made, ["ms0:/MUSIC/Dantis", "ms0:/MUSIC/Dantis/01 Ena.mp3",
                                    "ms0:/MUSIC/Dantis/02 Two.mp3"])
        self.assertEqual(written, psp.made[1:])
        self.assertEqual(plan.ascii, [("Δαντησ", "Dantis"), ("01 Ενα.mp3", "01 Ena.mp3")])
        self.assertEqual(psp.real("ms0:/MUSIC/Δάντης/x"), "ms0:/MUSIC/Dantis/x")

    def test_rename_and_other_errors(self):
        psp = RefusingPsp()
        self.assertEqual(pspfiles.rename(psp, "ms0:/MUSIC/a.mp3", "Ψ.mp3"), "ms0:/MUSIC/Ps.mp3")
        self.assertEqual(psp.mkdir("ms0:/MUSIC/Plain"), "ms0:/MUSIC/Plain")

        def full(*a, **k):
            raise xltlink.XltsError(xltlink.E_FULL, 0, "x")
        psp._call = full
        with self.assertRaises(xltlink.XltsError):  # only a refused name is tried again
            psp.mkdir("ms0:/MUSIC/Ω")


if __name__ == "__main__":
    unittest.main()
