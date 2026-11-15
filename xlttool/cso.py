"""PSP game images: ISO to CSO (smaller, played the same by ARK's ISO drivers) and
CSO/ZSO back to ISO. No Qt here.

CSO v1: a 24-byte header ("CISO", header size, total bytes, block size 2048,
version 1, index alignment), then one 32-bit entry per block plus one for the end:
the block's offset >> alignment, with the top bit set if it is stored as it is.
Each block is raw deflate (zlib without its header), or stored when that is not
smaller. XLT reads exactly this (src/disc.c).

zlib lets go of Python's lock while it compresses, so blocks are compressed on
several threads at once, in order.
"""
from __future__ import annotations

import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

from . import games

BLOCK = 2048
BATCH = 512          # blocks per piece of work for a thread


def _deflate(data: bytes, level: int) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def _batch(data: bytes, level: int) -> list[bytes]:
    return [_deflate(data[i:i + BLOCK], level) for i in range(0, len(data), BLOCK)]


def iso_to_cso(src: str, dst: str, level: int = 9, progress=None, cancel=None, threads: int = 0) -> str:
    """Compresses an ISO into a CSO at dst (written as dst + ".part", renamed when
    complete). Returns dst."""
    total = os.path.getsize(src)
    if total % BLOCK:
        raise ValueError("not a disc image (its size isn't a whole number of 2048-byte sectors)")
    blocks = total // BLOCK
    # the index holds offsets >> align in 31 bits: past 2 GB of output it needs an alignment
    align = 0 if total < (1 << 31) - (1 << 24) else 1
    header = b"CISO" + struct.pack("<IQIBB2x", 0x18, total, BLOCK, 1, align)
    index = [0] * (blocks + 1)
    tmp = dst + ".part"
    threads = threads or max(1, min(8, (os.cpu_count() or 2)))
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo, ThreadPoolExecutor(threads) as pool:
            fo.write(header)
            fo.write(b"\0" * 4 * (blocks + 1))
            pos = fo.tell()
            pending, done, n = [], 0, 0

            def drain(limit: int):
                nonlocal pos, done, n
                while len(pending) > limit:
                    raw, fut = pending.pop(0)
                    for k, comp in enumerate(fut.result()):
                        plain = raw[k * BLOCK:(k + 1) * BLOCK]
                        if pos & ((1 << align) - 1):  # each block starts on the alignment
                            fo.write(b"\0")
                            pos += 1
                        stored = len(comp) >= BLOCK
                        index[n] = (pos >> align) | (0x80000000 if stored else 0)
                        out = plain if stored else comp
                        fo.write(out)
                        pos += len(out)
                        n += 1
                    done += len(raw)
                    if progress:
                        progress(done / total)
            while True:
                if cancel and cancel():
                    raise InterruptedError
                raw = fi.read(BLOCK * BATCH)
                if not raw:
                    break
                pending.append((raw, pool.submit(_batch, raw, level)))
                drain(threads * 2)
            drain(0)
            if pos & ((1 << align) - 1):
                fo.write(b"\0")
                pos += 1
            index[n] = pos >> align
            fo.seek(len(header))
            fo.write(struct.pack(f"<{blocks + 1}I", *index))
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def cso_to_iso(src: str, dst: str, progress=None, cancel=None) -> str:
    """A CSO (v1 or v2) or ZSO back to a plain ISO at dst. Returns dst."""
    tmp = dst + ".part"
    try:
        with games.Disc(src) as d, open(tmp, "wb") as fo:
            if d.format == "iso":
                raise ValueError("this is a plain ISO already")
            step = d.block_size * 256
            at = 0
            while at < d.total:
                if cancel and cancel():
                    raise InterruptedError
                n = min(step, d.total - at)
                fo.write(d.read(at, n))
                at += n
                if progress:
                    progress(at / d.total)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst
