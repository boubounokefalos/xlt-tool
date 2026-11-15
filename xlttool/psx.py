"""PS1 games (BIN/CUE) to the EBOOT.PBP the PSP's PS1 emulator (POPS) runs. No Qt.

The EBOOT holds PARAM.SFO, the icons, DATA.PSP (a small program Sony made, the
same in every PS1 EBOOT) and DATA.PSAR: the disc as PSISOIMG0000, then STARTDAT
(the start screen). XLT Tool doesn't carry Sony's parts: DATA.PSP and STARTDAT
are copied once from a PS1 EBOOT the user already has (take_parts), and kept in
the data folder.

PSISOIMG0000, single disc (as popstation and pop-fe write it; psdevwiki "Eboot.PBP"):
  0x0000  "PSISOIMG0000", u32 length of the PSISO part
  0x0400  the disc ID as "_SLUS_00594"
  0x0800  the disc's TOC; u32 0x100000 (where the disc starts) at 0x0BFC
  0x0C00  the CD audio table (empty: no CD music here)
  0x1220  u32 length + 0x2D31, 0xFF 0x07 at +8, the title at +12
  0x4000  the index: 32 bytes per 16 sectors (u32 offset from 0x100000, u16 size,
          SHA-1 of the plain sectors, first 16 bytes, at +8)
  0x100000 the disc, 16 raw 2352-byte sectors at a time, each raw deflate (or as it is)
CD music: the image holds the data track only; the TOC lists every track where it is on
the disc, and each audio track, turned into ATRAC3 by atracdenc (LGPL; XLT Tool runs it
as a program), goes after the disc, listed in the table at 0x0C00 (16 bytes a track:
u32 offset from 0x100000, u32 length). This is what pop-fe writes for the PSP.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import games

SECTOR = 2352
CHUNK = 16 * SECTOR          # 0x9300
DISC_START = 0x100000
PSAR_ALIGN = 0x10000

_ID_RE = re.compile(rb"BOOT\s*=\s*cdrom0?:\\?\\?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})", re.I)


# --------------------------------------------------------------- the disc --

def parse_cue(cue: str) -> list[dict]:
    """The tracks of a cue sheet: [{"file", "number", "mode", "indexes": {n: frame in its file}}],
    files as full paths."""
    tracks, current = [], None
    base = os.path.dirname(cue)
    with open(cue, encoding="utf-8", errors="replace") as f:
        for line in f:
            words = line.strip().split()
            if not words:
                continue
            key = words[0].upper()
            if key == "FILE":
                m = re.match(r'\s*FILE\s+"?(.*?)"?\s+\w+\s*$', line, re.I)
                current = os.path.join(base, m.group(1) if m else words[1])
            elif key == "TRACK" and len(words) >= 3:
                tracks.append({"file": current, "number": int(words[1]), "mode": words[2].upper(), "indexes": {}})
            elif key == "INDEX" and len(words) >= 3 and tracks:
                mm, ss, ff = (int(x) for x in words[2].split(":"))
                tracks[-1]["indexes"][int(words[1])] = (mm * 60 + ss) * 75 + ff
    return tracks


def disc_layout(cue: str) -> list[dict]:
    """parse_cue with every track placed on the whole disc (files one after the other):
    "start" (the last index, where it plays from), "first" (its first index) and "stop"
    (its last sector), all absolute; "offset" = where its file starts."""
    tracks = parse_cue(cue)
    sizes, at, offsets = {}, 0, {}
    for t in tracks:
        f = t["file"]
        if f not in offsets:
            offsets[f] = at
            sizes[f] = os.path.getsize(f) // SECTOR
            at += sizes[f]
    for i, t in enumerate(tracks):
        off = offsets[t["file"]]
        idx = t["indexes"] or {1: 0}
        t["offset"] = off
        t["first"] = off + min(idx.values())
        t["start"] = off + idx[max(idx)]
        nxt = tracks[i + 1] if i + 1 < len(tracks) else None
        if nxt and nxt["file"] == t["file"]:
            t["stop"] = off + min((nxt["indexes"] or {1: 0}).values()) - 1
        else:
            t["stop"] = off + sizes[t["file"]] - 1
    return tracks


def cue_toc(tracks: list[dict]) -> bytes:
    """The TOC POPS reads, as pop-fe makes it from a cue sheet (disc_layout's tracks)."""
    def bcd(i: int) -> int:
        return i % 10 + 16 * (i // 10 % 10)

    def msf(frames: int):
        return frames // 4500, frames // 75 % 60, frames % 75
    toc = bytearray([0x41, 0, 0xA0, 0, 0, 0, 0, 0x01, 0x20, 0,
                     0x01, 0, 0xA1, 0, 0, 0, 0, 0, 0, 0,
                     0x01, 0, 0xA2, 0, 0, 0, 0, 0, 0, 0])
    toc[17] = bcd(len(tracks))
    m, s_, f = msf(max(t["stop"] for t in tracks) + 2)  # the lead-out: the next frame after the end
    toc[27:30] = bytes([bcd(m), bcd(s_), bcd(f)])
    for t in tracks:
        n = t["number"]
        if n == 1:
            toc += bytes([0x41, 0, bcd(1), 0, 2, 1, 0, 0, 2, 0])
            continue
        m, s_, f = msf(t["start"])
        m2, s2 = (m + (s_ + 2) // 60, (s_ + 2) % 60)
        toc += bytes([0x01, 0, bcd(n), bcd(m), bcd(s_), bcd(f), 0, bcd(m2), bcd(s2), bcd(f)])
    return bytes(toc)


def data_track(path: str) -> str:
    """The data track's file for a .cue, or the image itself (.bin/.img)."""
    if path.lower().endswith(".cue"):
        tracks = parse_cue(path)
        if not tracks or tracks[0]["mode"] == "AUDIO":
            raise ValueError("no data track in this cue sheet")
        for t in tracks:
            if not t["file"] or not os.path.exists(t["file"]):
                raise ValueError(f"the cue sheet's image isn't there: {t['file']}")
        return tracks[0]["file"]
    return path


def data_size(path: str) -> int:
    """Bytes of the data track (a one-file cue with music has it at the start)."""
    if path.lower().endswith(".cue"):
        t = disc_layout(path)[0]
        return (t["stop"] - t["offset"] + 1) * SECTOR
    return os.path.getsize(path)


# --------------------------------------------------------------- CD music --

def atracdenc() -> str:
    """The ATRAC3 encoder: $XLTTOOL_ATRACDENC, the one in the AppImage (beside ffmpeg), or
    one on PATH."""
    env = os.environ.get("XLTTOOL_ATRACDENC")
    if env and os.access(env, os.X_OK):
        return env
    from . import media  # the AppImage keeps it beside its ffmpeg (usr/bin)
    ff = media.ffmpeg_path()
    for here in ([os.path.join(os.path.dirname(ff), "atracdenc")] if ff else []) + \
            [os.path.join(os.path.dirname(sys.executable), "atracdenc")]:
        if os.access(here, os.X_OK):
            return here
    return shutil.which("atracdenc") or ""


def _wav(pcm: bytes) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)


def encode_music(cue: str, progress=None, cancel=None) -> list[bytes]:
    """Each audio track of a cue, from where it plays to its end, as ATRAC3 (atracdenc's
    output without its 0x60-byte header). [] if there are none."""
    tracks = [t for t in disc_layout(cue) if t["mode"] == "AUDIO"]
    if not tracks:
        return []
    exe = atracdenc()
    if not exe:
        raise RuntimeError("atracdenc (the ATRAC3 encoder) isn't there")
    out = [b""] * len(tracks)
    done = [0]
    with tempfile.TemporaryDirectory(prefix="xlt-tool-cdda-") as tmp:
        def one(i: int):
            if cancel and cancel():
                raise InterruptedError
            t = tracks[i]
            with open(t["file"], "rb") as f:
                f.seek((t["start"] - t["offset"]) * SECTOR)
                pcm = f.read((t["stop"] - t["start"] + 1) * SECTOR)
            wav, aea = os.path.join(tmp, f"{i}.wav"), os.path.join(tmp, f"{i}.aea")
            with open(wav, "wb") as w:
                w.write(_wav(pcm))
            subprocess.run([exe, "--encode=atrac3", "-i", wav, "-o", aea], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(wav)
            with open(aea, "rb") as a:
                out[i] = a.read()[0x60:]
            done[0] += 1
            if progress:
                progress(done[0] / len(tracks))
        with ThreadPoolExecutor(max(1, min(8, os.cpu_count() or 2))) as pool:
            list(pool.map(one, range(len(tracks))))
    return out


def audio_tracks(path: str) -> int:
    return sum(1 for t in parse_cue(path) if t["mode"] == "AUDIO") if path.lower().endswith(".cue") else 0


def read_sector_data(f, lba: int) -> bytes:
    """2048 bytes of user data from a raw 2352-byte sector (mode 2 form 1, or mode 1)."""
    f.seek(lba * SECTOR)
    raw = f.read(SECTOR)
    if len(raw) < SECTOR:
        return b""
    return raw[24:24 + 2048] if raw[15] == 2 else raw[16:16 + 2048]


def disc_id(image: str) -> str:
    """The game's ID from SYSTEM.CNF on the disc (SCES00344), or ""."""
    try:
        with open(image, "rb") as f:
            pvd = read_sector_data(f, 16)
            if pvd[1:6] != b"CD001":
                return ""
            root_lba, root_size = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
            for s in range(max(1, min(root_size, 16 * 2048) // 2048)):
                sec = read_sector_data(f, root_lba + s)
                p = 0
                while p + 33 < len(sec) and sec[p]:
                    ln, nl = sec[p], sec[p + 32]
                    name = sec[p + 33:p + 33 + nl].split(b";")[0].upper()
                    if name == b"SYSTEM.CNF":
                        lba, size = struct.unpack_from("<I", sec, p + 2)[0], struct.unpack_from("<I", sec, p + 10)[0]
                        text = read_sector_data(f, lba)[:min(size, 2048)]
                        m = _ID_RE.search(text)
                        return (m.group(1).decode().upper() + m.group(2).decode() + m.group(3).decode()) if m else ""
                    p += ln
    except OSError:
        pass
    return ""


def clean_title(name: str) -> str:
    """A title from a file name: '(Europe)', '[!]' and the like dropped."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name).strip(" -_.")
    return t or name


# ------------------------------------------------------- the user's parts --

def parts_dir() -> str:
    return os.path.join(games.data_dir(), "ps1")


def have_parts() -> bool:
    d = parts_dir()
    return os.path.isfile(os.path.join(d, "DATA.PSP")) and os.path.isfile(os.path.join(d, "STARTDAT.BIN"))


def take_parts(src) -> None:
    """Copies DATA.PSP and STARTDAT from a PS1 EBOOT (a path, or a games.RemoteFile
    for one on the PSP) into parts_dir(). Raises ValueError if it isn't one."""
    f = games.LocalFile(src) if isinstance(src, str) else src
    try:
        h = f.pread(0, 40)
        if len(h) < 40 or h[:4] != b"\0PBP":
            raise ValueError("not an EBOOT.PBP")
        offs = struct.unpack_from("<8I", h, 8)
        sfo = games.parse_sfo(f.pread(offs[0], min(offs[1] - offs[0], games.SFO_MAX)))
        if sfo.get("CATEGORY") != "ME":
            raise ValueError("not a PS1 game's EBOOT")
        data_psp = f.pread(offs[6], offs[7] - offs[6])
        if data_psp[:4] not in (b"~PSP", b"\x7fELF"):
            raise ValueError("its DATA.PSP isn't a PSP program")
        psar = offs[7]
        head = f.pread(psar, 0x400)
        pstitle = b""
        if head[:12] == b"PSISOIMG0000":
            start = psar + struct.unpack_from("<I", head, 12)[0]
        elif head[:10] == b"PSTITLEIMG":
            start = psar + struct.unpack_from("<I", head, 0x10)[0]
            pstitle = head  # the multi-disc header (it holds data of Sony's too): for multi-disc EBOOTs
        else:
            raise ValueError("its disc part isn't one XLT Tool knows")
        start = (start + 15) & ~15
        startdat = f.pread(start, min(f.size - start, 4 << 20))
        at = startdat.find(b"STARTDAT")
        if at < 0 or at > 0x1000:
            raise ValueError("no STARTDAT in it")
        startdat = startdat[at:]
    finally:
        if isinstance(src, str):
            f.close()
    d = parts_dir()
    os.makedirs(d, exist_ok=True)
    for name, data in (("DATA.PSP", data_psp), ("STARTDAT.BIN", startdat), ("PSTITLE.BIN", pstitle)):
        if not data:
            continue
        tmp = os.path.join(d, name + ".part")
        with open(tmp, "wb") as o:
            o.write(data)
        os.replace(tmp, os.path.join(d, name))


def have_title_part() -> bool:
    """A PSTITLEIMG header for multi-disc EBOOTs (from a donor EBOOT that has one)."""
    return os.path.isfile(os.path.join(parts_dir(), "PSTITLE.BIN"))


def _parts() -> tuple[bytes, bytes]:
    d = parts_dir()
    with open(os.path.join(d, "DATA.PSP"), "rb") as a, open(os.path.join(d, "STARTDAT.BIN"), "rb") as b:
        return a.read(), b.read()


# --------------------------------------------------------------- PARAM.SFO --

def make_sfo(values: dict) -> bytes:
    """A PARAM.SFO: keys in order, strings as UTF-8 (format 0x0204), ints 0x0404."""
    keys, data, entries = b"", b"", []
    for k in sorted(values):
        v = values[k]
        ko = len(keys)
        keys += k.encode() + b"\0"
        if isinstance(v, int):
            raw, fmt, room = struct.pack("<I", v), 0x0404, 4
        else:
            raw = str(v).encode("utf-8") + b"\0"
            fmt = 0x0204
            room = {"TITLE": 128, "LICENSE": 512}.get(k, (len(raw) + 3) // 4 * 4)
            raw = raw[:room]
        entries.append((ko, fmt, len(raw), room, len(data)))
        data += raw.ljust(room, b"\0")
    keys = keys.ljust((len(keys) + 3) // 4 * 4, b"\0")
    kstart = 20 + 16 * len(entries)
    head = b"\0PSF" + struct.pack("<IIII", 0x101, kstart, kstart + len(keys), len(entries))
    return head + b"".join(struct.pack("<HHIII", *e) for e in entries) + keys + data


def _psx_toc(size: int) -> bytes:
    """A TOC for one data track of size bytes (lead-out after it, plus the 2 s pregap)."""
    def bcd(i: int) -> int:
        return i % 10 + 16 * (i // 10 % 10)
    frames = (size + 150 * SECTOR) // SECTOR
    m, s, f = frames // 4500, frames // 75 % 60, frames % 75
    return bytes([0x41, 0, 0xA0, 0, 0, 0, 0, 0x01, 0x20, 0,
                  0x41, 0, 0xA1, 0, 0, 0, 0, 0x01, 0, 0,
                  0x41, 0, 0xA2, 0, 0, 0, 0, bcd(m), bcd(s), bcd(f),
                  0x41, 0, 0x01, 0, 0x02, 0x01, 0, 0, 0x02, 0])  # track 1, at 00:02:00


# ---------------------------------------------------------------- the EBOOT --

@dataclass
class Disc:
    """One disc of a game: its data track (the first size bytes of image), and from its
    cue sheet the TOC and CD music when it has any."""
    image: str
    game_id: str
    size: int = 0
    toc: bytes | None = None
    music: list | None = None


def _game_id(gid: str) -> str:
    g = re.sub(r"[^A-Z0-9]", "", gid.upper())[:9]
    if len(g) != 9:
        raise ValueError(f"a PS1 game ID looks like SLUS00594, not {gid!r}")
    return g


def _write_psiso(fo, disc: Disc, title: str, level: int, progress, cancel) -> int:
    """One disc as PSISOIMG0000 from fo's position. Returns its length."""
    base = fo.tell()
    size = disc.size or os.path.getsize(disc.image)
    if size % SECTOR:
        raise ValueError("not a raw CD image (its size isn't a whole number of 2352-byte sectors)")
    gid = _game_id(disc.game_id)
    n_chunks = (size + CHUNK - 1) // CHUNK
    head = bytearray(0x4000)
    head[0:12] = b"PSISOIMG0000"
    head[0x400:0x400 + 11] = f"_{gid[:4]}_{gid[4:]}".encode()
    toc = disc.toc or _psx_toc(size)
    head[0x800:0x800 + min(len(toc), 0x3FC)] = toc[:0x3FC]
    struct.pack_into("<I", head, 0xBFC, DISC_START)
    head[0x1228:0x122A] = b"\xff\x07"
    t = title.encode("utf-8")[:127]
    head[0x122C:0x122C + len(t)] = t
    fo.write(head)
    index_at = fo.tell()
    fo.write(b"\0" * (32 * n_chunks))
    fo.write(b"\0" * (base + DISC_START - fo.tell()))
    index, offset = bytearray(), 0
    with open(disc.image, "rb") as fi:
        for i in range(n_chunks):
            if cancel and cancel():
                raise InterruptedError
            buf = fi.read(min(CHUNK, max(0, size - i * CHUNK))).ljust(CHUNK, b"\0")  # the data track only
            c = zlib.compressobj(level, zlib.DEFLATED, -15)
            comp = c.compress(buf) + c.flush()
            out = comp if len(comp) < CHUNK else buf
            entry = bytearray(32)
            struct.pack_into("<IH", entry, 0, offset, len(out))
            entry[8:24] = hashlib.sha1(buf).digest()[:16]
            index += entry
            fo.write(out)
            offset += len(out)
            if progress and i % 16 == 0:
                progress(i / n_chunks)
    fo.write(b"\0" * (-fo.tell() & 15))
    table = bytearray()
    for track in (disc.music or [])[:98]:  # CD music after the disc, 16-byte aligned
        table += struct.pack("<IIII", fo.tell() - base - DISC_START, len(track), 0, 0)
        fo.write(track)
        fo.write(b"\0" * (-fo.tell() & 15))
    length = fo.tell() - base
    end = fo.tell()
    fo.seek(index_at)
    fo.write(index)
    fo.seek(base + 12)
    fo.write(struct.pack("<I", length))
    fo.seek(base + 0x1220)
    fo.write(struct.pack("<I", length + 0x2D31))
    if table:
        fo.seek(base + 0xC00)
        fo.write(table)
    fo.seek(end)
    return length


def make_eboot(image, dst: str, title: str, game_id: str = "", icon0: bytes | None = None,
               pic1: bytes | None = None, level: int = 9, progress=None, cancel=None,
               size: int = 0, toc: bytes | None = None, music: list | None = None) -> str:
    """Writes a PS1 EBOOT.PBP at dst (through dst + ".part"). image is a data track's file
    (with game_id, size, toc, music as in Disc), or a list of Disc for a game of several
    discs: one EBOOT for them all (PSTITLEIMG, up to 5 discs). Needs take_parts() done
    once (and, for several discs, from an EBOOT with a PSTITLEIMG header). Returns dst."""
    if not have_parts():
        raise RuntimeError("XLT Tool needs a PS1 EBOOT of yours first (for its DATA.PSP)")
    discs = image if isinstance(image, list) else [Disc(image, game_id, size, toc, music)]
    if not 1 <= len(discs) <= 5:
        raise ValueError("a PS1 EBOOT holds 1 to 5 discs")
    multi = len(discs) > 1
    if multi and not have_title_part():
        raise RuntimeError("for a game of several discs XLT Tool needs a PS1 EBOOT of yours that has "
                           "the multi-disc header (PSTITLEIMG), as pop-fe writes them")
    data_psp, startdat = _parts()
    gid = _game_id(discs[0].game_id)
    sfo = make_sfo({"BOOTABLE": 1, "CATEGORY": "ME", "DISC_ID": gid, "DISC_VERSION": "1.00",
                    "LICENSE": "Copyright(C) Sony Computer Entertainment America Inc.",
                    "PARENTAL_LEVEL": 1, "PSP_SYSTEM_VER": "3.01", "REGION": 0x8000,
                    "TITLE": title[:127]})
    parts = [sfo, icon0 or b"", b"", b"", pic1 or b"", b""]
    offs, at = [], 40
    for p in parts:
        offs.append(at)
        at += len(p)
    offs.append(at)                                   # DATA.PSP
    psar = (at + len(data_psp) + PSAR_ALIGN - 1) // PSAR_ALIGN * PSAR_ALIGN
    offs.append(psar)
    total = sum((d.size or os.path.getsize(d.image)) for d in discs) or 1
    tmp = dst + ".part"
    try:
        with open(tmp, "wb+") as fo:
            fo.write(b"\0PBP" + struct.pack("<I8I", 0x10000, *offs))
            for p in parts:
                fo.write(p)
            fo.write(data_psp)
            fo.write(b"\0" * (psar - fo.tell()))
            if multi:
                with open(os.path.join(parts_dir(), "PSTITLE.BIN"), "rb") as f:
                    pstitle = bytearray(f.read(0x400).ljust(0x400, b"\0"))
                fo.write(b"\0" * 0x400)
            done, starts = 0, []
            for d in discs:
                if multi:  # each disc on a 32 KB boundary
                    fo.write(b"\0" * (-fo.tell() & 0x7FFF))
                starts.append(fo.tell() - psar)
                share = (d.size or os.path.getsize(d.image)) / total
                _write_psiso(fo, d, title, level,
                             (lambda f, base=done, share=share: progress(base + f * share)) if progress else None,
                             cancel)
                done += share
            fo.write(b"\0" * (-fo.tell() & 15))
            startdat_at = fo.tell() - psar
            fo.write(startdat)
            if multi:
                pstitle[0:16] = b"PSTITLEIMG000000"
                struct.pack_into("<I", pstitle, 0x10, startdat_at)
                for k in range(5):
                    struct.pack_into("<I", pstitle, 0x200 + 4 * k, starts[k] if k < len(starts) else 0)
                pstitle[0x264:0x264 + 11] = f"_{gid[:4]}_{gid[4:]}".encode()
                struct.pack_into("<I", pstitle, 0x284, startdat_at + 0x2D31)
                t = title.encode("utf-8")[:0x7F]
                pstitle[0x30C:0x38C] = t.ljust(0x80, b"\0")
                fo.seek(psar)
                fo.write(pstitle)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if progress:
        progress(1.0)
    return dst


def read_disc(eboot: str, n: int = 0) -> bytes:
    """Disc n's image back out of a PS1 EBOOT (for checking)."""
    with open(eboot, "rb") as f:
        h = f.read(40)
        psar = struct.unpack_from("<I", h, 36)[0]
        f.seek(psar)
        head = f.read(0x400)
        if head[:10] == b"PSTITLEIMG":
            psar += struct.unpack_from("<I", head, 0x200 + 4 * n)[0]
        elif n:
            raise ValueError("a single-disc EBOOT")
        f.seek(psar)
        head = f.read(0x4000)
        if head[:12] != b"PSISOIMG0000":
            raise ValueError("not a PS1 EBOOT's disc")
        out = bytearray()
        i = 0
        while True:
            f.seek(psar + 0x4000 + 32 * i)
            e = f.read(32)
            off, size = struct.unpack_from("<IH", e, 0)
            if size == 0:
                break
            f.seek(psar + DISC_START + off)
            data = f.read(size)
            out += data if size == CHUNK else zlib.decompress(data, -15)
            i += 1
        return bytes(out)
