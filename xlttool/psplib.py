"""What is on the PSP: its games and saves, read over XLT's USB sync, and the
two libraries (PC and PSP) put side by side. No Qt here: the scan runs as a task
on the sync worker (pspsync.py), which owns the link.

Games are found where XLT looks for them: ISO/ and two levels of folders under
it (but not ISO/VIDEO), and PSP/GAME/*/EBOOT.PBP, a category folder (no EBOOT of
its own) one level deeper. A game's PARAM.SFO and icons are read with READ
(pieces of the file), so a 1.7 GB image costs a few small reads, not a copy.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field

from . import games, xltlink


@dataclass
class PspSave:
    folder: str          # ULUS10041DATA00
    path: str            # ms0:/PSP/SAVEDATA/ULUS10041DATA00
    size: int = 0
    files: int = 0
    mtime: tuple = ()
    title: str = ""      # SAVEDATA_TITLE
    detail: str = ""


@dataclass
class PspState:
    model: str = ""
    games: list = field(default_factory=list)    # games.Game, path = a PSP path
    saves: list = field(default_factory=list)    # PspSave
    free: int = 0
    total: int = 0
    device: str = "ms0:"
    xlt: str = ""                                # XLT's folder on the PSP
    played: dict = field(default_factory=dict)   # path (lower case) -> (last played, seconds, favorite)


def _sig(e: xltlink.Entry) -> list:
    return [e.size, list(e.mtime)]


def _tree(link, path: str, depth: int = 8) -> tuple[int, tuple, list]:
    """(bytes, newest mtime, files) of a folder on the PSP and everything in it."""
    total, newest, files = 0, (), []
    for e in link.list(path):
        p = f"{path}/{e.name}"
        if e.is_dir:
            if depth > 0:
                t, n, f = _tree(link, p, depth - 1)
                total += t
                newest = max(newest, n)
                files += f
        else:
            total += e.size
            newest = max(newest, e.mtime)
            files.append((p, e.size))
    return total, newest, files


def _mtime(t: tuple) -> float:
    try:
        return time.mktime((*t[:6], 0, 0, -1)) if t else 0.0
    except (OverflowError, ValueError):
        return 0.0


class PspLibrary:
    """Reads the PSP's games and saves; what was read before (same path, size and
    date) comes from an index in the cache, so a second look costs only listings."""

    def __init__(self, cache: str | None = None):
        self.cache = cache or games.cache_dir()
        self.icons = os.path.join(self.cache, "psp-games")
        self.index_path = os.path.join(self.cache, "psp-games.json")
        try:
            with open(self.index_path, encoding="utf-8") as f:
                self._index = json.load(f).get("files", {})
        except (OSError, ValueError):
            self._index = {}

    def scan(self, link, progress=None, cancel=None) -> PspState:
        st = PspState(model=xltlink.MODELS.get(link.info["model"], "PSP"))
        devs = [d for bit, d in ((1, "ms0:"), (2, "ef0:")) if link.info["devices"] & bit]
        st.device = devs[0] if devs else "ms0:"
        self._xlt = ""
        cands = []  # (kind, the game's PSP path, the file to read, Entry of that file)
        for dev in devs:
            cands += self._discs(link, f"{dev}/ISO", 2)
            cands += self._eboots(link, f"{dev}/PSP/GAME")
        index = {}
        for i, (kind, where, fpath, e) in enumerate(cands):
            if cancel and cancel():
                raise InterruptedError
            if progress:
                progress(i, len(cands), where)
            g = None
            if kind == "pbp":
                size, newest, _ = _tree(link, where)
                sig = _sig(e) + [size]
            else:
                size, newest = e.size, e.mtime
                sig = _sig(e)
            old = self._index.get(where)
            if old and old.get("sig") == sig:
                if not old.get("game"):
                    index[where] = old
                    continue
                g = games.Game(**old["game"])
                if g.icon and not os.path.exists(g.icon):
                    g = None
            if g is None:
                src = games.RemoteFile(link, fpath, e.size)
                if kind == "pbp":
                    g = games.read_pbp(fpath, self.icons, src=src, where=where, size=size, mtime=_mtime(newest))
                else:
                    g = games.read_disc(fpath, self.icons, src=src, size=size, mtime=_mtime(newest))
            index[where] = {"sig": sig, "game": asdict(g) if g else None}
            if g:
                st.games.append(g)
        self._index = index
        self._save()
        st.xlt = self._xlt
        st.played = self._played(link, st.xlt)
        for g in st.games:
            key = (g.eboot if g.kind in ("homebrew", "psn", "ps1") and g.eboot else g.path).lower()
            last, secs, fav = st.played.get(key, (0, 0, False))
            g.extra = dict(g.extra, played=last, seconds=secs, favorite=fav)
        for dev in devs:
            st.saves += self._saves(link, f"{dev}/PSP/SAVEDATA")
        try:
            st.free, st.total = link.df(st.device)
        except xltlink.XltsError:
            pass
        return st

    def _discs(self, link, folder: str, depth: int) -> list:
        out = []
        try:
            entries = link.list(folder)
        except xltlink.XltsError:
            return out
        for e in entries:
            p = f"{folder}/{e.name}"
            if e.is_dir:
                if depth > 0 and not (folder.endswith("/ISO") and e.name.upper() == "VIDEO"):
                    out += self._discs(link, p, depth - 1)
            elif e.name.lower().endswith(games.DISC_EXT):
                out.append(("disc", p, p, e))
        return out

    def _eboots(self, link, folder: str, depth: int = 1) -> list:
        out = []
        try:
            entries = link.list(folder)
        except xltlink.XltsError:
            return out
        for e in entries:
            if not e.is_dir:
                continue
            p = f"{folder}/{e.name}"
            try:
                inside = {x.name.upper(): x for x in link.list(p)}
            except xltlink.XltsError:
                continue
            if "XLTSYNC.PRX" in inside or "XLTSYNC.LOG" in inside:
                self._xlt = p
                continue  # XLT itself
            eb = inside.get("EBOOT.PBP")
            if eb and not eb.is_dir:
                out.append(("pbp", p, f"{p}/{eb.name}", eb))
            elif depth > 0:
                out += self._eboots(link, p, depth - 1)
        return out

    @staticmethod
    def _played(link, xlt: str) -> dict:
        """XLT's play history: cache/games.txt in its folder (XLT's src/gamesfile.h)."""
        if not xlt:
            return {}
        try:
            text = link.get(f"{xlt}/cache/games.txt")
        except Exception:  # noqa: BLE001 - an older XLT, or none yet: no play time
            return {}
        return parse_games_txt(text)

    def _saves(self, link, folder: str) -> list:
        out = []
        try:
            entries = link.list(folder)
        except xltlink.XltsError:
            return out
        for e in entries:
            if not e.is_dir or e.name.lower().endswith((".xltnew", ".xltold")):
                continue  # (a save being put back by XLT Tool)
            s = PspSave(folder=e.name, path=f"{folder}/{e.name}")
            try:
                files = link.list(s.path)
            except xltlink.XltsError:
                continue
            s.files = sum(1 for x in files if not x.is_dir)
            s.size = sum(x.size for x in files if not x.is_dir)
            s.mtime = max((x.mtime for x in files), default=e.mtime)
            sfo = next((x for x in files if x.name.upper() == "PARAM.SFO"), None)
            if sfo and sfo.size <= games.SFO_MAX:
                try:
                    v = games.parse_sfo(link.read(f"{s.path}/{sfo.name}", 0, sfo.size))
                    s.title = games.clean_title(str(v.get("SAVEDATA_TITLE") or v.get("TITLE") or ""))
                    s.detail = games.clean_title(str(v.get("SAVEDATA_DETAIL", "")))
                except xltlink.XltsError:
                    pass
            out.append(s)
        return out

    def _save(self):
        os.makedirs(self.cache, exist_ok=True)
        tmp = self.index_path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "files": self._index}, f)
        os.replace(tmp, self.index_path)


def parse_games_txt(data: bytes) -> dict:
    """cache/games.txt -> {path in lower case: (last played as Unix time, seconds played, favorite)}."""
    lines = data.split(b"\n")
    if not lines or not lines[0].startswith(b"XLT games 1"):
        return {}
    out = {}
    for raw in lines[1:]:
        f = raw.split(b"\t")
        if len(f) < 7:
            continue
        try:
            path = f[1].decode("cp932", errors="replace")
            out[path.lower()] = (int(f[3]), int(f[4]), f[5] == b"1")
        except ValueError:
            continue
    return out


def played_text(extra: dict) -> str:
    """'12 h 40 min · last played yesterday', from XLT's play history; '' if never."""
    secs, last = extra.get("seconds", 0), extra.get("played", 0)
    if not last:
        return ""
    if secs >= 3600:
        total = f"{secs // 3600} h {secs % 3600 // 60} min" if secs < 100 * 3600 else f"{secs // 3600} h"
    elif secs >= 60:
        total = f"{secs // 60} min"
    else:
        total = "under a minute" if secs else ""
    days = (time.time() - last) // 86400
    if days < 1 and time.localtime(last).tm_mday == time.localtime().tm_mday:
        when = "today"
    elif days < 2:
        when = "yesterday"
    elif days < 14:
        when = f"{int(days)} days ago"
    else:
        when = time.strftime("%d %b %Y", time.localtime(last))
    return f"{total} · last played {when}" if total else f"Last played {when}"



# ----------------------------------------------------- PC and PSP together --

def match_key(g: games.Game) -> tuple:
    """The same game on the PC and on the PSP: by its ID, or, for homebrew (whose
    IDs are often a template's), by its title. An ISO and its CSO are the same game."""
    family = "disc" if g.kind in ("iso", "cso", "zso") else g.kind
    if g.kind == "homebrew" or not g.game_id:
        return (family, g.title.lower())
    return (family, g.game_id.upper())


@dataclass
class Entry:
    pc: games.Game | None = None
    psp: games.Game | None = None

    @property
    def game(self) -> games.Game:
        return self.pc or self.psp

    def saves_on_psp(self, state: PspState | None) -> list:
        prefix = self.game.save_prefix()
        if not state or not prefix:
            return []
        return [s for s in state.saves if s.folder.upper().startswith(prefix)]


def merge(pc: list, psp: list) -> list:
    """One entry per game, on the PC, on the PSP, or both."""
    out, by_key = [], {}
    for g in pc:
        e = Entry(pc=g)
        out.append(e)
        by_key.setdefault(match_key(g), e)
    for g in psp:
        e = by_key.get(match_key(g))
        if e is not None and e.psp is None:
            e.psp = g
        else:
            out.append(Entry(psp=g))
    return out


# ---------------------------------------------------------------- copying --

def copy_game_to_pc(link, g: games.Game, dest_folder: str, progress=None, cancel=None) -> str:
    """Copies a game from the PSP into dest_folder (an image as a file, a PBP game as
    its folder). Returns where it went. A name already there gets " 2", " 3"..."""
    name = g.path.rstrip("/").rsplit("/", 1)[-1]
    base, ext = (os.path.splitext(name) if g.kind in ("iso", "cso", "zso") else (name, ""))
    target = os.path.join(dest_folder, name)
    n = 2
    while os.path.exists(target):
        target = os.path.join(dest_folder, f"{base} {n}{ext}")
        n += 1
    if g.kind in ("iso", "cso", "zso"):
        files = [(g.path, target)]
    else:
        _, _, found = _tree(link, g.path)
        files = [(p, os.path.join(target, *p[len(g.path) + 1:].split("/"))) for p, _ in found]
    total = g.size or 1
    done = 0
    try:
        for remote, local in files:
            os.makedirs(os.path.dirname(local), exist_ok=True)

            def step(got, size, base=done):
                if cancel and cancel():
                    raise InterruptedError
                if progress:
                    progress((base + got) / total)
            link.get(remote, local, progress=step)
            done += os.path.getsize(local)
    except BaseException:
        # half a game is no game: what was copied goes again
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target):
            os.remove(target)
        raise
    return target


def backup_saves(link, saves: list, game_id: str, progress=None, cancel=None) -> list:
    """Copies save folders from the PSP into new dated backups on the PC. Returns their paths."""
    out, stamp = [], time.time()
    total = sum(s.size for s in saves) or 1
    done = 0
    for s in saves:
        dest = games.new_backup_dir(game_id, s.folder, stamp)
        try:
            for e in link.list(s.path):
                if e.is_dir:
                    continue
                if cancel and cancel():
                    raise InterruptedError

                def step(got, size, base=done):
                    if progress:
                        progress((base + got) / total)
                local = os.path.join(dest, e.name)
                link.get(f"{s.path}/{e.name}", local, progress=step)
                t = _mtime(e.mtime)
                if t:  # the PSP's date, so a save put back keeps it
                    os.utime(local, (t, t))
                done += e.size
        except BaseException:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
            raise
        out.append(dest)
    return out


# ------------------------------------------------------- sending, removing --

SAVE_NEW, SAVE_OLD = ".xltnew", ".xltold"   # a save being put back: never half of one under its name
ROOM = 32 * 1024 * 1024                     # left free on the stick after a send


def _free_name(link, folder: str, name: str, is_dir: bool) -> str:
    """A name in folder (on the PSP) that isn't taken: "Name", "Name 2", ..."""
    base, ext = (name, "") if is_dir else os.path.splitext(name)
    for n in range(1, 1000):
        cand = xltlink.psp_name(name if n == 1 else f"{base} {n}{ext}")
        if link.stat(f"{folder}/{cand}") is None:
            return cand
    raise xltlink.XltsError(xltlink.E_EXIST, 0, f"{folder}/{name}")


def _local_files(g: games.Game) -> list[tuple[str, str]]:
    """(file on the PC, its path inside the game's folder on the PSP) for a PBP game."""
    if not os.path.isdir(g.path):  # a loose PS1/homebrew .pbp: it becomes an EBOOT.PBP in its own folder
        return [(g.path, "EBOOT.PBP")]
    out = []
    for root, dirs, files in os.walk(g.path):
        dirs.sort()
        for n in sorted(files):
            p = os.path.join(root, n)
            out.append((p, os.path.relpath(p, g.path).replace(os.sep, "/")))
    return out


def send_game_to_psp(link, g: games.Game, device: str = "ms0:", progress=None, cancel=None) -> str:
    """Sends a game from the PC to where XLT finds it: an image to ISO/, a PBP game
    as its folder to PSP/GAME/. Checks the room first. Returns its PSP path. A game
    folder cut off halfway is deleted again (an image never shows half: .xltpart)."""
    free, _ = link.df(device)
    if g.size + ROOM > free:
        raise xltlink.XltsError(xltlink.E_FULL, 0, f"{g.title} needs {games.human(g.size)}, "
                                f"the PSP has {games.human(free)} free")
    if g.kind in ("iso", "cso", "zso"):
        folder = f"{device}/ISO"
        link.mkdir(folder)
        target = f"{folder}/{_free_name(link, folder, os.path.basename(g.path), False)}"
        files = [(g.path, target)]
    else:
        folder = f"{device}/PSP/GAME"
        name = os.path.basename(g.path) if os.path.isdir(g.path) else (g.game_id or os.path.splitext(
            os.path.basename(g.path))[0])
        target = f"{folder}/{_free_name(link, folder, name, True)}"
        files = [(p, f"{target}/{rel}") for p, rel in _local_files(g)]
    total = sum(os.path.getsize(p) for p, _ in files) or 1
    done = 0
    try:
        for local, remote in files:
            def step(sent, size, base=done):
                if cancel and cancel():
                    raise InterruptedError
                if progress:
                    progress((base + sent) / total)
            link.put(local, remote, progress=step)
            done += os.path.getsize(local)
    except BaseException:
        if len(files) > 1 or g.kind not in ("iso", "cso", "zso"):
            try:
                link.delete_tree(target)
            except Exception:  # noqa: BLE001 - the link may be gone; the user sees the folder then
                pass
        raise
    return target


def remove_game(link, g: games.Game) -> dict:
    """Removes a game from the PSP: an image, or a PBP game's whole folder."""
    if g.kind in ("iso", "cso", "zso"):
        st = link.stat(g.path)
        link.delete(g.path)
        return dict(files=1, folders=0, bytes=st.size if st else 0)
    return link.delete_tree(g.path)


def remove_saves(link, saves: list) -> int:
    n = 0
    for s in saves:
        link.delete_tree(s.path)
        n += 1
    return n


def newest_backups(game_id: str) -> list:
    """The newest backup of each of a game's save folders kept on the PC."""
    seen, out = set(), []
    for b in games.save_backups(game_id):  # newest first
        if b.folder not in seen:
            seen.add(b.folder)
            out.append(b)
    return out


def _settle_save(link, path: str):
    """A save left halfway by an earlier restore: the old one goes back if the new one
    never got its name, and leftovers go."""
    old, new = link.stat(path + SAVE_OLD), link.stat(path + SAVE_NEW)
    here = link.stat(path)
    if old and not here:
        link.rename(path + SAVE_OLD, path)
    elif old:
        link.delete_tree(path + SAVE_OLD)
    if new:
        link.delete_tree(path + SAVE_NEW)


def restore_saves(link, backups: list, state: "PspState | None" = None, device: str = "ms0:",
                  progress=None, cancel=None) -> list:
    """Puts save backups from the PC back on the PSP. A save already there is backed up
    to the PC first, then replaced without ever leaving half of one: the backup goes in
    as FOLDER.xltnew, the old one is renamed FOLDER.xltold, the new one into place, and
    the old one deleted. Returns the PSP paths written."""
    total = sum(games.folder_size(b.path)[0] for b in backups) or 1
    done, out = 0, []
    on_psp = {s.folder.upper(): s for s in (state.saves if state else [])}
    for b in backups:
        if cancel and cancel():
            raise InterruptedError
        path = f"{device}/PSP/SAVEDATA/{xltlink.psp_name(b.folder)}"
        _settle_save(link, path)
        here = link.stat(path)
        if here:
            s = on_psp.get(b.folder.upper()) or PspSave(folder=b.folder, path=path)
            backup_saves(link, [s], b.game_id)
        files = sorted(n for n in os.listdir(b.path) if os.path.isfile(os.path.join(b.path, n)))
        try:
            for n in files:
                local = os.path.join(b.path, n)

                def step(sent, size, base=done):
                    if cancel and cancel():
                        raise InterruptedError
                    if progress:
                        progress((base + sent) / total)
                link.put(local, f"{path}{SAVE_NEW}/{n}", replace=True, progress=step)
                done += os.path.getsize(local)
        except BaseException:
            try:
                link.delete_tree(path + SAVE_NEW)
            except Exception:  # noqa: BLE001
                pass
            raise
        if here:
            link.rename(path, path + SAVE_OLD)
        link.rename(path + SAVE_NEW, path)
        if here:
            link.delete_tree(path + SAVE_OLD)
        out.append(path)
    return out
