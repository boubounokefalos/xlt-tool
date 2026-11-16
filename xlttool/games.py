"""The game library on the PC: PSP games (ISO, CSO, ZSO), homebrew, PSN and PS1
games (EBOOT.PBP) found in the folders the user picks, with what the games say
about themselves (PARAM.SFO, ICON0, PIC1), read offline. No Qt here.

The formats follow XLT's own readers (src/disc.c, src/probe.c): CSO v1 (deflate),
CSO v2 (deflate or LZ4 per block), ZSO (LZ4), and the PBP container. LZ4 is
decoded here in Python: only a few blocks are read per game.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field

SECTOR = 2048
SFO_MAX = 64 * 1024
PIC_MAX = 4 * 1024 * 1024
DISC_EXT = (".iso", ".cso", ".zso")

KIND_NAMES = {"iso": "PSP game", "cso": "PSP game (CSO)", "zso": "PSP game (ZSO)",
              "homebrew": "Homebrew", "psn": "PSN game", "ps1": "PS1 game"}
# PBP categories: MG homebrew, EG PSN, ME PS1. PG (game update) and UG (UMD stub) aren't games.
PBP_KINDS = {"MG": "homebrew", "EG": "psn", "ME": "ps1"}
REGIONS = {"U": "USA", "E": "Europe", "J": "Japan", "P": "Japan", "K": "Korea", "A": "Asia", "H": "Asia"}


# ------------------------------------------------------------------ places --

def _base(xdg: str, win: str, unix: str) -> str:
    """$XDG_* when set (on Windows too: the tests point it at a temporary folder),
    else %APPDATA% / %LOCALAPPDATA% on Windows, else the XDG default."""
    base = os.environ.get(xdg)
    if not base and sys.platform == "win32":
        base = os.environ.get(win)
    return base or os.path.expanduser(unix)


def data_dir() -> str:
    """Where XLT Tool keeps what it owns (save backups)."""
    return os.path.join(_base("XDG_DATA_HOME", "APPDATA", "~/.local/share"), "xlt-tool")


def cache_dir() -> str:
    """What can be made again (the library's index, icons)."""
    return os.path.join(_base("XDG_CACHE_HOME", "LOCALAPPDATA", "~/.cache"), "xlt-tool")


# --------------------------------------------------------------------- LZ4 --

def lz4_block(src: bytes, limit: int) -> bytes:
    """Decodes one LZ4 block (no frame), up to `limit` bytes out."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < limit:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        if i + 2 > n:
            raise ValueError("LZ4: cut off")
        off = src[i] | src[i + 1] << 8
        i += 2
        if not off or off > len(out):
            raise ValueError("LZ4: bad offset")
        ml = token & 15
        if ml == 15:
            while i < n:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if ml <= off:
            out += out[start:start + ml]
        else:  # overlapping copy: repeats the last `off` bytes
            for k in range(ml):
                out.append(out[start + k])
    return bytes(out[:limit])


# ------------------------------------------------------------------ sources --

class LocalFile:
    """A file on the PC, read in pieces."""

    def __init__(self, path: str):
        self.f = open(path, "rb")
        self.f.seek(0, os.SEEK_END)
        self.size = self.f.tell()

    def pread(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        return self.f.read(n)

    def close(self):
        self.f.close()


class RemoteFile:
    """A file on the PSP, read in pieces over USB sync (Link.read), 64 KB at a time
    and kept: finding PARAM.SFO and the icons in an image reads the same places
    more than once."""
    CHUNK = 64 * 1024

    def __init__(self, link, path: str, size: int):
        self.link, self.path, self.size = link, path, size
        self.chunks: dict[int, bytes] = {}

    def pread(self, off: int, n: int) -> bytes:
        out = bytearray()
        while n > 0 and off < self.size:
            i, inside = divmod(off, self.CHUNK)
            if i not in self.chunks:
                if len(self.chunks) > 64:
                    self.chunks.clear()
                self.chunks[i] = self.link.read(self.path, i * self.CHUNK, self.CHUNK)
            piece = self.chunks[i][inside:inside + n]
            if not piece:
                break
            out += piece
            off += len(piece)
            n -= len(piece)
        return bytes(out)

    def close(self):
        self.chunks.clear()


def _source(src):
    return LocalFile(src) if isinstance(src, str) else src


# -------------------------------------------------------------- disc images --

class Disc:
    """Reads an ISO, CSO (v1/v2) or ZSO image as if it were the plain ISO. src is
    a path on the PC, or a source (LocalFile, RemoteFile)."""

    def __init__(self, src):
        self.src = _source(src)
        h = self.src.pread(0, 24)
        self.cached = -1
        self.block = b""
        if h[:4] in (b"CISO", b"ZISO"):
            self.format = "cso" if h[0:1] == b"C" else "zso"
            self.total = struct.unpack_from("<Q", h, 8)[0]
            self.block_size = struct.unpack_from("<I", h, 16)[0]
            self.ver, self.align = h[20], h[21]
            if self.block_size < SECTOR or self.block_size > 1 << 20 or self.block_size % SECTOR:
                raise ValueError("not a CSO/ZSO image")
        else:
            self.format = "iso"
            self.total = self.src.size
            self.block_size = SECTOR

    def close(self):
        self.src.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _load(self, n: int):
        if n == self.cached:
            return
        i0, i1 = struct.unpack("<II", self.src.pread(24 + n * 4, 8))
        flag = i0 >> 31
        pos, end = (i0 & 0x7FFFFFFF) << self.align, (i1 & 0x7FFFFFFF) << self.align
        if end < pos:
            raise ValueError("bad block index")
        csize = end - pos
        stored = csize >= self.block_size if self.format == "cso" and self.ver >= 2 else flag
        lz4 = self.format == "zso" or (self.ver >= 2 and flag)
        if stored:
            data = self.src.pread(pos, self.block_size)
        else:
            raw = self.src.pread(pos, min(csize, self.block_size + 64))
            if lz4:
                data = lz4_block(raw, self.block_size)
            else:
                d = zlib.decompressobj(-15)
                data = d.decompress(raw, self.block_size)
        self.block = data.ljust(self.block_size, b"\0")
        self.cached = n

    def read(self, off: int, size: int) -> bytes:
        if self.format == "iso":
            data = self.src.pread(off, size)
            if len(data) != size:
                raise ValueError("past the end of the image")
            return data
        if off + size > self.total:
            raise ValueError("past the end of the image")
        out = bytearray()
        while size:
            n, inside = divmod(off, self.block_size)
            take = min(self.block_size - inside, size)
            self._load(n)
            out += self.block[inside:inside + take]
            off += take
            size -= take
        return bytes(out)

    def find(self, path: str) -> tuple[int, int] | None:
        """(offset, size) of "DIR/FILE" in the image (case-insensitive, ";1" ignored)."""
        pvd = self.read(16 * SECTOR, SECTOR)
        if pvd[0] != 1 or pvd[1:6] != b"CD001":
            return None
        lba, size = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
        is_dir = True
        for part in path.upper().split("/"):
            if not is_dir:
                return None
            hit = self._find_in_dir(lba, size, part)
            if not hit:
                return None
            lba, size, is_dir = hit
        return None if is_dir else (lba * SECTOR, size)

    def _find_in_dir(self, lba: int, size: int, name: str):
        for s in range(min(size, 64 * SECTOR) // SECTOR or 1):
            sec = self.read((lba + s) * SECTOR, SECTOR)
            p = 0
            while p + 33 < SECTOR:
                ln = sec[p]
                if ln == 0:
                    break
                if p + ln > SECTOR or ln < 34:
                    return None
                nl = sec[p + 32]
                raw = sec[p + 33:p + 33 + nl].split(b";")[0].rstrip(b".")
                if raw.decode("latin-1").upper() == name:
                    return (struct.unpack_from("<I", sec, p + 2)[0], struct.unpack_from("<I", sec, p + 10)[0],
                            bool(sec[p + 25] & 2))
                p += ln
        return None

    def file(self, path: str, limit: int) -> bytes | None:
        hit = self.find(path)
        if not hit or hit[1] > limit:
            return None
        return self.read(*hit)


# --------------------------------------------------------------- PARAM.SFO --

def parse_sfo(b: bytes) -> dict:
    """PARAM.SFO's keys and values (strings and integers)."""
    if len(b) < 20 or b[:4] != b"\0PSF":
        return {}
    keys, data, count = struct.unpack_from("<III", b, 8)
    if keys >= len(b) or data >= len(b) or 20 + count * 16 > len(b):
        return {}
    out = {}
    for i in range(count):
        ko, fmt, ln, _, do = struct.unpack_from("<HHIII", b, 20 + i * 16)
        ko += keys
        do += data
        if ko >= len(b) or do + ln > len(b):
            continue
        k = b[ko:b.find(b"\0", ko)].decode("ascii", "replace")
        v = b[do:do + ln]
        if fmt == 0x0404 and ln >= 4:
            out[k] = struct.unpack_from("<I", v)[0]
        else:
            out[k] = v.split(b"\0")[0].decode("utf-8", "replace")
    return out


def clean_title(t: str) -> str:
    """As XLT shows it: no ™ ® ©, newlines as spaces, single spaces."""
    for ch in "™®©":
        t = t.replace(ch, "")
    return " ".join(t.replace("\r", " ").replace("\n", " ").split())


def region_of(game_id: str) -> str:
    return REGIONS.get(game_id[2:3].upper(), "") if len(game_id) >= 4 else ""


# -------------------------------------------------------------------- games --

@dataclass
class Game:
    path: str               # the image, or the folder an EBOOT.PBP is in
    kind: str               # iso cso zso homebrew psn ps1
    title: str
    game_id: str = ""       # DISC_ID, e.g. ULUS10041
    region: str = ""
    version: str = ""       # DISC_VERSION or APP_VER
    firmware: str = ""      # PSP_SYSTEM_VER
    size: int = 0           # bytes: the image, or the whole folder
    mtime: float = 0
    icon: str = ""          # cached ICON0 (PNG path), "" if none
    pic: str = ""           # cached PIC1
    eboot: str = ""         # the PBP file (PBP kinds)
    extra: dict = field(default_factory=dict)

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, self.kind)

    @property
    def is_folder(self) -> bool:
        return os.path.isdir(self.path)

    def save_prefix(self) -> str:
        """Save folders on the PSP start with the game's ID (ULUS10041DATA00); PS1 saves too."""
        return self.game_id[:9].upper()


def _key(path: str) -> str:
    return hashlib.sha1(os.path.abspath(path).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _store_pic(data: bytes | None, key: str, name: str, icons: str) -> str:
    if not data or data[:8] != b"\x89PNG\r\n\x1a\n":
        return ""
    os.makedirs(icons, exist_ok=True)
    p = os.path.join(icons, f"{key}.{name}.png")
    with open(p, "wb") as f:
        f.write(data)
    return p


def _apply_sfo(g: Game, sfo: dict):
    g.title = clean_title(str(sfo.get("TITLE", ""))) or g.title
    g.game_id = str(sfo.get("DISC_ID", "")).strip()
    g.region = region_of(g.game_id)
    g.version = str(sfo.get("DISC_VERSION") or sfo.get("APP_VER") or "").strip()
    g.firmware = str(sfo.get("PSP_SYSTEM_VER", "")).strip()


def read_disc(path: str, icons: str, src=None, size: int = 0, mtime: float = 0) -> Game | None:
    """A PSP game image, or None if it isn't one. src: read it from there instead
    (a RemoteFile for a game on the PSP; then size and mtime are given)."""
    try:
        with Disc(src or path) as d:
            sfo = parse_sfo(d.file("PSP_GAME/PARAM.SFO", SFO_MAX) or b"")
            if not sfo.get("TITLE"):
                return None
            if src is None:
                st = os.stat(path)
                size, mtime = st.st_size, st.st_mtime
            g = Game(path=path, kind=d.format, title="", size=size, mtime=mtime)
            _apply_sfo(g, sfo)
            key = _key(path)
            g.icon = _store_pic(d.file("PSP_GAME/ICON0.PNG", PIC_MAX), key, "icon0", icons)
            g.pic = _store_pic(d.file("PSP_GAME/PIC1.PNG", PIC_MAX), key, "pic1", icons)
            return g
    except (OSError, ValueError, struct.error, zlib.error):
        return None


def read_pbp(path: str, icons: str, src=None, where: str = "", size: int = 0, mtime: float = 0) -> Game | None:
    """An EBOOT.PBP (homebrew, PSN, PS1), or None if it isn't a game (an update, a stub).
    src: read it from there instead (a RemoteFile; then where, size and mtime are given)."""
    f = None
    try:
        f = src or LocalFile(path)
        h = f.pread(0, 40)
        if len(h) < 40 or h[:4] != b"\0PBP":
            return None
        offs = struct.unpack_from("<8I", h, 8)  # SFO ICON0 ICON1 PIC0 PIC1 SND0 PSP PSAR

        def part(i: int, limit: int) -> bytes | None:
            start = offs[i]
            end = offs[i + 1] if i + 1 < 8 else start
            if end <= start or end - start > limit:
                return None
            return f.pread(start, end - start)

        sfo = parse_sfo(part(0, SFO_MAX) or b"")
        kind = PBP_KINDS.get(str(sfo.get("CATEGORY", "")))
        if not kind or not sfo.get("TITLE"):
            return None
        if src is None:
            loose = os.path.basename(path).upper() != "EBOOT.PBP"
            where = path if loose else os.path.dirname(path)
            size, mtime = folder_size(where)
        g = Game(path=where, kind=kind, title="", eboot=path, size=size, mtime=mtime)
        _apply_sfo(g, sfo)
        key = _key(where)
        g.icon = _store_pic(part(1, PIC_MAX), key, "icon0", icons)
        g.pic = _store_pic(part(4, PIC_MAX), key, "pic1", icons)
        return g
    except (OSError, ValueError, struct.error):
        return None
    finally:
        if f is not None and src is None:
            f.close()


def folder_size(path: str) -> tuple[int, float]:
    """(bytes, newest mtime) of a file, or of a folder and everything in it."""
    if not os.path.isdir(path):
        st = os.stat(path)
        return st.st_size, st.st_mtime
    total, newest = 0, 0.0
    for root, _dirs, files in os.walk(path):
        for n in files:
            try:
                st = os.stat(os.path.join(root, n))
            except OSError:
                continue
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return total, newest


# ----------------------------------------------------------------- scanning --

def find_candidates(folders: list[str]) -> list[str]:
    """Files that may be games: images, and PBPs. A folder with an EBOOT.PBP is a
    game of its own: nothing deeper in it is looked at (a PS1 game's extra files)."""
    out, seen = [], set()
    for top in folders:
        for root, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            names = {n.upper(): n for n in files}
            if "EBOOT.PBP" in names:
                p = os.path.join(root, names["EBOOT.PBP"])
                if p not in seen:
                    seen.add(p)
                    out.append(p)
                dirs[:] = []  # its own files (a PS1 game's extras): not games of their own...
                files = [n for n in files if n.lower().endswith(DISC_EXT)]  # ...but images beside it are
            for n in sorted(files):
                low = n.lower()
                if low.endswith(DISC_EXT) or low.endswith(".pbp"):
                    p = os.path.join(root, n)
                    if p not in seen:
                        seen.add(p)
                        out.append(p)
    return out


class Library:
    """The games in the user's folders. scan() reuses what an earlier scan read
    for files whose size and date haven't changed (index in cache_dir())."""

    def __init__(self, cache: str | None = None):
        self.cache = cache or cache_dir()
        self.icons = os.path.join(self.cache, "games")
        self.index_path = os.path.join(self.cache, "games.json")
        self.games: list[Game] = []
        self._index: dict[str, dict] = {}
        try:
            with open(self.index_path, encoding="utf-8") as f:
                self._index = json.load(f).get("files", {})
        except (OSError, ValueError):
            self._index = {}

    def scan(self, folders: list[str], progress=None, cancel=None) -> list[Game]:
        cands = find_candidates(folders)
        games, index = [], {}
        for i, p in enumerate(cands):
            if cancel and cancel():
                break
            if progress:
                progress(i, len(cands), p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            sig = [st.st_size, st.st_mtime]
            old = self._index.get(p)
            g = None
            if old and old.get("sig") == sig:
                if old.get("game"):
                    g = Game(**old["game"])
                    if g.icon and not os.path.exists(g.icon):
                        g = None  # the cache was cleared: read again
                else:
                    index[p] = old  # known not to be a game
                    continue
            if g is None:
                g = read_pbp(p, self.icons) if p.lower().endswith(".pbp") else read_disc(p, self.icons)
            elif g.kind in ("homebrew", "psn", "ps1") and os.path.isdir(g.path):
                g.size, g.mtime = folder_size(g.path)  # other files in the folder may have changed
            index[p] = {"sig": sig, "game": asdict(g) if g else None}
            if g:
                games.append(g)
        self._index = index
        self.games = games
        self._save()
        return games

    def _save(self):
        os.makedirs(self.cache, exist_ok=True)
        tmp = self.index_path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "saved": time.time(), "files": self._index}, f)
        os.replace(tmp, self.index_path)


# -------------------------------------------------------------------- saves --

def saves_dir() -> str:
    return os.path.join(data_dir(), "saves")


@dataclass
class SaveBackup:
    game_id: str     # the 9-character ID the save folders start with
    folder: str      # the save folder's name on the PSP (ULUS10041DATA00)
    when: str        # the backup's folder name, YYYY-MM-DD_HHMMSS
    path: str        # where it is on the PC
    title: str = ""  # from the save's own PARAM.SFO
    detail: str = ""


def save_backups(game_id: str = "") -> list[SaveBackup]:
    """Save backups kept on the PC: saves/<ID>/<save folder>/<YYYY-MM-DD_HHMMSS>/,
    newest first. game_id limits them to one game."""
    root = saves_dir()
    out = []
    ids = [game_id[:9].upper()] if game_id else (sorted(os.listdir(root)) if os.path.isdir(root) else [])
    for gid in ids:
        gdir = os.path.join(root, gid)
        if not os.path.isdir(gdir):
            continue
        for folder in sorted(os.listdir(gdir)):
            fdir = os.path.join(gdir, folder)
            if not os.path.isdir(fdir):
                continue
            for when in sorted(os.listdir(fdir), reverse=True):
                p = os.path.join(fdir, when)
                if not os.path.isdir(p):
                    continue
                b = SaveBackup(gid, folder, when, p)
                try:
                    with open(os.path.join(p, "PARAM.SFO"), "rb") as f:
                        sfo = parse_sfo(f.read(SFO_MAX))
                    b.title = clean_title(str(sfo.get("SAVEDATA_TITLE") or sfo.get("TITLE") or ""))
                    b.detail = clean_title(str(sfo.get("SAVEDATA_DETAIL", "")))
                except OSError:
                    pass
                out.append(b)
    out.sort(key=lambda b: b.when, reverse=True)
    return out


def new_backup_dir(game_id: str, save_folder: str, when: float | None = None) -> str:
    """The folder a new backup of one save goes into (made)."""
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when or time.time()))
    p = os.path.join(saves_dir(), game_id[:9].upper(), save_folder, stamp)
    os.makedirs(p, exist_ok=True)
    return p


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
