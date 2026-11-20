# Copied from XLT (tools/xltcast.py): the PC side of XLT Cast. Keep the two the same.
"""XLT Cast, PC side (until XLT Tool has its Cast window): the game on the PSP, on the PC.

On the PSP: XLT > Settings > XLT > XLT Cast (On), which installs plugin/xltcast and turns it
on in PLUGINS.TXT; then start a game (homebrew too), connect the cable and press L+R+SELECT:
casting is off until then. L+R+SELECT again turns it off; so does HOME, so that
quitting the game is safe (USB still on when a game quits hangs the PSP).

    xltcast.py test [--ways W,...] [--seconds N] [--prio P] [--every K] [--out DIR]
        the game's frame rate without casting, then cast for N seconds each way, with the
        frame rate on the PC and the game's own frame rate meanwhile; one frame of each
        way saved as a PNG in DIR. Ways (default uncached,cached):
          cached     copied to RAM by the CPU through its cache: the way to use
          uncached   the same through the uncached address (4-5x slower)
          direct     the USB controller reads the frame buffer itself
                     (refused by the PSP's bus driver: 80243002)
        (DMA copy and GE copy were removed: both froze the PSP.)
    xltcast.py frames [--count N] [--out DIR]
        N frames in a row (default 90) as PNGs, with each one's pixel format and row length
        (for pictures that come out wrong now and then)
    xltcast.py record [--seconds N] [--out FILE.wav]
        the game's sound for N seconds (default 10) into a WAV file, with what arrived
    xltcast.py view [--way W] [--scale S] [--smooth]
        a window with the picture (needs PySide6 as well as pyusb)

Needs pyusb and, on Linux, the same access as XLT sync (tools/60-xltsync.rules).
The protocol is plugin/xltcast/xltcast.h: keep the two in step. Logs on the PSP:
SEPLUGINS/xltcast.log.
"""
from __future__ import annotations

import argparse
import glob
import os
import signal
import struct
import sys
import time
import zlib

try:
    import usb.core
    import usb.util
except ImportError:  # a venv with PySide6 but not pyusb: the system's pyusb, if the same Python
    v = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for d in glob.glob(f"/usr/lib/{v}/site-packages") + glob.glob(f"/usr/lib/python3/dist-packages"):
        if os.path.isdir(os.path.join(d, "usb")):
            sys.path.append(d)
            break
    try:
        import usb.core
        import usb.util
    except ImportError:
        usb = None

VID, PID = 0x054C, 0x01CB
SUBCLASS, PROTOCOL = 0x58, 0x43
MAGIC = 0x43544C58
VERSION = 3
REPLY = 0x8000
H, PIECE_ROWS = 272, 64
HELLO, START, STOP, STATS, FRAME, AUDIO = 1, 2, 3, 4, 0x10, 0x11
DIRECT, COPY = 1, 2
# way -> (mode, copy method)
WAYS = {"direct": (DIRECT, 0), "auto": (COPY, 0), "uncached": (COPY, 1), "cached": (COPY, 2)}
METHOD_NAMES = {0: "auto", 1: "uncached", 2: "cached"}
STATUS = {0: "ok", 1: "unknown command", 2: "unknown mode", 3: "no memory for copy mode"}
FMT_NAMES = {0: "565", 1: "5551", 2: "4444", 3: "8888"}

HDR = struct.Struct("<IHHIIIIII")
STATS_S = struct.Struct("<18I")
STATS_FIELDS = ("version flips vblanks sent failed send_us grab_us fmt stride mode speed copy_buf prio last_err "
                "method dma_err audio_hooks audio_frames").split()
STATS_GE = struct.Struct("<3I")  # newer plugins add these after the 18
STATS_GE_FIELDS = ("ge_waits", "ge_wait_us", "ge_timeouts")
TIMEOUT = 3000
VBLANK_HZ = 59.94


class CastError(Exception):
    pass


class Frame:
    def __init__(self, fmt: int, stride: int, number: int, prev_us: int, data: bytes):
        self.fmt, self.stride, self.number, self.prev_us, self.data = fmt, stride, number, prev_us, data

    @property
    def bpp(self) -> int:
        return 4 if self.fmt == 3 else 2

    def rgb(self) -> bytes:
        """480x272 RGB, 3 bytes a pixel (slow: for one still, not a stream)."""
        out = bytearray(480 * H * 3)
        bpp, row = self.bpp, self.stride * self.bpp
        o = 0
        for y in range(H):
            line = self.data[y * row:y * row + 480 * bpp]
            if bpp == 4:
                for x in range(480):
                    out[o:o + 3] = line[x * 4:x * 4 + 3]
                    o += 3
                continue
            for (c,) in struct.iter_unpack("<H", line):
                if self.fmt == 0:
                    r, g, b = c & 31, c >> 5 & 63, c >> 11
                    out[o], out[o + 1], out[o + 2] = r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2
                elif self.fmt == 1:
                    r, g, b = c & 31, c >> 5 & 31, c >> 10 & 31
                    out[o], out[o + 1], out[o + 2] = r << 3 | r >> 2, g << 3 | g >> 2, b << 3 | b >> 2
                else:
                    out[o], out[o + 1], out[o + 2] = (c & 15) * 17, (c >> 4 & 15) * 17, (c >> 8 & 15) * 17
                o += 3
        return bytes(out)

    def blank(self) -> bool:
        return not any(self.data[i] for i in range(0, len(self.data), 97))

    def save_png(self, path: str):
        rgb = self.rgb()
        raw = b"".join(b"\0" + rgb[y * 1440:(y + 1) * 1440] for y in range(H))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 480, H, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class Cast:
    """One XLT Cast device. backend: a libusb backend (XLT Tool's bundled one), or None."""

    def __init__(self, backend=None):
        if usb is None:
            raise CastError("pyusb is missing (Arch: python-pyusb, or pip install pyusb)")
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            raise CastError("No PSP (054c:01cb). In the game, press L+R+SELECT to turn casting on "
                            "(and xltcast.prx on in PLUGINS.TXT, the cable in). See SEPLUGINS/xltcast.log")
        try:  # from the descriptor the system keeps, without opening it (sync may be about to)
            sync = not any(i.bInterfaceSubClass == SUBCLASS and i.bInterfaceProtocol == PROTOCOL for i in dev[0])
        except (usb.core.USBError, IndexError):
            sync = False
        if sync:
            raise CastError("The PSP is there, but as XLT sync, not XLT Cast: start a game, then L+R+SELECT")
        self.dev = dev
        try:
            self._setup(dev)
        except BaseException:
            # let it go now: on Windows only one program may have it open, and Python collects late
            usb.util.dispose_resources(dev)
            raise

    def _setup(self, dev):
        try:
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError:
                cfg = None
            if cfg is None or cfg.bConfigurationValue != 1:
                dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno == 13:
                raise CastError("Permission denied: install tools/60-xltsync.rules, then replug the PSP")
            raise
        cfg = dev.get_active_configuration()
        intf = usb.util.find_descriptor(cfg, bInterfaceClass=0xFF, bInterfaceSubClass=SUBCLASS)
        self.intf = intf
        if intf is None or intf.bInterfaceProtocol != PROTOCOL:
            raise CastError("The PSP is there, but as XLT sync, not XLT Cast: start a game, then L+R+SELECT")
        try:
            usb.util.claim_interface(dev, intf)
        except usb.core.USBError as e:
            if e.errno == 16:
                raise CastError("Another program has the PSP's link (an xltcast view window?): close it first")
            raise
        is_in = lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
        self.dev = dev
        self.ep_in = usb.util.find_descriptor(intf, custom_match=is_in)
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: not is_in(e))
        self.seq = 0
        self.drain()
        status, self.hello = self.command(HELLO)
        if self.hello is None or self.hello["version"] != VERSION:
            got = self.hello["version"] if self.hello else "1 or older"
            raise CastError(f"The plugin on the PSP is version {got}, this tool {VERSION}: copy the new xltcast.prx")

    def close(self):
        """Lets the device go (another program, or the next Cast, may open it)."""
        try:
            usb.util.release_interface(self.dev, self.intf)
        except Exception:  # noqa: BLE001 - gone already
            pass
        usb.util.dispose_resources(self.dev)

    def drain(self):
        try:
            while self.ep_in.read(512 * 256, 300):
                pass
        except usb.core.USBError:
            pass

    def send(self, op: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0):
        self.seq += 1
        self.ep_out.write(HDR.pack(MAGIC, op, 0, self.seq, 0, a, b, c, d), TIMEOUT)
        return self.seq

    def read_message(self, timeout: int = TIMEOUT):
        """("frame", Frame), ("audio", (rate, first sample, 16-bit stereo bytes))
        or ("reply", (op, status, seq, payload))."""
        raw = bytes(self.ep_in.read(512, timeout))
        if len(raw) != HDR.size:
            raise CastError(f"a message of {len(raw)} bytes, not a header")
        magic, op, status, seq, n, a, b, c, d = HDR.unpack(raw)
        if magic != MAGIC:
            raise CastError(f"bad magic {magic:08x}")
        if op == FRAME:
            row = b * (4 if a == 3 else 2)
            parts = []
            for y in range(0, H, PIECE_ROWS):
                size = min(PIECE_ROWS, H - y) * row
                got = bytes(self.ep_in.read(size, timeout))
                if len(got) != size:
                    raise CastError(f"a piece of {len(got)} bytes, not {size}")
                parts.append(got)
            return "frame", Frame(a, b, c, d, b"".join(parts))
        if op == AUDIO:
            data = bytes(self.ep_in.read(n, timeout)) if n else b""
            return "audio", (a, b, data)
        payload = bytes(self.ep_in.read(max(512, n), timeout)) if n else b""
        return "reply", (op & ~REPLY, status, seq, payload)

    def wait_reply(self, seq: int, on_frame=None, timeout: int = TIMEOUT):
        while True:
            kind, m = self.read_message(timeout)
            if kind == "frame":
                if on_frame:
                    on_frame(m)
                continue
            if kind == "audio":  # sound sent meanwhile (the caller wasn't listening for it)
                continue
            op, status, s, payload = m
            if s == seq:
                return status, payload

    def command(self, op: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0, on_frame=None):
        status, payload = self.wait_reply(self.send(op, a, b, c, d), on_frame)
        stats = dict(zip(STATS_FIELDS, STATS_S.unpack_from(payload))) if len(payload) >= STATS_S.size else None
        if stats is not None:
            ge = STATS_GE.unpack_from(payload, STATS_S.size) if len(payload) >= STATS_S.size + STATS_GE.size else (0, 0, 0)
            stats.update(zip(STATS_GE_FIELDS, ge))
        return status, stats


def rate(a: dict, b: dict, key: str) -> float:
    secs = (b["vblanks"] - a["vblanks"]) / VBLANK_HZ
    return (b[key] - a[key]) / secs if secs > 0 else 0.0


def cmd_test(args):
    cast = Cast()
    h = cast.hello
    print(f"PSP: {'high' if h['speed'] == 2 else 'full'} speed USB, game in {FMT_NAMES.get(h['fmt'], h['fmt'])} colour, "
          f"frame buffer {h['stride']} wide")
    rows = []

    _, s0 = cast.command(STATS)
    t_base = time.monotonic()
    time.sleep(args.seconds)
    _, s1 = cast.command(STATS)
    base = rate(s0, s1, "flips")
    seen_vb = (s1["vblanks"] - s0["vblanks"]) / (time.monotonic() - t_base)
    rows.append(("not casting", "-", f"{base:.1f}", "-", "-",
                 f"watcher saw {seen_vb:.0f} of 60 vblanks a second" if seen_vb < 55 else "-"))

    ways = [w.strip() for w in args.ways.split(",") if w.strip()]
    for w in ways:
        if w not in WAYS:
            raise CastError(f"unknown way {w!r} (ways: {', '.join(WAYS)})")
    for name in ways:
        _, a = cast.command(STATS)
        status, _ = cast.command(START, WAYS[name][0], args.prio, args.every, WAYS[name][1])
        if status:
            rows.append((name, STATUS.get(status, status), "", "", "", ""))
            continue
        got = {"n": 0, "bytes": 0, "first": None, "blank": 0}
        t0 = time.monotonic()

        def on_frame(f: Frame):
            got["n"] += 1
            got["bytes"] += len(f.data)
            if got["first"] is None and got["n"] > 3:
                got["first"] = f
            if f.blank():
                got["blank"] += 1

        err = None
        try:
            while time.monotonic() - t0 < args.seconds:
                kind, m = cast.read_message()
                if kind == "frame":
                    on_frame(m)
            _, b = cast.command(STATS, on_frame=on_frame)
            dt = time.monotonic() - t0
            cast.command(STOP, on_frame=on_frame)
        except (usb.core.USBError, CastError) as e:
            # the PSP gives up on a stuck transfer after 3 s, then waits for the PC again
            err = "timed out" if isinstance(e, usb.core.USBTimeoutError) else str(e)
            dt = time.monotonic() - t0
            b = None
            for _ in range(3):
                time.sleep(1.5)
                cast.drain()
                try:
                    _, b = cast.command(STATS)
                    break
                except (usb.core.USBError, CastError):
                    pass
            if b is None:
                rows.append((name, "", "", "", "", f"stopped: {err}; the PSP no longer answers"))
                break
        sent = max(1, b["sent"] - a["sent"])
        avg_ms = (b["send_us"] - a["send_us"]) / sent / 1000
        grab_ms = (b["grab_us"] - a["grab_us"]) / sent / 1000
        fail = b["failed"] - a["failed"]
        note = []
        if err:
            note.append(f"stopped: {err}")
        if fail:
            note.append(f"{fail} failed (code {b['last_err']:08x})")
        if WAYS[name][0] == COPY and METHOD_NAMES.get(b["method"]) != name and got["n"]:
            note.append(f"copied {METHOD_NAMES.get(b['method'], b['method'])}")
        gw = b["ge_waits"] - a["ge_waits"]
        if gw:
            note.append(f"{gw} copies waited for the GE ({(b['ge_wait_us'] - a['ge_wait_us']) / gw / 1000:.1f} ms each), "
                        f"{b['ge_timeouts'] - a['ge_timeouts']} still drawing after the wait")
        if b["dma_err"] - a["dma_err"]:
            note.append(f"{b['dma_err'] - a['dma_err']} frames dropped (the game drew over them while copied)")
        if got["blank"]:
            note.append(f"{got['blank']} of {got['n']} frames all black")
        if got["first"] is not None:
            os.makedirs(args.out, exist_ok=True)
            path = os.path.join(args.out, f"xltcast-{name}.png")
            got["first"].save_png(path)
            note.append(f"frame saved: {path}")
        vb = (b["vblanks"] - a["vblanks"]) / dt
        if vb < 55:  # the watcher should see all 60 a second: fewer, and it isn't getting the CPU
            note.append(f"watcher saw {vb:.0f} of 60 vblanks a second")
        rows.append((name, f"{got['n'] / dt:.1f}", f"{rate(a, b, 'flips'):.1f}", f"{got['bytes'] / dt / 1e6:.1f}",
                     f"{avg_ms:.1f}" + (f" ({grab_ms:.1f} copying)" if WAYS[name][0] == COPY else ""), "; ".join(note)))

    print()
    head = ("way", "fps on PC", "game fps", "MB/s", "ms a frame", "notes")
    widths = [max(len(str(r[i])) for r in rows + [head]) for i in range(5)]
    for r in [head] + rows:
        print("  ".join(str(r[i]).ljust(widths[i]) for i in range(5)) + "  " + r[5])
    print(f"\n(the game's fps counts its frame buffer changes; thread priority {s1['prio']:#x}"
          f"{f', every {args.every}. frame' if args.every > 1 else ''})")


def cmd_frames(args):
    cast = Cast()
    os.makedirs(args.out, exist_ok=True)
    status, _ = cast.command(START, COPY, args.prio, args.every, 0)
    if status:
        raise CastError(f"start: {STATUS.get(status, status)}")
    got, lines = [], []
    try:
        while len(got) < args.count:
            try:
                kind, m = cast.read_message(1000)
            except usb.core.USBTimeoutError:
                continue
            if kind == "frame":
                got.append(m)
    finally:
        try:
            cast.command(STOP, on_frame=lambda f: None)
        except (usb.core.USBError, CastError):
            pass
    for i, f in enumerate(got):  # saved after: the PNGs take longer than the frames
        name = f"frame{i:03d}.png"
        f.save_png(os.path.join(args.out, name))
        lines.append(f"{name}  number {f.number}  format {FMT_NAMES.get(f.fmt, f.fmt)}  row {f.stride} px  {len(f.data)} bytes")
    with open(os.path.join(args.out, "frames.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    fmts = sorted({(FMT_NAMES.get(f.fmt, f.fmt), f.stride) for f in got})
    print(f"{len(got)} frames in {args.out} (formats and rows seen: {fmts}); list in frames.txt")


def write_wav(path: str, pcm: bytes, rate: int = 44100):
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)


def cmd_record(args):
    cast = Cast()
    print(f"PSP: sound calls hooked {cast.hello['audio_hooks']} of 15")
    status, _ = cast.command(START, COPY, args.prio, args.every, 0)
    if status:
        raise CastError(f"start: {STATUS.get(status, status)}")
    pcm, frames, gaps, last, t0 = bytearray(), 0, 0, None, time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            try:
                kind, m = cast.read_message(1000)
            except usb.core.USBTimeoutError:
                continue
            if kind == "frame":
                frames += 1
            elif kind == "audio":
                rate, first, data = m
                if last is not None and first != last:
                    gaps += 1
                    if first > last and first - last < rate:  # a hole: silence keeps the timing
                        pcm += bytes((first - last) * 4)
                last = first + len(data) // 4
                pcm += data
    finally:
        try:
            cast.command(STOP, on_frame=lambda f: None)
        except (usb.core.USBError, CastError):
            pass
    dt = time.monotonic() - t0
    write_wav(args.out, bytes(pcm))
    secs = len(pcm) / 4 / 44100
    peak = max((abs(v) for (v,) in struct.iter_unpack("<h", bytes(pcm[: 44100 * 4 * 10]))), default=0) if pcm else 0
    print(f"{secs:.1f} s of sound in {dt:.1f} s ({frames / dt:.1f} fps of picture meanwhile), "
          f"{gaps} gaps, loudest sample {peak} of 32767: {args.out}")


def cmd_view(args):
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        raise CastError("view needs PySide6 (try ~/.venvs/xlttool/bin/python)")
    cast = Cast()
    status, _ = cast.command(START, WAYS[args.way][0], args.prio, args.every, WAYS[args.way][1])
    if status:
        raise CastError(f"start: {STATUS.get(status, status)}")
    qfmt = {0: QtGui.QImage.Format.Format_RGB16, 1: QtGui.QImage.Format.Format_RGB555,
            3: QtGui.QImage.Format.Format_RGBX8888}

    class Reader(QtCore.QThread):
        frame = QtCore.Signal(object)
        failed = QtCore.Signal(str)

        def run(self):
            try:
                while not self.isInterruptionRequested():
                    kind, m = cast.read_message()
                    if kind == "frame":
                        self.frame.emit(m)
            except Exception as e:  # shown, then the window stays
                self.failed.emit(str(e))

    app = QtWidgets.QApplication(sys.argv[:1])
    label = QtWidgets.QLabel()
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("background: black")
    label.setMinimumSize(480, 272)
    label.resize(480 * args.scale, 272 * args.scale)
    label.setWindowTitle("XLT Cast")
    times: list[float] = []
    mode = (QtCore.Qt.TransformationMode.SmoothTransformation if args.smooth
            else QtCore.Qt.TransformationMode.FastTransformation)

    def show(f: Frame):
        if f.fmt in qfmt:
            img = QtGui.QImage(f.data, 480, H, f.stride * f.bpp, qfmt[f.fmt])
            if f.fmt != 3:
                img = img.rgbSwapped()  # the PSP keeps red in the low bits
        else:  # 4444: rare
            img = QtGui.QImage(f.rgb(), 480, H, 1440, QtGui.QImage.Format.Format_RGB888)
        pix = QtGui.QPixmap.fromImage(img).scaled(label.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, mode)
        label.setPixmap(pix)
        now = time.monotonic()
        times.append(now)
        while times and now - times[0] > 1:
            times.pop(0)
        label.setWindowTitle(f"XLT Cast · {len(times)} fps · {args.way} · {FMT_NAMES.get(f.fmt, f.fmt)}")

    reader = Reader()
    reader.frame.connect(show)
    reader.failed.connect(lambda e: label.setWindowTitle(f"XLT Cast · stopped: {e}"))
    reader.start()
    label.show()
    # Ctrl+C in the terminal closes the window like its close button (Qt's loop never gives
    # Python a turn to see the signal by itself, so a timer does)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QtCore.QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(200)
    app.exec()
    reader.requestInterruption()
    reader.wait(4000)
    try:
        cast.send(STOP)
    except usb.core.USBError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test", help="measure: the game's fps without and with casting, fps on the PC")
    t.add_argument("--seconds", type=float, default=10)
    t.add_argument("--ways", default="uncached,cached", help="ways to try, in order (default uncached,cached)")
    t.add_argument("--out", default=".", help="folder for the saved frames")
    fr = sub.add_parser("frames", help="frames in a row as PNGs, with their formats")
    fr.add_argument("--count", type=int, default=90)
    fr.add_argument("--out", default="xltcast-frames")
    r = sub.add_parser("record", help="the game's sound into a WAV file")
    r.add_argument("--seconds", type=float, default=10)
    r.add_argument("--out", default="xltcast.wav")
    v = sub.add_parser("view", help="a window with the picture")
    v.add_argument("--way", choices=[w for w in WAYS if w != "direct"], default="auto")
    v.add_argument("--scale", type=int, default=2)
    v.add_argument("--smooth", action="store_true", help="smooth scaling (default sharp pixels)")
    for p in (t, fr, r, v):
        p.add_argument("--prio", type=lambda s: int(s, 0), default=0,
                       help="the PSP's sending thread priority, 0x08-0x7F (lower is more urgent; default 0x30)")
        p.add_argument("--every", type=int, default=1, help="send every K-th frame")
    args = ap.parse_args()
    try:
        {"test": cmd_test, "frames": cmd_frames, "record": cmd_record, "view": cmd_view}[args.cmd](args)
    except CastError as e:
        sys.exit(f"xltcast: {e}")


if __name__ == "__main__":
    main()
