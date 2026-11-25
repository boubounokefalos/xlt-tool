"""The Files view's work on the PSP, over XLT's USB sync (xltlink.Link), without Qt:
which of XLT's folders can be browsed and changed, and sending, copying, renaming
and deleting there. The rules are the PSP's (plugin/xltsync/syncpath.c in XLT);
they are checked here too so the view can grey out what the PSP would refuse."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import games, psplib, xltlink

# (shown name, folder on the device, icon names): the folders sync may change things in
PLACES = (
    ("Video", "VIDEO", ("folder-videos-symbolic", "folder-videos", "folder")),
    ("Music", "MUSIC", ("folder-music-symbolic", "folder-music", "folder")),
    ("Pictures", "PICTURE", ("folder-pictures-symbolic", "folder-pictures", "folder")),
    ("Books", "BOOKS", ("accessories-dictionary-symbolic", "folder-documents-symbolic", "folder")),
    ("Downloads", "DOWNLOADS", ("folder-download-symbolic", "folder-download", "folder")),
    ("Games (ISO)", "ISO", ("media-optical-symbolic", "media-optical", "folder")),
    ("Games (PSP/GAME)", "PSP/GAME", ("applications-games-symbolic", "applications-games", "folder")),
    ("Saves", "PSP/SAVEDATA", ("media-floppy-symbolic", "media-floppy", "document-save-symbolic", "folder")),
)
SAVES = "PSP/SAVEDATA"
GAME_FOLDERS = ("ISO", "PSP/GAME", SAVES)   # a change there changes the Library too


def split(path: str) -> tuple[str, str]:
    """'ms0:/PSP/GAME/X' -> ('ms0:', 'PSP/GAME/X')."""
    p = xltlink.psp_path(path)
    return p[:4], p[5:]


def place_of(path: str) -> str:
    """The PLACES folder a path is in ('' if none), as PLACES spells it."""
    rest = split(path)[1].lower()
    for _, folder, _ in PLACES:
        f = folder.lower()
        if rest == f or rest.startswith(f + "/"):
            return folder
    return ""


def save_depth(path: str) -> int:
    """1: a save folder itself (or something beside one), 2: inside a save folder, 0: not in saves."""
    rest = split(path)[1]
    if not rest.lower().startswith(SAVES.lower() + "/"):
        return 0
    return 1 if "/" not in rest[len(SAVES) + 1:] else 2


def can_change(path: str) -> bool:
    """Whether the PSP lets the PC remove or rename this (a place itself: no)."""
    rest = split(path)[1]
    place = place_of(path)
    if not place or rest.lower() == place.lower():
        return False
    return place != SAVES or rest.count("/") <= 3  # a save folder, or a file directly in one


def can_add_files(folder: str) -> bool:
    """Files can be sent into folder (in Saves: only inside a save folder)."""
    return bool(place_of(folder)) and (place_of(folder) != SAVES or save_depth(folder) == 1)


def can_add_folders(folder: str) -> bool:
    """Folders can be made in folder (in Saves: only save folders, in Saves itself)."""
    if place_of(folder) == SAVES:
        return split(folder)[1].lower() == SAVES.lower()
    return bool(place_of(folder))


def join(folder: str, name: str) -> str:
    return xltlink.psp_path(folder).rstrip("/") + "/" + name


def entry_time(e: xltlink.Entry) -> float:
    return psplib._mtime(e.mtime)


# ------------------------------------------------------------------ the work --

@dataclass
class Plan:
    """Local files to send and the PSP paths they go to, folders to make first."""
    files: list            # (local, remote)
    folders: list          # remote folders, parents first
    total: int = 0
    skipped: list = None   # (local, why)
    ascii: list = None     # after upload: (name asked for, name made) where the PSP refused the name


def plan_upload(link, locals_: list[str], folder: str, existing: set[str] | None = None,
                replace: bool = False) -> Plan:
    """What sending these files and folders into folder does. existing: the names (lower
    case) already in folder; those are left out unless replace. Names are made holdable
    (xltlink.psp_name); a folder's files keep its layout."""
    existing = existing if existing is not None else {e.name.lower() for e in link.list(folder)}
    plan = Plan([], [], 0, [])
    in_saves = place_of(folder) == SAVES
    for local in locals_:
        local = os.path.abspath(local)
        name = xltlink.psp_name(os.path.basename(local.rstrip(os.sep)))
        target = join(folder, name)
        if name.lower() in existing and not replace:
            plan.skipped.append((local, "already there"))
            continue
        if os.path.isdir(local):
            if not can_add_folders(folder):
                plan.skipped.append((local, "only files can go into a save folder" if in_saves
                                     else "folders can't go here"))
                continue
            plan.folders.append(target)
            for root, dirs, files in os.walk(local):
                dirs.sort()
                rel = os.path.relpath(root, local)
                parts = [] if rel == "." else [xltlink.psp_name(p) for p in rel.split(os.sep)]
                here = "/".join([target] + parts)
                if parts:
                    if in_saves:  # a save folder holds files only
                        plan.skipped.append((root, "a save folder holds files only"))
                        dirs[:] = []
                        continue
                    plan.folders.append(here)
                for f in sorted(files):
                    p = os.path.join(root, f)
                    if os.path.islink(p) or not os.path.isfile(p):
                        continue
                    plan.files.append((p, f"{here}/{xltlink.psp_name(f)}"))
                    plan.total += os.path.getsize(p)
        elif os.path.isfile(local):
            if not can_add_files(folder):
                plan.skipped.append((local, "files go inside a save folder, not beside them" if in_saves
                                     else "files can't go here"))
                continue
            plan.files.append((local, target))
            plan.total += os.path.getsize(local)
    for local, _ in plan.files:
        if os.path.getsize(local) >= 1 << 32:
            raise xltlink.XltsError(xltlink.E_SIZE, 0, f"{os.path.basename(local)}: 4 GB or more "
                                                       "(the Memory Stick's FAT32 can't hold it)")
    return plan


def upload(link, plan: Plan, replace: bool = False, progress=None, cancel=None) -> list[str]:
    """Sends a plan's files. progress(fraction, name). Returns the PSP paths written. A name
    the PSP refused goes as plain ASCII (xltlink.ascii_name); plan.ascii lists those."""
    plan.ascii = []
    for f in plan.folders:
        _note_ascii(plan, f, link.mkdir(f))
    done, written, total = 0, [], plan.total or 1
    for local, remote in plan.files:
        if cancel and cancel():
            raise InterruptedError
        name = os.path.basename(local)

        def step(sent, _size, base=done):
            if cancel and cancel():
                raise InterruptedError
            if progress:
                progress((base + sent) / total, name)
        got = link.put(local, remote, replace=replace, progress=step)
        made = (got or {}).get("name") or xltlink.psp_shown(remote)
        _note_ascii(plan, remote, made)
        done += os.path.getsize(local)
        written.append(made)
    return written


def _note_ascii(plan: Plan, asked: str, made) -> None:
    """Notes a name the PSP made in ASCII instead (made: the path the link says it made)."""
    if isinstance(made, str):
        a, m = xltlink.psp_shown(asked).rsplit("/", 1)[-1], made.rsplit("/", 1)[-1]
        if a != m:
            plan.ascii.append((a, m))


def _tree(link, path: str) -> list[tuple[str, xltlink.Entry]]:
    """Everything in a folder on the PSP: (path relative to it, entry), folders before what is in them."""
    out, todo = [], [("", path)]
    while todo:
        rel, p = todo.pop(0)
        for e in link.list(p):
            r = f"{rel}/{e.name}" if rel else e.name
            out.append((r, e))
            if e.is_dir:
                todo.append((r, f"{p}/{e.name}"))
    return out


def local_name(name: str) -> str:
    """A PSP name as a file name on this PC (Windows refuses a few characters FAT allows)."""
    if os.name == "nt":
        name = "".join("_" if c in '<>:"/\\|?*' else c for c in name)
    return name.replace("/", "_") or "_"


def free_local(dest: str, name: str) -> str:
    """dest/name, or dest/'name 2' ... if that is taken on the PC."""
    base, ext = os.path.splitext(name)
    for n in range(1, 1000):
        p = os.path.join(dest, name if n == 1 else f"{base} {n}{ext}")
        if not os.path.exists(p):
            return p
    raise FileExistsError(os.path.join(dest, name))


def download(link, items: list[tuple[str, xltlink.Entry]], dest: str, progress=None, cancel=None) -> list[str]:
    """Copies PSP files and folders (path, entry) into dest on the PC, a taken name getting
    " 2". Keeps the PSP's dates. Returns the local paths made (top level)."""
    work = []   # (remote, local, entry)
    made = []
    for path, e in items:
        top = free_local(dest, local_name(e.name))
        made.append(top)
        work.append((path, top, e))
        if e.is_dir:
            for rel, sub in _tree(link, path):
                work.append((f"{path}/{rel}", os.path.join(top, *[local_name(x) for x in rel.split("/")]), sub))
    total = sum(e.size for _, _, e in work if not e.is_dir) or 1
    done = 0
    for remote, local, e in work:
        if cancel and cancel():
            raise InterruptedError
        if e.is_dir:
            os.makedirs(local, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(local), exist_ok=True)

        def step(got, _size, base=done, name=e.name):
            if cancel and cancel():
                raise InterruptedError
            if progress:
                progress((base + got) / total, name)
        link.get(remote, local, progress=step)
        t = entry_time(e)
        if t:
            os.utime(local, (t, t))
        done += e.size
    for remote, local, e in reversed(work):  # folders' dates last: filling them changed them
        t = entry_time(e)
        if e.is_dir and t and os.path.isdir(local):
            os.utime(local, (t, t))
    return made


def rename(link, path: str, new_name: str) -> str:
    """Renames in the same folder. Returns the new PSP path."""
    folder = path.rsplit("/", 1)[0]
    target = join(folder, xltlink.psp_name(new_name.strip()))
    if target.lower() != path.lower():
        try:
            taken = link.stat(target) is not None
        except xltlink.XltsError:  # a name the PSP refuses can't be taken: the rename says the rest
            taken = False
        if taken:
            raise xltlink.XltsError(xltlink.E_EXIST, 0, xltlink.psp_shown(target))
    made = link.rename(path, target)
    return made if isinstance(made, str) else xltlink.psp_shown(target)


def back_up_saves(link, paths: list[str], progress=None, cancel=None) -> list[str]:
    """Before saves are removed: each save folder that will lose something, copied to the
    PC's save backups (as the Library does). Returns the backup folders."""
    folders = []
    for p in paths:
        if save_depth(p):
            dev, rest = split(p)
            parts = rest.split("/")
            path = f"{dev}/{'/'.join(parts[:3])}"
            if path.lower() not in (x[1].lower() for x in folders):
                folders.append((parts[2], path))
    out = []
    for name, path in folders:
        size = sum(e.size for e in link.list(path) if not e.is_dir)
        out += psplib.backup_saves(link, [psplib.PspSave(name, path, size)], name[:9], progress, cancel)
    return out


def remove(link, items: list[tuple[str, xltlink.Entry]], progress=None, cancel=None) -> dict:
    """Deletes files and folders (with what is in them) on the PSP. Returns {files, folders, bytes}."""
    total = dict(files=0, folders=0, bytes=0)
    for i, (path, e) in enumerate(items):
        if cancel and cancel():
            raise InterruptedError
        if progress:
            progress(i / max(len(items), 1), e.name)
        if e.is_dir:
            got = link.delete_tree(path)
            for k in total:
                total[k] += got.get(k, 0)
        else:
            link.delete(path)
            total["files"] += 1
            total["bytes"] += e.size
    return total


def human_time(e: xltlink.Entry) -> str:
    t = entry_time(e)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


def size_text(e: xltlink.Entry) -> str:
    return "" if e.is_dir else games.human(e.size)
