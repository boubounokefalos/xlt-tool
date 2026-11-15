"""Tests for xlttool/psplib.py with a pretend PSP: a folder on the PC that answers
like XLT's USB sync (list, read, get, df), so the scan, the PC/PSP matching and
copying games and saves to the PC run without the hardware."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xlttool import games, psplib, xltlink  # noqa: E402
from test_games import FIX, PNG, make_pbp, make_sfo  # noqa: E402


class FakeLink:
    """ms0:/ is `root` on the PC."""

    def __init__(self, root: str):
        self.root = root
        self.info = {"model": 10, "devices": 1, "buf_size": 256 * 1024, "put_chunk": 128 * 1024}
        self.reads = 0

    def _local(self, p: str) -> str:
        assert p.startswith("ms0:/"), p
        return os.path.join(self.root, *[x for x in p[5:].split("/") if x])

    def list(self, p):
        d = self._local(p)
        if not os.path.isdir(d):
            raise xltlink.XltsError(xltlink.E_NOENT, 0, p)
        out = []
        for n in sorted(os.listdir(d)):
            st = os.stat(os.path.join(d, n))
            out.append(xltlink.Entry(n, 0 if os.path.isdir(os.path.join(d, n)) else st.st_size,
                                     time.localtime(st.st_mtime)[:6], os.path.isdir(os.path.join(d, n))))
        return out

    def read(self, p, off, n):
        self.reads += 1
        with open(self._local(p), "rb") as f:
            f.seek(off)
            return f.read(n)

    def get(self, p, local=None, progress=None):
        if local is None:
            with open(self._local(p), "rb") as f:
                return f.read()
        shutil.copyfile(self._local(p), local)
        if progress:
            progress(os.path.getsize(local), os.path.getsize(local))
        return b""

    free, total = 10 ** 10, 2 * 10 ** 10

    def df(self, dev="ms0:", fresh=False):
        return self.free, self.total

    def stat(self, p):
        q = self._local(xltlink.psp_shown(p))
        if not os.path.exists(q):
            return None
        st = os.stat(q)
        return xltlink.Entry(os.path.basename(q), 0 if os.path.isdir(q) else st.st_size,
                             time.localtime(st.st_mtime)[:6], os.path.isdir(q))

    def mkdir(self, p):
        os.makedirs(self._local(xltlink.psp_shown(p)), exist_ok=True)

    def put(self, local, remote, replace=False, progress=None):
        q = self._local(xltlink.psp_shown(remote))
        if os.path.exists(q) and not replace:
            raise xltlink.XltsError(xltlink.E_EXIST, 0, remote)
        os.makedirs(os.path.dirname(q), exist_ok=True)
        if getattr(self, "fail_after", None) is not None:
            self.fail_after -= 1
            if self.fail_after < 0:
                raise RuntimeError("cable out")
        shutil.copyfile(local, q)
        if progress:
            progress(os.path.getsize(local), os.path.getsize(local))
        return {}

    def delete(self, p):
        q = self._local(xltlink.psp_shown(p))
        if os.path.isdir(q):
            os.rmdir(q)
        else:
            os.remove(q)

    def delete_tree(self, p):
        q = self._local(xltlink.psp_shown(p))
        if not os.path.exists(q):
            raise xltlink.XltsError(xltlink.E_NOENT, 0, p)
        n = sum(len(f) for _, _, f in os.walk(q)) if os.path.isdir(q) else 1
        shutil.rmtree(q) if os.path.isdir(q) else os.remove(q)
        return dict(files=n, folders=0, bytes=0)

    def rename(self, a, b):
        os.rename(self._local(xltlink.psp_shown(a)), self._local(xltlink.psp_shown(b)))


class Psp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.psp = os.path.join(self.tmp.name, "psp")
        self.pc = os.path.join(self.tmp.name, "pc")
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp.name, "data")

    def tearDown(self):
        del os.environ["XDG_DATA_HOME"]
        self.tmp.cleanup()

    def put(self, root, rel, data):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def test_scan_match_copy(self):
        hb = make_pbp(make_sfo({"TITLE": "Emu", "DISC_ID": "UCJS10041", "CATEGORY": "MG"}))
        ps1 = make_pbp(make_sfo({"TITLE": "Old Game", "DISC_ID": "SLUS00594", "CATEGORY": "ME"}), pic1=PNG)
        xlt = make_pbp(make_sfo({"TITLE": "XLT", "CATEGORY": "MG"}))
        self.put(self.psp, "PSP/GAME/Emu/EBOOT.PBP", hb)
        self.put(self.psp, "PSP/GAME/Emu/data/rom.bin", b"r" * 100)
        self.put(self.psp, "PSP/GAME/CAT_Old/SLUS00594/EBOOT.PBP", ps1)  # in a category folder
        self.put(self.psp, "PSP/GAME/XLT/EBOOT.PBP", xlt)
        self.put(self.psp, "PSP/GAME/XLT/xltsync.prx", b"x")
        self.put(self.psp, "PSP/SAVEDATA/SLUS00594/SCEVMC0.VMP", b"v" * 50)
        self.put(self.psp, "PSP/SAVEDATA/SLUS00594/PARAM.SFO", make_sfo({"SAVEDATA_TITLE": "Memory card"}))
        self.put(self.psp, "PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN", b"d")
        have_fix = os.path.isdir(FIX)
        if have_fix:
            for root, rel, name in ((self.psp, "ISO/sub/game.cso", "game.cso"), (self.psp, "ISO/VIDEO/skip.iso", "game.iso"),
                                    (self.pc, "games/game.iso", "game.iso")):
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                shutil.copyfile(os.path.join(FIX, name), os.path.join(root, rel))
        self.put(self.pc, "games/Emu/EBOOT.PBP", hb)

        link = FakeLink(self.psp)
        lib = psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache"))
        st = lib.scan(link)
        found = {g.path: g for g in st.games}
        want = {"ms0:/PSP/GAME/Emu", "ms0:/PSP/GAME/CAT_Old/SLUS00594"} | ({"ms0:/ISO/sub/game.cso"} if have_fix else set())
        self.assertEqual(set(found), want)
        self.assertEqual(found["ms0:/PSP/GAME/Emu"].size, len(hb) + 100)
        self.assertEqual((st.free, st.total, st.model), (10 ** 10, 2 * 10 ** 10, "PSP-E1000"))
        self.assertEqual({s.folder: s.title for s in st.saves}, {"SLUS00594": "Memory card", "ULUS10041DATA00": ""})

        # a second look reads nothing again (same sizes and dates)
        link.reads = 0
        psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache")).scan(link)
        self.assertEqual(link.reads, 1)  # only the one save's PARAM.SFO: no game is read again

        pc = games.Library(cache=os.path.join(self.tmp.name, "pccache")).scan([os.path.join(self.pc, "games")])
        entries = psplib.merge(pc, st.games)
        by = {e.game.title: e for e in entries}
        self.assertTrue(by["Emu"].pc and by["Emu"].psp)
        self.assertTrue(by["Old Game"].psp and not by["Old Game"].pc)
        if have_fix:
            self.assertTrue(by["Test Game Subtitle"].pc and by["Test Game Subtitle"].psp)  # ISO on PC, CSO on PSP
        self.assertEqual([s.folder for s in by["Old Game"].saves_on_psp(st)], ["SLUS00594"])

        # copy the PSP-only PS1 game, then its saves
        old = by["Old Game"].psp
        dest = os.path.join(self.pc, "games")
        where = psplib.copy_game_to_pc(link, old, dest)
        self.assertEqual(where, os.path.join(dest, "SLUS00594"))
        with open(os.path.join(where, "EBOOT.PBP"), "rb") as f:
            self.assertEqual(f.read(), ps1)
        again = psplib.copy_game_to_pc(link, old, dest)
        self.assertEqual(again, os.path.join(dest, "SLUS00594 2"))
        backups = psplib.backup_saves(link, by["Old Game"].saves_on_psp(st), "SLUS00594")
        self.assertEqual(len(backups), 1)
        b = games.save_backups("SLUS00594")
        self.assertEqual((len(b), b[0].title), (1, "Memory card"))

    def test_cancel_leaves_nothing(self):
        big = make_pbp(make_sfo({"TITLE": "Big", "CATEGORY": "MG"}))
        self.put(self.psp, "PSP/GAME/Big/EBOOT.PBP", big)
        self.put(self.psp, "PSP/GAME/Big/zzz.bin", b"z" * 10)
        link = FakeLink(self.psp)
        st = psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache")).scan(link)
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 1  # after the first file
        dest = os.path.join(self.pc, "games")
        os.makedirs(dest)
        with self.assertRaises(InterruptedError):
            psplib.copy_game_to_pc(link, st.games[0], dest, cancel=cancel)
        self.assertEqual(os.listdir(dest), [])


class PlayTime(Psp):
    def test_games_txt(self):
        hb = make_pbp(make_sfo({"TITLE": "Emu", "CATEGORY": "MG"}))
        self.put(self.psp, "PSP/GAME/Emu/EBOOT.PBP", hb)
        self.put(self.psp, "PSP/GAME/XLT/xltsync.prx", b"x")
        self.put(self.psp, "PSP/GAME/XLT/cache/games.txt",
                 b"XLT games 1\nhomebrew\tms0:/PSP/GAME/emu/EBOOT.PBP\t\t1794688113\t210\t1\tEmu\n"
                 b"iso\tms0:/ISO/x.iso\tULUS10021\t0\t0\t0\tGone\nbroken line\n")
        st = psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache")).scan(FakeLink(self.psp))
        self.assertEqual(st.xlt, "ms0:/PSP/GAME/XLT")
        g = st.games[0]
        self.assertEqual((g.extra["played"], g.extra["seconds"], g.extra["favorite"]), (1794688113, 210, True))
        self.assertTrue(psplib.played_text(g.extra).startswith("3 min · last played "))
        self.assertEqual(psplib.played_text({}), "")
        self.assertEqual(psplib.parse_games_txt(b"something else"), {})


class SendRemove(Psp):
    def test_send_and_remove(self):
        hb = make_pbp(make_sfo({"TITLE": "Emu", "CATEGORY": "MG"}))
        ps1 = make_pbp(make_sfo({"TITLE": "Old Game", "DISC_ID": "SLUS00594", "CATEGORY": "ME"}))
        self.put(self.pc, "games/Emu/EBOOT.PBP", hb)
        self.put(self.pc, "games/Emu/roms/a.bin", b"a" * 10)
        self.put(self.pc, "games/Café game.pbp", ps1)          # loose, and a name Shift-JIS lacks
        os.makedirs(os.path.join(self.psp, "PSP/GAME/Emu"))      # a folder of that name is taken
        if os.path.isdir(FIX):
            shutil.copyfile(os.path.join(FIX, "game.cso"), os.path.join(self.pc, "games", "game.cso"))
        pc = {g.title: g for g in games.Library(cache=os.path.join(self.tmp.name, "pccache")).scan(
            [os.path.join(self.pc, "games")])}
        link = FakeLink(self.psp)

        where = psplib.send_game_to_psp(link, pc["Emu"])
        self.assertEqual(where, "ms0:/PSP/GAME/Emu 2")
        self.assertTrue(os.path.isfile(os.path.join(self.psp, "PSP/GAME/Emu 2/roms/a.bin")))
        where = psplib.send_game_to_psp(link, pc["Old Game"])
        self.assertEqual(where, "ms0:/PSP/GAME/SLUS00594")
        if os.path.isdir(FIX):
            where = psplib.send_game_to_psp(link, pc["Test Game Subtitle"])
            self.assertEqual(where, "ms0:/ISO/game.cso")

        st = psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache")).scan(link)
        found = {g.path: g for g in st.games}
        self.assertIn("ms0:/PSP/GAME/Emu 2", found)
        self.assertEqual(found["ms0:/PSP/GAME/SLUS00594"].kind, "ps1")

        psplib.remove_game(link, found["ms0:/PSP/GAME/Emu 2"])
        self.assertFalse(os.path.exists(os.path.join(self.psp, "PSP/GAME/Emu 2")))
        if os.path.isdir(FIX):
            psplib.remove_game(link, found["ms0:/ISO/game.cso"])
            self.assertFalse(os.path.exists(os.path.join(self.psp, "ISO/game.cso")))

    def test_no_room_and_cut_off(self):
        hb = make_pbp(make_sfo({"TITLE": "Emu", "CATEGORY": "MG"}))
        self.put(self.pc, "games/Emu/EBOOT.PBP", hb)
        self.put(self.pc, "games/Emu/b.bin", b"b")
        g = games.Library(cache=os.path.join(self.tmp.name, "pccache")).scan([os.path.join(self.pc, "games")])[0]
        link = FakeLink(self.psp)
        link.free = 1000
        with self.assertRaises(xltlink.XltsError) as e:
            psplib.send_game_to_psp(link, g)
        self.assertEqual(e.exception.status, xltlink.E_FULL)
        link.free = 10 ** 10
        link.fail_after = 1  # the second file fails
        with self.assertRaises(RuntimeError):
            psplib.send_game_to_psp(link, g)
        self.assertFalse(os.path.exists(os.path.join(self.psp, "PSP/GAME/Emu")))  # nothing half-sent left

    def test_restore_saves(self):
        link = FakeLink(self.psp)
        # a backup on the PC, two of them for one folder: the newest goes back
        old = games.new_backup_dir("ULUS10041", "ULUS10041DATA00", when=1700000000)
        with open(os.path.join(old, "DATA.BIN"), "wb") as f:
            f.write(b"old backup")
        new = games.new_backup_dir("ULUS10041", "ULUS10041DATA00", when=1700100000)
        with open(os.path.join(new, "DATA.BIN"), "wb") as f:
            f.write(b"new backup")
        with open(os.path.join(new, "PARAM.SFO"), "wb") as f:
            f.write(make_sfo({"SAVEDATA_TITLE": "Chapter 9"}))
        b = psplib.newest_backups("ULUS10041")
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0].path, new)
        # not on the PSP yet: it is simply put there
        psplib.restore_saves(link, b)
        with open(os.path.join(self.psp, "PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN"), "rb") as f:
            self.assertEqual(f.read(), b"new backup")
        # on the PSP, changed there: backed up first, then replaced, no leftovers
        self.put(self.psp, "PSP/SAVEDATA/ULUS10041DATA00/DATA.BIN", b"played on")
        self.put(self.psp, "PSP/SAVEDATA/ULUS10041DATA00/EXTRA.BIN", b"x")
        before = len(games.save_backups("ULUS10041"))
        st = psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache")).scan(link)
        time.sleep(1.1)  # a new backup gets its own second
        psplib.restore_saves(link, b, st)
        self.assertEqual(len(games.save_backups("ULUS10041")), before + 1)
        names = sorted(os.listdir(os.path.join(self.psp, "PSP/SAVEDATA")))
        self.assertEqual(names, ["ULUS10041DATA00"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.psp, "PSP/SAVEDATA/ULUS10041DATA00"))),
                         ["DATA.BIN", "PARAM.SFO"])
        # a restore cut off earlier: the old save comes back, leftovers go
        os.rename(os.path.join(self.psp, "PSP/SAVEDATA/ULUS10041DATA00"),
                  os.path.join(self.psp, "PSP/SAVEDATA/ULUS10041DATA00.xltold"))
        self.put(self.psp, "PSP/SAVEDATA/ULUS10041DATA00.xltnew/DATA.BIN", b"half")
        psplib._settle_save(link, "ms0:/PSP/SAVEDATA/ULUS10041DATA00")
        self.assertEqual(sorted(os.listdir(os.path.join(self.psp, "PSP/SAVEDATA"))), ["ULUS10041DATA00"])
        # the scan never shows the temporary folders as saves
        self.put(self.psp, "PSP/SAVEDATA/X.xltnew/A", b"a")
        st = psplib.PspLibrary(cache=os.path.join(self.tmp.name, "cache")).scan(link)
        self.assertEqual([s.folder for s in st.saves], ["ULUS10041DATA00"])


if __name__ == "__main__":
    unittest.main()
