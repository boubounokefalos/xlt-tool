# Copied from XLT (tools/xltlink.py): the PC side of XLT's USB sync. Keep the two the same.
"""XLT USB sync, PC side: send files to a PSP running XLT, without USB mode.

On the PSP: XLT > Settings > XLT > USB sync (On), then connect the cable.

    xltlink.py info                       the PSP, its link and storage devices
    xltlink.py ls [PATH]                  a folder (default ms0:/)
    xltlink.py put FILE... DEST           send files or folders (DEST a folder: ends in / or exists)
    xltlink.py get PATH [LOCAL]           copy a file from the PSP
    xltlink.py log [--lines N]            XLT's logs (the sync driver's, the library's)
    xltlink.py rm [-r] PATH...            delete files or empty folders (-r: folders with what is in them)
    xltlink.py mv FROM TO                 rename (in the same folder; the PSP may refuse others)
    xltlink.py mkdir PATH                 make a folder, with its parents
    xltlink.py df [--fresh] [ms0:|ef0:]   free space (the PSP's own count, ~12 s the first time; then kept for a minute)
    xltlink.py selftest [DIR]             try every command on test files in DIR (default ms0:/DOWNLOADS/xltlink-test)
    xltlink.py speed [--mb N]             raw link speed, nothing written
    xltlink.py ctl                        the Windows (WinUSB) descriptors, as Windows would ask

Paths are the PSP's: "ms0:/VIDEO/a.mp4", or just "VIDEO/a.mp4" for ms0:. Only XLT's
folders can be changed: ISO, VIDEO, MUSIC, PICTURE, BOOKS, DOWNLOADS, PSP/GAME, and in
PSP/SAVEDATA a save folder and the files directly in it.

Needs pyusb (Arch: python-pyusb, Debian/Ubuntu: python3-usb, Fedora: python3-pyusb,
or pip install pyusb) and, on Linux, access to the device: tools/60-xltsync.rules.
The protocol is plugin/xltsync/xltsync.h: keep the two in step.
"""
from __future__ import annotations

import argparse
import errno
import os
import random
import struct
import sys
import time
import zlib

try:  # only Link needs it: the names and paths work without
    import usb.core
    import usb.util
except ImportError:
    usb = None
PYUSB_MISSING = ("pyusb is missing: install python-pyusb (Arch), python3-usb (Debian/Ubuntu), "
                 "python3-pyusb (Fedora) or 'pip install pyusb'")

VID, PID = 0x054C, 0x01CB
SUBCLASS, PROTOCOL = 0x58, 0x53
CAST_PROTOCOL = 0x43  # XLT Cast (plugin/xltcast): the same IDs and subclass, while a game is cast
NET_PROTOCOL = 0x4E   # the PSP's wired internet (plugin/xltnet): the same IDs and subclass too
VENDOR_REQ = 0x58
GUID = "{AE1513B4-0AA3-4365-B951-3261C67CE994}"
MAGIC = 0x53544C58
PROTO = 4
REPLY = 0x8000
PART = ".xltpart"

(HELLO, ECHO, SINK, SOURCE, CTLLOG, DF, LIST, STAT, MKDIR, PUT, PUT_END, DELETE,
 RENAME, GET, DELETE_TREE, READ, POWER) = range(1, 18)
DF_COUNTED, DF_KEPT, DF_DRIVER = range(3)  # how DF got its figure
OK, E_OP, E_SIZE, MORE, E_PATH, E_NOENT, E_EXIST, E_IO, E_FULL, E_STATE = range(10)
STATUS = {E_OP: "unknown command", E_SIZE: "size out of range", E_PATH: "path not allowed",
          E_NOENT: "not found", E_EXIST: "already there", E_IO: "the PSP refused",
          E_FULL: "Memory Stick full", E_STATE: "no file waiting"}
PUT_REPLACE = 1

HDR = struct.Struct("<IHHIII")      # magic, op, status, seq, len, arg
HELLO_S = struct.Struct("<IIII32sII")
DF_S = struct.Struct("<QQ")
TIME_S = struct.Struct("<6H")
ENTRY_S = struct.Struct("<I6HBB")   # size, mtime, is_dir, name_len; then the name
PUT_S = struct.Struct("<II6H")      # size, flags, mtime; then the path
PUTDONE_S = struct.Struct("<III")   # crc32, microseconds, written
REMOVED_S = struct.Struct("<IIQ")   # files, folders, bytes
READ_S = struct.Struct("<QI")       # offset, length; then the path
POWER_S = struct.Struct("<iiii")    # battery %, charging, plugged in, minutes left

TIMEOUT = 5000          # ms, a command and its reply
DATA_TIMEOUT = 30000    # ms, one transfer of a file (the stick can pause to allocate)
DF_TIMEOUT = 90000      # ms, the PSP driver counting free clusters on a big card (~12 s on 128 GB)
TREE_TIMEOUT = 600000   # ms, deleting a folder with thousands of files

MODELS = {0: "PSP-1000", 1: "PSP-2000", 2: "PSP-3000", 3: "PSP-3000", 4: "PSP Go", 6: "PSP-3000",
          8: "PSP-3000", 10: "PSP-E1000"}


class LinkError(Exception):
    """No XLT sync device, no access to it, or no pyusb: the message says which."""


class XltsError(Exception):
    def __init__(self, status: int, arg: int = 0, what: str = ""):
        self.status, self.arg = status, arg
        text = STATUS.get(status, f"status {status}")
        if status == E_IO and arg:
            text += f" ({arg & 0xFFFFFFFF:08x})"
        super().__init__(f"{what}: {text}" if what else text)


def psp_path(p: str) -> str:
    """'VIDEO/a.mp4' -> 'ms0:/VIDEO/a.mp4'; full paths kept (a trailing slash dropped)."""
    p = p.replace("\\", "/")
    if p[:7].lower() == "flash0:":  # the firmware's fonts, readable only
        return "flash0:/" + p[7:].lstrip("/").rstrip("/")
    if p[:4].lower() in ("ms0:", "ef0:"):
        p = p[:4].lower() + "/" + p[4:].lstrip("/")
    else:
        p = "ms0:/" + p.lstrip("/")
    return p.rstrip("/") if len(p) > 5 else p


# The PSP's FAT driver takes and gives names in Shift-JIS (Microsoft's cp932), not
# UTF-8: a UTF-8 name is stored garbled, and a name with a character Shift-JIS lacks
# (é, ü, –, emoji) comes back from the PSP cut off at that character, so the file
# can't be opened. Such characters are replaced by the nearest plain one.
_SUBST = {"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
          "ł": "l", "Ł": "L", "þ": "th", "Þ": "Th", "ð": "d", "ı": "i", "…": "...", "€": "EUR", "™": "TM",
          "©": "(c)", "®": "(R)", "ς": "σ"}  # Shift-JIS has no final sigma
_DASHES, _QUOTES = "‐‑‒–—―−", "‘’‚‛′“”„‟″«»‹›"
_FAT_BAD = '\\:*?"<>|'


def psp_name(name: str) -> str:
    """name as the PSP can hold it: every character Shift-JIS has, the rest replaced."""
    import unicodedata
    out = []
    for ch in name:
        if ch in _FAT_BAD or ord(ch) < 0x20:
            out.append("_")
            continue
        try:
            ch.encode("cp932")
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        if ch in _SUBST:
            out.append(_SUBST[ch])
        elif ch in _DASHES:
            out.append("-")
        elif ch in _QUOTES:
            out.append("'")
        else:
            # the letter without its accent (é -> e, ά -> α), if Shift-JIS has that
            plain = ""
            for c in unicodedata.normalize("NFKD", ch):
                if unicodedata.combining(c) or not c.isprintable():
                    continue
                try:
                    c.encode("cp932")
                    plain += c
                except UnicodeEncodeError:
                    pass
            out.append(plain or "_")
    result = "".join(out).rstrip(" .")  # FAT drops trailing dots and spaces
    return result or "_"


# The fallback when the PSP refuses a name anyway (80010016, EINVAL): seen once on an
# E1004 on 2026-11-24, every two-byte Shift-JIS name refused while ASCII ones worked,
# and not seen again since. Link then makes the name plain ASCII and tries once more:
# Greek and Cyrillic in Latin letters, accents dropped, the rest "_".
EINVAL = 0x80010016
_LATIN = dict(zip("αβγδεζηθικλμνξοπρσςτυφχψω",
                  "a v g d e z i th i k l m n x o p r s s t y f ch ps o".split()))
_LATIN.update(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґђјљњћџ",
                  "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya "
                  "i yi ye g dj j lj nj c dz".split()))
_LATIN = {k: ("" if v == "_" else v) for k, v in _LATIN.items()}
_PAIRS = {"ου": "ou", "αυ": "av", "ευ": "ev", "μπ": "b", "ντ": "nt"}  # Greek pairs read as one sound
_ASCII_SUBST = {"×": "x", "÷": "-", "°": "o", "·": "-", "¡": "!", "¿": "?"}


def _plain(ch: str) -> str:
    """One character as plain ASCII ("" if it has none)."""
    import unicodedata
    for table in (_SUBST, _ASCII_SUBST):
        if ch in table and table[ch].isascii():
            return table[ch]
    if ch in _DASHES:
        return "-"
    if ch in _QUOTES:
        return "'"
    out = ""
    for c in unicodedata.normalize("NFKD", ch):  # the letter without its accent (é -> e, ά -> α)
        if unicodedata.combining(c):
            continue
        if " " <= c <= "~":
            out += c
        elif c.lower() in _LATIN:
            lat = _LATIN[c.lower()]
            out += lat if c == c.lower() else lat[:1].upper() + lat[1:]
    return out


def ascii_name(name: str) -> str:
    """A name in plain ASCII, for when the PSP refuses its Shift-JIS form (see EINVAL)."""
    import unicodedata
    name = unicodedata.normalize("NFC", name)
    out, i = [], 0
    while i < len(name):
        ch = name[i]
        if ch in _FAT_BAD or ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("_")
            i += 1
            continue
        if " " <= ch <= "~":
            out.append(ch)
            i += 1
            continue
        pair = "".join(c for c in unicodedata.normalize("NFKD", name[i:i + 2]) if not unicodedata.combining(c))
        if len(pair) == 2 and pair.lower() in _PAIRS:
            lat = _PAIRS[pair.lower()]
            out.append(lat.upper() if pair.isupper() else lat.capitalize() if pair[0].isupper() else lat)
            i += 2
            continue
        lat = _plain(ch)
        if len(lat) > 1 and ch.isupper() and name[i + 1:i + 2].isupper():  # ΧΡΗΣΤΟΣ -> CHRISTOS
            lat = lat.upper()
        out.append(lat or "_")
        i += 1
    return "".join(out).rstrip(" .") or "_"


def psp_bytes(path: str) -> bytes:
    """A full PSP path, each name made holdable, in the PSP's encoding, NUL-terminated."""
    p = psp_path(path)
    cut = 8 if p.startswith("flash0:/") else 5
    dev, parts = p[:cut], [n for n in p[cut:].split("/") if n]
    if any(n in (".", "..") for n in parts):  # never renamed into something harmless-looking: refused
        raise XltsError(E_PATH, 0, p)
    return (dev + "/".join(psp_name(n) for n in parts)).encode("cp932") + b"\0"


def psp_shown(path) -> str:
    """The path as it ends up on the PSP (psp_bytes as text). Bytes: sent as they are."""
    raw = path if isinstance(path, bytes) else psp_bytes(path)
    return raw.rstrip(b"\0").decode("cp932", errors="replace")


def psp_time(t: float) -> bytes:
    lt = time.localtime(t)
    return TIME_S.pack(lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)


class Entry:
    def __init__(self, name: str, size: int, mtime: tuple, is_dir: bool):
        self.name, self.size, self.mtime, self.is_dir = name, size, mtime, is_dir

    def __repr__(self):
        return f"Entry({self.name!r}, {self.size}, dir={self.is_dir})"


def parse_entries(data: bytes) -> list[Entry]:
    out, at = [], 0
    while at < len(data):
        size, y, mo, d, h, mi, s, is_dir, n = ENTRY_S.unpack_from(data, at)
        at += ENTRY_S.size
        name = data[at:at + n].decode("cp932", errors="replace")
        at += n
        out.append(Entry(name, size, (y, mo, d, h, mi, s), bool(is_dir)))
    return out


def other_mode(protocol: int) -> str:
    if protocol == CAST_PROTOCOL:
        return "No XLT sync device: the PSP is casting a game (XLT Cast)"
    if protocol == NET_PROTOCOL:
        return "No XLT sync device: the PSP is on its wired internet link"
    return f"No XLT sync device: the PSP is in another mode (protocol {protocol:#x})"


class Link:
    """One XLT sync device. Every method raises XltsError when the PSP says no,
    usb.core.USBError when the link itself fails."""

    def __init__(self, backend=None):
        self.renamed = {}  # path asked for (lower case) -> the ASCII path made instead (see EINVAL)
        if usb is None:
            raise LinkError(PYUSB_MISSING)
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise LinkError("No XLT sync device (054c:01cb). Is USB sync On in XLT and the cable in? "
                            "If it is, unplug the cable and plug it in again.")
        self.dev = dev
        try:
            self._setup(dev)
        except BaseException:
            self.close()  # it may be open already: let it go now, not when Python collects it
            raise

    def close(self):
        """Lets the device go at once. Until then Windows keeps it from everyone else, XLT Cast
        included when the PSP comes back as Cast's device (the same IDs, the same Windows device)."""
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: BLE001 - gone already
            pass

    def _setup(self, dev):
        try:  # from the descriptor the system keeps: no need to open the device
            other = [i.bInterfaceProtocol for i in dev[0]
                     if i.bInterfaceSubClass == SUBCLASS and i.bInterfaceProtocol != PROTOCOL]
        except (usb.core.USBError, IndexError):
            other = []
        if other:
            # Before opening: on Windows the Cast window (or the net link) holds it, and opening fails
            # as if the driver were wrong
            raise LinkError(other_mode(other[0]))
        try:
            # Only when not set up yet: setting the configuration again makes the PSP
            # start over (it cancels what it was waiting for, then attaches anew).
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError:
                cfg = None
            if cfg is None or cfg.bConfigurationValue != 1:
                dev.set_configuration()
        except usb.core.USBError as e:
            if sys.platform == "win32" and e.errno in (errno.EACCES, errno.ENOSYS):
                # libusb can't open a device whose driver isn't WinUSB ("not supported").
                raise LinkError("Permission denied: Windows has not given the PSP the WinUSB driver. "
                                "Unplug the PSP and plug it in again, or set the driver up from XLT Tool")
            if e.errno == 13:
                raise LinkError("Permission denied: install tools/60-xltsync.rules (see the file), "
                                "then unplug and replug the PSP")
            raise
        cfg = dev.get_active_configuration()
        intf = usb.util.find_descriptor(cfg, bInterfaceClass=0xFF, bInterfaceSubClass=SUBCLASS)
        if intf is None:
            raise LinkError("The device has no XLT sync interface")
        if intf.bInterfaceProtocol != PROTOCOL:
            # the same IDs, but XLT Cast's or the net link's device: not ours to open
            raise LinkError(other_mode(intf.bInterfaceProtocol))
        try:
            usb.util.claim_interface(dev, intf)
        except usb.core.USBError as e:
            if e.errno == 16:
                raise LinkError("Another program is using the PSP's sync link (XLT Tool?): close it first")
            raise
        is_in = lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
        self.ep_in = usb.util.find_descriptor(intf, custom_match=is_in)
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: not is_in(e))
        self.packet = self.ep_out.wMaxPacketSize
        self.seq = 0
        try:  # whatever a previous run left half-sent on the PSP's side
            while self.ep_in.read(512, 200):
                pass
        except usb.core.USBError:
            pass
        self.info = self.hello()
        if self.info["proto"] != PROTO:
            raise LinkError(f"The PSP speaks protocol {self.info['proto']}, this tool {PROTO}: "
                            "update XLT or this tool")

    # -- framing
    def _send(self, op: int, payload: bytes = b"", arg: int = 0, length: int | None = None):
        self.seq += 1
        n = len(payload) if length is None else length
        self.ep_out.write(HDR.pack(MAGIC, op, 0, self.seq, n, arg), TIMEOUT)
        if payload:
            self.ep_out.write(payload, TIMEOUT)

    def _reply(self, op: int, timeout: int = TIMEOUT) -> tuple[int, int, bytes]:
        raw = bytes(self.ep_in.read(512, timeout))
        if len(raw) != HDR.size:
            raise RuntimeError(f"reply of {len(raw)} bytes, expected {HDR.size}")
        magic, rop, status, seq, length, arg = HDR.unpack(raw)
        if magic != MAGIC or rop != (op | REPLY) or seq != self.seq:
            raise RuntimeError(f"unexpected reply: op {rop:#x} seq {seq} (sent op {op} seq {self.seq})")
        data = bytes(self.ep_in.read(length, timeout)) if length else b""
        if len(data) != length:
            raise RuntimeError(f"reply data {len(data)} bytes, expected {length}")
        return status, arg, data

    def _call(self, op: int, payload: bytes = b"", arg: int = 0, what: str = "",
              ok=(OK,), timeout: int = TIMEOUT) -> tuple[int, int, bytes]:
        self._send(op, payload, arg)
        status, rarg, data = self._reply(op, timeout)
        if status not in ok:
            raise XltsError(status, rarg, what)
        return status, rarg, data

    def _path(self, p) -> bytes:
        """str: made holdable (psp_bytes), inside a folder made under an ASCII name if it was.
        bytes: sent exactly as given (tests of the PSP's own checks)."""
        return p.rstrip(b"\0") + b"\0" if isinstance(p, bytes) else psp_bytes(self.real(p))

    def real(self, path: str) -> str:
        """Where path is on the PSP: the same, unless it (or a folder it is in) had to be made
        under an ASCII name in this session."""
        p = psp_shown(path)  # names as the PSP holds them, as the keys are
        low = p.lower()
        for asked in sorted(getattr(self, "renamed", {}), key=len, reverse=True):
            if low == asked or low.startswith(asked + "/"):
                return self.renamed[asked] + p[len(asked):]
        return p

    def _ascii_retry(self, path, e: XltsError):
        """The ASCII path to try instead when the PSP refused path's name (None: not that case)."""
        if isinstance(path, bytes) or e.status != E_IO or (e.arg & 0xFFFFFFFF) != EINVAL:
            return None
        p = self.real(path)
        folder, name = p.rsplit("/", 1)
        if psp_name(name).isascii():
            return None
        return f"{folder}/{ascii_name(name)}"

    def _made_as(self, path: str, alt: str):
        if not hasattr(self, "renamed"):
            self.renamed = {}
        self.renamed[psp_shown(path).lower()] = alt

    # -- commands
    def hello(self) -> dict:
        _, _, data = self._call(HELLO)
        proto, buf, speed, model, name, put_chunk, devices = HELLO_S.unpack_from(data.ljust(HELLO_S.size, b"\0"))
        return dict(proto=proto, buf_size=buf, speed=speed, model=model,
                    name=name.split(b"\0")[0].decode(errors="replace"), put_chunk=put_chunk, devices=devices)

    def df(self, dev: str = "ms0:", fresh: bool = False) -> tuple[int, int]:
        """(free, total) bytes. The PSP counts once, then keeps the figure for a minute and
        moves it with sync's own changes; fresh=True counts again. self.df_how says which."""
        _, how, data = self._call(DF, dev[:4].encode() + b"\0", 1 if fresh else 0, what=dev, timeout=DF_TIMEOUT)
        self.df_how = how
        return DF_S.unpack(data)

    def list(self, path: str) -> list[Entry]:
        out, skip = [], 0
        while True:
            status, count, data = self._call(LIST, self._path(path), skip, psp_path(path), ok=(OK, MORE))
            out += parse_entries(data)
            skip += count
            if status != MORE:
                return out

    def stat(self, path: str) -> Entry | None:
        try:
            _, _, data = self._call(STAT, self._path(path), what=psp_path(path))
        except XltsError as e:
            if e.status == E_NOENT:
                return None
            raise
        return parse_entries(data)[0]

    def mkdir(self, path: str) -> str:
        """Returns the folder's path on the PSP (an ASCII name if the PSP refused the name)."""
        try:
            self._call(MKDIR, self._path(path), what=psp_path(path))
            return path if isinstance(path, bytes) else self.real(path)
        except XltsError as e:
            alt = self._ascii_retry(path, e)
            if alt is None:
                raise
        self._call(MKDIR, psp_bytes(alt), what=alt)
        self._made_as(path, alt)
        return alt

    def delete(self, path: str):
        self._call(DELETE, self._path(path), what=psp_path(path))

    def delete_tree(self, path: str) -> dict:
        """A folder and everything in it (or a file), removed by the PSP in one command.
        Returns {files, folders, bytes}. On an error, what was removed so far stays removed."""
        self._send(DELETE_TREE, self._path(path))
        status, err, data = self._reply(DELETE_TREE, TREE_TIMEOUT)
        files, folders, size = REMOVED_S.unpack(data) if len(data) == REMOVED_S.size else (0, 0, 0)
        if status:
            e = XltsError(status, err, psp_path(path))
            e.removed = dict(files=files, folders=folders, bytes=size)
            raise e
        return dict(files=files, folders=folders, bytes=size)

    def rename(self, src: str, dst: str) -> str:
        """Returns the new path on the PSP (an ASCII name if the PSP refused the name)."""
        what = f"{psp_path(src)} -> {psp_path(dst)}"
        try:
            self._call(RENAME, self._path(src) + self._path(dst), what=what)
            return dst if isinstance(dst, bytes) else self.real(dst)
        except XltsError as e:
            alt = self._ascii_retry(dst, e)
            if alt is None:
                raise
        self._call(RENAME, self._path(src) + psp_bytes(alt), what=what)
        self._made_as(dst, alt)
        return alt

    def put(self, local: str, remote: str, replace: bool = False, progress=None) -> dict:
        """Sends one file. progress(sent, total) is called as it goes. Returns
        {bytes, seconds (PC), psp_seconds (writing on the PSP), name (the path as on the PSP:
        an ASCII name if the PSP refused the name)}."""
        size = os.path.getsize(local)
        if size >= 1 << 32:
            raise XltsError(E_SIZE, 0, f"{local}: 4 GB or more (FAT32 can't hold it)")
        req = PUT_S.pack(size, PUT_REPLACE if replace else 0, 0, 0, 0, 0, 0, 0)
        req = req[:8] + psp_time(os.path.getmtime(local))
        try:
            _, chunk, _ = self._call(PUT, req + self._path(remote), what=psp_shown(self._path(remote)))
        except XltsError as e:
            alt = self._ascii_retry(remote, e)
            if alt is None:
                raise
            _, chunk, _ = self._call(PUT, req + psp_bytes(alt), what=alt)
            self._made_as(remote, alt)
        shown = psp_shown(self._path(remote))
        crc, sent, t0 = 0, 0, time.perf_counter()
        with open(local, "rb") as f:
            while sent < size:
                block = f.read(min(chunk, size - sent))
                if not block:
                    raise OSError(f"{local} got shorter while sending")
                self.ep_out.write(block, DATA_TIMEOUT)
                crc = zlib.crc32(block, crc)
                sent += len(block)
                if progress:
                    progress(sent, size)
        status, err, data = self._reply(PUT, DATA_TIMEOUT)
        if status:
            raise XltsError(status, err, shown)
        psp_crc, us, written = PUTDONE_S.unpack(data)
        keep = psp_crc == crc and written == size
        self._call(PUT_END, arg=1 if keep else 0, what=shown)
        if not keep:
            raise RuntimeError(f"{shown}: the PSP wrote something else "
                               f"(CRC {psp_crc:08x}, expected {crc:08x}): not kept")
        return dict(bytes=size, seconds=time.perf_counter() - t0, psp_seconds=us / 1e6, name=shown)

    def get(self, remote, local: str | None = None, progress=None) -> bytes:
        """A file from the PSP: its bytes, or, with local, written there as it comes
        (through local + ".part", renamed once the CRC matches; returns b"")."""
        chunk = self.info["put_chunk"]
        _, size, _ = self._call(GET, self._path(remote), chunk, psp_shown(remote))
        out, crc, got = bytearray(), 0, 0
        f = open(local + ".part", "wb") if local else None
        try:
            while got < size:
                data = bytes(self.ep_in.read(min(chunk, size - got), DATA_TIMEOUT))
                if f:
                    f.write(data)
                else:
                    out += data
                got += len(data)
                crc = zlib.crc32(data, crc)
                if progress:
                    progress(got, size)
            status, psp_crc, _ = self._reply(GET, DATA_TIMEOUT)
            if status or psp_crc != crc:
                raise RuntimeError(f"{psp_shown(remote)}: changed while it was read (CRC {psp_crc:08x}, got {crc:08x})")
            if f:
                f.close()
                os.replace(local + ".part", local)
                f = None
        finally:
            if f:
                f.close()
                os.remove(local + ".part")
        return bytes(out)

    def power(self) -> dict:
        """The battery: {battery (percent, -1 none), charging, plugged, minutes (-1 unknown)}."""
        _, _, data = self._call(POWER)
        b, c, p, m = POWER_S.unpack(data)
        return dict(battery=b, charging=bool(c), plugged=bool(p), minutes=m)

    def read(self, remote, offset: int, length: int) -> bytes:
        """A piece of a file on the PSP (up to the PSP's buffer size; fewer bytes at its end)."""
        out = bytearray()
        step = self.info["buf_size"]
        while length > 0:
            n = min(step, length)
            _, got, data = self._call(READ, READ_S.pack(offset, n) + self._path(remote), what=psp_shown(remote))
            out += data
            if len(data) < n:
                break
            offset += n
            length -= n
        return bytes(out)

    # -- link tests
    def echo(self, payload: bytes) -> bytes:
        _, _, data = self._call(ECHO, payload)
        return data

    def sink(self, total: int, chunk: int) -> tuple[float, float]:
        block = bytes(range(256)) * (chunk // 256)
        self._send(SINK, arg=chunk, length=total)
        t0, sent = time.perf_counter(), 0
        while sent < total:
            n = min(chunk, total - sent)
            self.ep_out.write(block[:n], TIMEOUT)
            sent += n
        status, us, _ = self._reply(SINK)
        if status:
            raise XltsError(status, us, "sink")
        return time.perf_counter() - t0, us / 1e6

    def source(self, total: int, chunk: int) -> tuple[float, bytes]:
        self._send(SOURCE, arg=chunk, length=total)
        status, arg, _ = self._reply(SOURCE)
        if status:
            raise XltsError(status, arg, "source")
        t0, got, first = time.perf_counter(), 0, b""
        while got < total:
            data = self.ep_in.read(min(chunk, total - got), TIMEOUT)
            first = first or bytes(data[:16])
            got += len(data)
        return time.perf_counter() - t0, first

    def ctllog(self) -> str:
        _, _, data = self._call(CTLLOG)
        return data.decode(errors="replace")


# ---------------------------------------------------------------- the commands

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def rate(n: int, s: float) -> str:
    return f"{n / s / 1048576:.2f} MB/s" if s > 0 else "?"


def power_text(p: dict) -> str:
    """'64%, charging' / '80% · about 3 h 10 min left' / 'on the charger, no battery'."""
    if p["battery"] < 0:
        return "on the charger, no battery" if p["plugged"] else "no battery"
    text = f"{p['battery']}%"
    if p["charging"]:
        text += ", charging"
    elif p["plugged"]:
        text += ", on the charger"
    elif p["minutes"] >= 10:  # (a PSP-E1000 says 1 minute at 100%: no real estimate there)
        text += f" · about {p['minutes'] // 60} h {p['minutes'] % 60} min left" if p["minutes"] >= 60 \
            else f" · about {p['minutes']} min left"
    return text


def cmd_info(link: Link, a):
    h = link.info
    speed = {1: "full speed (USB 1.1)", 2: "high speed (USB 2.0)"}.get(h["speed"], f"speed {h['speed']}")
    devs = ", ".join(d for bit, d in ((1, "ms0: (Memory Stick)"), (2, "ef0: (internal)")) if h["devices"] & bit)
    print(f"{h['name']}, protocol {h['proto']}, {MODELS.get(h['model'], 'model ' + str(h['model']))}, {speed}")
    print(f"storage: {devs or 'none'}; transfers of {h['put_chunk'] // 1024} KB")
    print(f"battery: {power_text(link.power())}")


def cmd_ls(link: Link, a):
    path = a.path or "ms0:/"
    entries = sorted(link.list(path), key=lambda e: (not e.is_dir, e.name.lower()))
    for e in entries:
        y, mo, d, h, mi, _ = e.mtime
        size = "<dir>" if e.is_dir else human(e.size)
        print(f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}  {size:>10}  {e.name}{'/' if e.is_dir else ''}")
    print(f"{len(entries)} entries in {psp_path(path)}")


def progress_bar(name: str):
    t0 = time.perf_counter()
    last = [0.0]

    def show(sent, total):
        now = time.perf_counter()
        if now - last[0] < 0.2 and sent < total:
            return
        last[0] = now
        pct = sent * 100 // total if total else 100
        print(f"\r  {name[:40]:40} {pct:3d}%  {human(sent):>9}  {rate(sent, now - t0):>11}", end="", flush=True)
    return show


def cmd_put(link: Link, a):
    dest = a.dest
    dest_is_dir = dest.endswith("/") or len(a.files) > 1 or any(os.path.isdir(f) for f in a.files)
    if not dest_is_dir:
        st = link.stat(dest)
        dest_is_dir = bool(st and st.is_dir)
    jobs = []
    for f in a.files:
        f = f.rstrip(os.sep) or f
        if os.path.isdir(f):
            base = os.path.basename(os.path.abspath(f))
            for root, dirs, files in os.walk(f):
                dirs.sort()
                rel = os.path.relpath(root, f)
                for name in sorted(files):
                    sub = base if rel == "." else f"{base}/{rel.replace(os.sep, '/')}"
                    jobs.append((os.path.join(root, name), f"{psp_path(dest)}/{sub}/{name}"))
        else:
            jobs.append((f, f"{psp_path(dest)}/{os.path.basename(f)}" if dest_is_dir else psp_path(dest)))
    total, t0, failed = 0, time.perf_counter(), 0
    for local, remote in jobs:
        try:
            r = link.put(local, remote, replace=a.replace, progress=progress_bar(os.path.basename(local)))
            renamed = f" (saved as {r['name']}: the PSP can't hold some characters)" if r["name"] != psp_path(remote) else ""
            print(f"\r  {remote}{renamed}: {human(r['bytes'])}, {rate(r['bytes'], r['seconds'])} "
                  f"(writing on the PSP: {rate(r['bytes'], r['psp_seconds'])})".ljust(100))
            total += r["bytes"]
        except XltsError as e:
            print(f"\r  {e}".ljust(100))
            failed += 1
    if len(jobs) > 1:
        print(f"{len(jobs) - failed} of {len(jobs)} files, {human(total)} in "
              f"{time.perf_counter() - t0:.1f} s ({rate(total, time.perf_counter() - t0)})")
    return 1 if failed else 0


def cmd_get(link: Link, a):
    local = a.local or os.path.basename(psp_path(a.remote))
    if os.path.isdir(local):
        local = os.path.join(local, os.path.basename(psp_path(a.remote)))
    data = link.get(a.remote, local, progress=progress_bar(os.path.basename(local)))
    print(f"\r  {psp_shown(a.remote)} -> {local}: {human(len(data))}".ljust(100))


def cmd_log(link: Link, a):
    """XLT's logs: the sync driver's and the library's (start-up, scans, USB sync)."""
    for path in ("ms0:/PSP/GAME/XLT/xltsync.log", "ms0:/PSP/GAME/XLT/cache/scan.log"):
        try:
            text = link.get(path).decode("utf-8", errors="replace")
        except XltsError as e:
            print(f"== {path}: {e}")
            continue
        lines = text.splitlines()
        print(f"== {path} ({len(lines)} lines{', last ' + str(a.lines) if len(lines) > a.lines else ''})")
        print("\n".join(lines[-a.lines:]))


def cmd_rm(link: Link, a):
    for p in a.paths:
        if a.r:
            r = link.delete_tree(p)
            print(f"deleted {psp_path(p)}: {r['files']} files, {r['folders']} folders, {human(r['bytes'])}")
        else:
            link.delete(p)
            print(f"deleted {psp_path(p)}")


def cmd_mv(link: Link, a):
    link.rename(a.src, a.dst)
    print(f"{psp_path(a.src)} -> {psp_path(a.dst)}")


def cmd_mkdir(link: Link, a):
    link.mkdir(a.path)
    print(f"made {psp_path(a.path)}")


def cmd_df(link: Link, a):
    t0 = time.perf_counter()
    free, total = link.df(a.dev or "ms0:", a.fresh)
    how = {DF_COUNTED: "counted from the FAT (older XLT)", DF_KEPT: "kept from the last count",
           DF_DRIVER: "the PSP's own count"}.get(link.df_how, "?")
    print(f"{a.dev or 'ms0:'} {human(free)} free of {human(total)} ({how}, {time.perf_counter() - t0:.1f} s)")


def cmd_speed(link: Link, a):
    total = a.mb << 20
    for chunk in sorted({64 * 1024, link.info["buf_size"]}):
        pc, psp = link.sink(total, chunk)
        print(f"PC->PSP {a.mb} MB in {chunk // 1024:3d} KB transfers: {rate(total, pc)} (PC clock), "
              f"{rate(total, psp)} (PSP clock)")
        pc, first = link.source(total, chunk)
        ok = first == struct.pack("<IIII", 0, 4, 8, 12)
        print(f"PSP->PC {a.mb} MB in {chunk // 1024:3d} KB transfers: {rate(total, pc)}"
              f"{'' if ok else '  data pattern WRONG'}")


def cmd_ctl(link: Link, a):
    """What Windows asks a new device, and whether the answers are right."""
    dev, bad = link.dev, 0

    def check(what, ok, detail=""):
        nonlocal bad
        bad += not ok
        print(f"  {'ok  ' if ok else 'BAD '} {what}{': ' + detail if detail else ''}")

    import uuid
    check("USB 2.01 (Windows then asks for the BOS)", dev.bcdUSB == 0x0201, f"bcdUSB {dev.bcdUSB:04x}, bcdDevice {dev.bcdDevice:04x}")
    # MS OS 2.0 (Windows 8.1+): the BOS names the vendor request; it returns WINUSB and the GUID
    try:
        head = bytes(dev.ctrl_transfer(0x80, 6, 0x0F00, 0, 5, 1000))
        bos = bytes(dev.ctrl_transfer(0x80, 6, 0x0F00, 0, struct.unpack_from("<H", head, 2)[0], 1000))
        plat = uuid.UUID(bytes_le=bos[9:25]) == uuid.UUID("d8dd60df-4589-4cc7-9cd2-659d9e648a9f")
        setlen, code = struct.unpack_from("<HB", bos, 29)
        check("BOS: MS OS 2.0 platform capability", plat and code == VENDOR_REQ, f"{len(bos)} bytes, set of {setlen}")
        raw = bytes(dev.ctrl_transfer(0xC0, code, 0, 7, setlen, 1000))
        text = raw.decode("utf-16-le", errors="replace")
        check("MS OS 2.0 descriptor set: WINUSB and the GUID", len(raw) == setlen and b"WINUSB" in raw and GUID in text,
              f"{len(raw)} bytes")
    except (usb.core.USBError, struct.error) as e:
        check("MS OS 2.0", False, str(e))
    # MS OS 1.0 (Windows 7, and 8+ if 2.0 fails): string 0xEE, then compatible ID; the GUID request
    # (bmRequestType 0xC1) is refused by the PSP's own bus driver, so 1.0 gives the driver only
    try:
        raw = bytes(dev.ctrl_transfer(0x80, 6, 0x03EE, 0, 0x12, 1000))
        check("MS OS 1.0: string 0xEE", raw[:16] == b"\x12\x03" + "MSFT100".encode("utf-16-le")
              and raw[16] == VENDOR_REQ, raw.hex())
    except usb.core.USBError as e:
        check("MS OS 1.0: string 0xEE", False, str(e))
    try:
        raw = bytes(dev.ctrl_transfer(0xC0, VENDOR_REQ, 0, 4, 0x28, 1000))
        check("MS OS 1.0: compatible ID WINUSB", len(raw) == 40 and raw[18:24] == b"WINUSB", raw.hex())
    except usb.core.USBError as e:
        check("MS OS 1.0: compatible ID WINUSB", False, str(e))
    for name, args in (("unknown vendor request (should be refused at once)", (0xC0, 0x99, 0, 0, 8)),):
        t0 = time.perf_counter()
        try:
            raw = bytes(dev.ctrl_transfer(*args, 1000))
            check(name, False, f"answered {raw.hex()}")
        except usb.core.USBError as e:
            quick = time.perf_counter() - t0 < 0.5
            check(name, e.errno == 32 or quick, f"{e} after {time.perf_counter() - t0:.2f} s")
    try:
        link.hello()
        check("the link still works afterwards", True)
    except Exception as e:
        check("the link still works afterwards", False, str(e))
    try:
        log = link.ctllog()
        print("What the PSP's driver was handed (type req value index length -> 0 answered, 1 bus driver, 2 refused):")
        print("".join("  " + line + "\n" for line in log.splitlines()) or "  nothing", end="")
    except Exception as e:
        print(f"CTLLOG failed: {e}")
    return 1 if bad else 0


def cmd_selftest(link: Link, a):
    """Every command on throw-away files; changes only the test folder."""
    import tempfile
    base = psp_path(a.dir or "ms0:/DOWNLOADS/xltlink-test")
    chunk = link.info["put_chunk"]
    bad = 0

    def check(what, ok, detail=""):
        nonlocal bad
        bad += not ok
        print(f"  {'ok  ' if ok else 'BAD '} {what}{': ' + detail if detail else ''}")

    def refused(what, fn, status):
        try:
            fn()
            check(what, False, "it was allowed")
        except XltsError as e:
            check(what, e.status == status, str(e))

    rnd = random.Random(1234)
    with tempfile.TemporaryDirectory(prefix="xltlink-") as tmp:
        sizes = [0, 1, 511, 512, 513, chunk - 1, chunk, chunk + 1, 2 * chunk + 1000, 5 * 1048576 + 7]
        files = {}
        for n in sizes:
            p = os.path.join(tmp, f"t{n}.bin")
            with open(p, "wb") as f:
                f.write(rnd.randbytes(n))
            os.utime(p, (1700000000, 1700000000))  # 2023-11-14, local time
            files[n] = p
        print(f"Test folder {base}")
        link.mkdir(base)
        t0 = time.perf_counter()
        free0, total = link.df(fresh=True)
        check("df counts", total > 0 and 0 < free0 <= total and link.df_how in (DF_COUNTED, DF_DRIVER),
              f"{human(free0)} free, {'from the FAT' if link.df_how == DF_COUNTED else 'the driver'}, "
              f"{time.perf_counter() - t0:.1f} s")
        for n, p in files.items():
            try:
                r = link.put(p, f"{base}/t{n}.bin", replace=True)
                st = link.stat(f"{base}/t{n}.bin")
                want = time.localtime(1700000000)[:6]
                check(f"put {n} bytes", st is not None and st.size == n,
                      f"{rate(n, r['psp_seconds'])} writing" if n >= 1048576 else "")
                if n == 1:
                    check("its date kept", st is not None and st.mtime == tuple(want), f"{st.mtime if st else None}")
            except (XltsError, RuntimeError) as e:
                check(f"put {n} bytes", False, str(e))
        names = {e.name for e in link.list(base)}
        check("ls shows them all", names == {f"t{n}.bin" for n in sizes}, f"{len(names)} entries")
        check("no .xltpart left", not any(n.endswith(PART) for n in names))
        refused("put over a file without --replace", lambda: link.put(files[1], f"{base}/t1.bin"), E_EXIST)
        link.put(files[513], f"{base}/t1.bin", replace=True)
        st = link.stat(f"{base}/t1.bin")
        check("put --replace replaces", st is not None and st.size == 513)
        link.rename(f"{base}/t0.bin", f"{base}/renamed.bin")
        check("rename", link.stat(f"{base}/renamed.bin") is not None and link.stat(f"{base}/t0.bin") is None)
        link.mkdir(f"{base}/sub/deeper")
        st = link.stat(f"{base}/sub/deeper")
        check("mkdir with parents", st is not None and st.is_dir)
        uni = "Ελληνικά – ünïcödé ♪ ソ.bin"  # Greek, a dash and accents Shift-JIS lacks, ソ (trail byte '\\')
        want = psp_name(uni)
        try:
            r = link.put(files[1], f"{base}/{uni}", replace=True)
            names = {e.name for e in link.list(base)}
            check("a non-ASCII name: kept what the PSP can hold", r["name"].endswith("/" + want) and want in names,
                  f"{want!r}")
        except XltsError as e:
            check("a non-ASCII name", False, str(e))

        refused("write outside XLT's folders (SEPLUGINS)", lambda: link.put(files[1], "ms0:/SEPLUGINS/x.bin"), E_PATH)
        refused("write to PSP/SYSTEM", lambda: link.put(files[1], "ms0:/PSP/SYSTEM/x.bin"), E_PATH)
        refused("this tool refuses a path with ..", lambda: link.put(files[1], f"{base}/../../SEPLUGINS/x.bin"), E_PATH)
        # sent byte for byte, past this tool's own clean-up: the PSP's checks
        raw = base.encode() + b"/"
        refused("the PSP refuses a path with ..", lambda: link.put(files[1], raw + b"../../SEPLUGINS/x.bin"), E_PATH)
        refused("the PSP refuses a name FAT can't hold", lambda: link.put(files[1], raw + b"a?b.bin"), E_PATH)
        refused("the PSP refuses a broken Shift-JIS name", lambda: link.put(files[1], raw + b"e\xe9.bin"), E_PATH)
        refused("the PSP refuses an empty name (//)", lambda: link.put(files[1], raw + b"sub//x.bin"), E_PATH)
        refused("delete a root folder (VIDEO)", lambda: link.delete("ms0:/VIDEO"), E_PATH)
        refused("delete a missing file", lambda: link.delete(f"{base}/missing.bin"), E_NOENT)

        p = link.power()
        check("power", -1 <= p["battery"] <= 100 and (p["battery"] >= 0 or p["plugged"]), power_text(p))

        # pieces of a file
        with open(files[2 * chunk + 1000], "rb") as f:
            whole = f.read()
        r1 = link.read(f"{base}/t{2 * chunk + 1000}.bin", 1000, 5000)
        r2 = link.read(f"{base}/t{2 * chunk + 1000}.bin", len(whole) - 10, 100)
        r3 = link.read(f"{base}/t{2 * chunk + 1000}.bin", 0, len(whole))
        check("read pieces of a file", r1 == whole[1000:6000] and r2 == whole[-10:] and r3 == whole,
              f"{len(r1)}, {len(r2)} (at the end), {len(r3)} (in {-(-len(whole) // link.info['buf_size'])} reads)")
        check("read past the end", link.read(f"{base}/t{2 * chunk + 1000}.bin", len(whole) + 5, 10) == b"")
        refused("read a missing file", lambda: link.read(f"{base}/missing.bin", 0, 10), E_NOENT)

        # free space kept and moved with sync's own changes
        t0 = time.perf_counter()
        free1, _ = link.df()
        check("df again is kept, not counted", link.df_how == DF_KEPT and time.perf_counter() - t0 < 0.5,
              f"{time.perf_counter() - t0:.2f} s")
        big = 5 * 1048576 + 7
        check("df went down by what was sent", free0 - free1 >= sum(sizes) - 4 * 1048576,
              f"{human(free0 - free1)} less")
        link.delete(f"{base}/t{big}.bin")
        free2, _ = link.df()
        check("df goes up by a deleted file", free2 - free1 >= big, f"{human(free2 - free1)} more")

        # whole folders
        for d in ("tree", "tree/a", "tree/a/b", "tree/c"):
            link.mkdir(f"{base}/{d}")
        for i, d in enumerate(("tree", "tree/a", "tree/a/b", "tree/a/b", "tree/c")):
            link.put(files[513], f"{base}/{d}/f{i}.bin", replace=True)
        refused("delete (not -r) a folder with files", lambda: link.delete(f"{base}/tree"), E_IO)
        try:
            r = link.delete_tree(f"{base}/tree")
            check("delete a folder tree", r == dict(files=5, folders=4, bytes=5 * 513) and
                  link.stat(f"{base}/tree") is None, f"{r}")
        except XltsError as e:
            check("delete a folder tree", False, str(e))
        r = link.delete_tree(f"{base}/t1.bin")
        check("delete_tree of a file", r["files"] == 1 and link.stat(f"{base}/t1.bin") is None)
        refused("delete_tree of a root folder (VIDEO)", lambda: link.delete_tree("ms0:/VIDEO"), E_PATH)
        refused("delete_tree of PSP/GAME", lambda: link.delete_tree("ms0:/PSP/GAME"), E_PATH)
        # (XLT's own folder is refused too; not tried here: if XLT ran from elsewhere, it would go)
        refused("delete_tree of a missing folder", lambda: link.delete_tree(f"{base}/missing"), E_NOENT)

        # save data: a save folder and the files in it, nothing else
        save = "ms0:/PSP/SAVEDATA/XLTLINKTEST00"
        try:
            link.put(files[513], f"{save}/DATA.BIN", replace=True)
            st = link.stat(f"{save}/DATA.BIN")
            check("put a file into a save folder", st is not None and st.size == 513)
            link.rename(save, save[:-2] + "01")
            check("rename a save folder", link.stat(save[:-2] + "01/DATA.BIN") is not None)
            link.rename(save[:-2] + "01", save)
        except XltsError as e:
            check("save data", False, str(e))
        refused("a file beside the save folders", lambda: link.put(files[1], "ms0:/PSP/SAVEDATA/x.bin"), E_PATH)
        refused("a folder inside a save folder", lambda: link.mkdir(f"{save}/sub"), E_PATH)
        refused("a file deeper in a save folder", lambda: link.put(files[1], f"{save}/sub/x.bin"), E_PATH)
        refused("delete PSP/SAVEDATA", lambda: link.delete_tree("ms0:/PSP/SAVEDATA"), E_PATH)
        refused("move a save's file out of SAVEDATA", lambda: link.rename(f"{save}/DATA.BIN", f"{base}/DATA.BIN"), E_PATH)
        try:
            r = link.delete_tree(save)
            check("delete a save folder", r["files"] == 1 and link.stat(save) is None, f"{r}")
        except XltsError as e:
            check("delete a save folder", False, str(e))

        r = link.delete_tree(base)
        check("cleaned up (delete_tree)", link.stat(base) is None, f"{r['files']} files, {r['folders']} folders")
    print("All passed." if not bad else f"{bad} FAILED.")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[2:]))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(fn=cmd_info)
    p = sub.add_parser("ls"); p.add_argument("path", nargs="?"); p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("put"); p.add_argument("files", nargs="+"); p.add_argument("dest")
    p.add_argument("--replace", action="store_true", help="replace files already there"); p.set_defaults(fn=cmd_put)
    p = sub.add_parser("get"); p.add_argument("remote"); p.add_argument("local", nargs="?"); p.set_defaults(fn=cmd_get)
    p = sub.add_parser("log"); p.add_argument("--lines", type=int, default=40); p.set_defaults(fn=cmd_log)
    p = sub.add_parser("rm"); p.add_argument("-r", action="store_true"); p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_rm)
    p = sub.add_parser("mv"); p.add_argument("src"); p.add_argument("dst"); p.set_defaults(fn=cmd_mv)
    p = sub.add_parser("mkdir"); p.add_argument("path"); p.set_defaults(fn=cmd_mkdir)
    p = sub.add_parser("df"); p.add_argument("--fresh", action="store_true"); p.add_argument("dev", nargs="?")
    p.set_defaults(fn=cmd_df)
    p = sub.add_parser("selftest"); p.add_argument("dir", nargs="?"); p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser("speed"); p.add_argument("--mb", type=int, default=32); p.set_defaults(fn=cmd_speed)
    sub.add_parser("ctl").set_defaults(fn=cmd_ctl)
    a = ap.parse_args()
    try:
        link = Link()
    except LinkError as e:
        sys.exit(str(e))
    try:
        sys.exit(a.fn(link, a) or 0)
    except XltsError as e:
        sys.exit(f"Failed: {e}")
    except (usb.core.USBError, RuntimeError) as e:
        sys.exit(f"The link failed: {e}")


if __name__ == "__main__":
    main()
