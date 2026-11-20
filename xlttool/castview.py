"""XLT Cast: the game being played on the PSP, live in a window (XLT's plugin/xltcast).

The PSP is a USB device only while a game is being cast (L+R+SELECT in the game, with
Settings > XLT > XLT Cast on in XLT): until then the window waits and looks for it every
second, and when casting stops (HOME, L+R+SELECT, the game quits) it waits again. The
reading runs on its own thread; frames reach the window as Qt signals.
"""
from __future__ import annotations

import array
import ctypes
import ctypes.util
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QToolButton,
                               QVBoxLayout, QWidget)

from . import xltcast

LOOK_EVERY = 1.0  # s between looks for the PSP while there is none
GONE_AFTER = 3.0  # s without a message from the PSP before checking it is still on USB
H = xltcast.H
COLOUR = {0: "16-bit colour", 1: "16-bit colour", 2: "16-bit colour", 3: "32-bit colour"}
QFMT = {0: QImage.Format.Format_RGB16, 1: QImage.Format.Format_RGB555, 3: QImage.Format.Format_RGBX8888}


def frame_image(f: xltcast.Frame) -> QImage:
    """A 480x272 QImage of its own (not pointing into the frame's bytes)."""
    if f.fmt in QFMT:
        img = QImage(f.data, 480, H, f.stride * f.bpp, QFMT[f.fmt])
        img = img.rgbSwapped() if f.fmt != 3 else img.copy()  # the PSP keeps red in the low bits
    else:  # 4444: rare, done the slow way
        img = QImage(f.rgb(), 480, H, 1440, QImage.Format.Format_RGB888).copy()
    return img


def _backend():
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:  # noqa: BLE001 - not bundled: the system's libusb
        return None


class Reader(QThread):
    """Finds the PSP, starts casting, reads frames; after a lost link, looks again."""
    frame = Signal(object)        # QImage
    audio = Signal(bytes)         # 44.1 kHz 16-bit stereo, as it comes
    status = Signal(str, str)     # "waiting" | "casting" | "error", text
    info = Signal(int)            # the game's pixel format (frames as sent)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cast: xltcast.Cast | None = None
        self.every = 1  # 1: every frame; 2: every other one (the window sets it; taken at once)

    def run(self):
        backend = _backend()
        while not self.isInterruptionRequested():
            cast = None
            try:
                cast = xltcast.Cast(backend)
            except xltcast.CastError as e:
                text = str(e)
                if "ermission" in text:
                    self.status.emit("error", "No access to the PSP over USB: set it up as for USB sync "
                                              "(PSP over USB in the destinations)")
                elif "as XLT sync" in text:
                    self.status.emit("waiting", "The PSP is in XLT: start a game, then press L+R+SELECT")
                elif "version" in text:
                    self.status.emit("error", text)
                else:
                    self.status.emit("waiting", "Waiting for the PSP: in a game, press L+R+SELECT")
            except Exception:  # noqa: BLE001 - usb errors while opening: look again
                self.status.emit("waiting", "Waiting for the PSP: in a game, press L+R+SELECT")
            if cast is None:
                # waited out of the except: the error, and the device it points to, are gone by now
                self._sleep(LOOK_EVERY)
                continue
            self._cast = cast
            try:
                every = self.every
                status, _ = cast.command(xltcast.START, xltcast.COPY, 0, every, 0)
                if status:
                    self.status.emit("error", f"The PSP can't cast: {xltcast.STATUS.get(status, status)}")
                    self._sleep(3)
                    continue
                self.status.emit("casting", "")
                fmt = -1
                last_msg = time.monotonic()
                while not self.isInterruptionRequested():
                    if self.every != every:  # changed in the window: START again says so (its reply is passed over)
                        every = self.every
                        cast.send(xltcast.START, xltcast.COPY, 0, every, 0)
                    try:
                        kind, m = cast.read_message(1000)
                        last_msg = time.monotonic()
                    except xltcast.usb.core.USBTimeoutError:
                        # no new frame yet (a loading screen, a paused game): still there, unless the PSP is
                        # gone (a pulled cable can come back as timeouts, not as "no device")
                        if time.monotonic() - last_msg > GONE_AFTER and not self._present(backend):
                            raise ConnectionError("the PSP is gone")
                        continue
                    if kind == "audio":
                        self.audio.emit(m[2])
                        continue
                    if kind != "frame":
                        continue
                    if m.fmt != fmt:
                        fmt = m.fmt
                        self.info.emit(fmt)
                    self.frame.emit(frame_image(m))
            except Exception:  # noqa: BLE001 - the link went (casting stopped, cable out)
                pass
            finally:
                if self.isInterruptionRequested():
                    try:
                        cast.send(xltcast.STOP)
                    except Exception:  # noqa: BLE001
                        pass
                cast.close()
                self._cast = None
            self.status.emit("waiting", "Casting stopped. In a game, L+R+SELECT starts it again")
            self._sleep(LOOK_EVERY)

    @staticmethod
    def _present(backend) -> bool:
        """The PSP still on USB (as anything with its IDs)."""
        try:
            return xltcast.usb.core.find(idVendor=xltcast.VID, idProduct=xltcast.PID, backend=backend) is not None
        except Exception:  # noqa: BLE001 - can't tell: assume it is
            return True

    def _sleep(self, s: float):
        end = time.monotonic() + s
        while time.monotonic() < end and not self.isInterruptionRequested():
            self.msleep(50)


class PulseSound:
    """The same through the PC's sound server (PulseAudio, or PipeWire's Pulse side) with
    libpulse-simple, for when Qt Multimedia isn't there (the AppImage carries only Qt's
    essentials). Its writes block, so a thread of its own does them."""
    RATE, MAX_BEHIND = 44100, 0.12

    class _Spec(ctypes.Structure):
        _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]

    class _Attr(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in ("maxlength", "tlength", "prebuf", "minreq", "fragsize")]

    def __init__(self):
        name = ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0"
        lib = ctypes.CDLL(name)
        lib.pa_simple_new.restype = ctypes.c_void_p
        lib.pa_simple_new.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_flush.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        self.lib = lib
        spec = self._Spec(3, self.RATE, 2)  # PA_SAMPLE_S16LE
        none = 0xFFFFFFFF
        attr = self._Attr(none, int(self.RATE * 4 * 0.06), none, none, none)  # ~60 ms in the server
        err = ctypes.c_int(0)
        self.pa = lib.pa_simple_new(None, b"XLT Tool", 1, None, b"XLT Cast", ctypes.byref(spec), None,
                                    ctypes.byref(attr), ctypes.byref(err))  # 1: PA_STREAM_PLAYBACK
        if not self.pa:
            raise RuntimeError(f"no sound server (pulse error {err.value})")
        self.q: queue.Queue = queue.Queue()
        self.waiting = 0  # bytes queued here, not yet written
        self.volume, self.muted = 1.0, False
        self._gain = 1.0
        self._flush = False
        self._alive = True
        self.thread = threading.Thread(target=self._run, name="cast-sound", daemon=True)
        self.thread.start()

    def play(self, data: bytes):
        if self.waiting > self.RATE * 4 * self.MAX_BEHIND:
            return  # behind: dropped, the next pieces catch up
        self.waiting += len(data)
        self.q.put(data)

    def stop(self):
        try:
            while True:
                d = self.q.get_nowait()
                if d is None:  # closing: the thread's end marker stays for it
                    self.q.put(None)
                    break
                self.waiting -= len(d)
        except queue.Empty:
            pass
        self._flush = True

    def set_volume(self, volume: float, muted: bool):
        self.volume, self.muted = volume, muted
        self._gain = 0.0 if muted else volume ** 2  # a slider that sounds even

    def close(self):
        self._alive = False
        self.q.put(None)
        self.thread.join(1.0)
        if self.pa:
            self.lib.pa_simple_free(self.pa)
            self.pa = None

    def _run(self):
        err = ctypes.c_int(0)
        while self._alive:
            data = self.q.get()
            if data is None:
                break
            self.waiting -= len(data)
            if self._flush:
                self._flush = False
                self.lib.pa_simple_flush(self.pa, ctypes.byref(err))
            g = self._gain
            if g != 1.0:
                a = array.array("h", data)
                a = array.array("h", (int(v * g) for v in a))
                data = a.tobytes()
            if self.lib.pa_simple_write(self.pa, data, len(data), ctypes.byref(err)) < 0:
                break


class WinSound:
    """The same on Windows through its own waveOut (winmm, always there), for the Windows build,
    which carries neither Qt Multimedia nor libpulse. A thread keeps a few blocks queued with
    the system and refills the ones it has played."""
    RATE, MAX_BEHIND = 44100, 0.12
    NBUF, SLOT = 8, 8192        # blocks, bytes each (the PSP sends up to 2048 frames = 8 KB at a time)
    IN_FLIGHT = 4               # blocks queued with Windows at most (~80 ms)
    WHDR_DONE, WHDR_PREPARED = 0x1, 0x2

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("winmm is Windows only")
        import ctypes.wintypes as wt

        class WaveFormatEx(ctypes.Structure):
            _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                        ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                        ("cbSize", wt.WORD)]

        class WaveHdr(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wt.DWORD), ("dwBytesRecorded", wt.DWORD),
                        ("dwUser", ctypes.c_size_t), ("dwFlags", wt.DWORD), ("dwLoops", wt.DWORD),
                        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

        self.lib = ctypes.WinDLL("winmm")
        fmt = WaveFormatEx(1, 2, self.RATE, self.RATE * 4, 4, 16, 0)  # WAVE_FORMAT_PCM, 16-bit stereo
        self.handle = ctypes.c_void_p()
        r = self.lib.waveOutOpen(ctypes.byref(self.handle), ctypes.c_uint(0xFFFFFFFF), ctypes.byref(fmt), None, None, 0)
        if r != 0:  # WAVE_MAPPER, CALLBACK_NULL
            raise RuntimeError(f"no sound output (waveOutOpen {r})")
        self.bufs = [ctypes.create_string_buffer(self.SLOT) for _ in range(self.NBUF)]
        self.hdrs = [WaveHdr() for _ in range(self.NBUF)]
        self.hdr_size = ctypes.sizeof(WaveHdr)
        self.q: queue.Queue = queue.Queue()
        self.waiting = 0
        self.volume, self.muted, self._gain = 1.0, False, 1.0
        self._reset = False
        self._alive = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="cast-sound", daemon=True)
        self.thread.start()

    def play(self, data: bytes):
        if self.waiting > self.RATE * 4 * self.MAX_BEHIND:
            return  # behind: dropped, the next pieces catch up
        self.waiting += len(data)
        self.q.put(data)

    def stop(self):
        try:
            while True:
                d = self.q.get_nowait()
                if d is None:  # closing: the thread's end marker stays for it
                    self.q.put(None)
                    break
                self.waiting -= len(d)
        except queue.Empty:
            pass
        self._reset = True

    def set_volume(self, volume: float, muted: bool):
        self.volume, self.muted = volume, muted
        self._gain = 0.0 if muted else volume ** 2

    def close(self):
        self._alive = False
        self.q.put(None)
        self.thread.join(1.0)
        with self.lock:
            if self.handle:
                self.lib.waveOutReset(self.handle)
                for h in self.hdrs:
                    if h.dwFlags & self.WHDR_PREPARED:
                        self.lib.waveOutUnprepareHeader(self.handle, ctypes.byref(h), self.hdr_size)
                self.lib.waveOutClose(self.handle)
                self.handle = None

    def _busy(self) -> int:
        return sum(1 for h in self.hdrs if h.dwFlags & self.WHDR_PREPARED and not h.dwFlags & self.WHDR_DONE)

    def _run(self):
        slot = 0
        while self._alive:
            data = self.q.get()
            if data is None:
                break
            self.waiting -= len(data)
            with self.lock:
                if not self.handle:
                    break
                if self._reset:
                    self._reset = False
                    self.lib.waveOutReset(self.handle)  # what was queued goes; the blocks come back done
                while self._busy() >= self.IN_FLIGHT and self._alive:
                    self.lock.release()
                    time.sleep(0.005)
                    self.lock.acquire()
                g = self._gain
                if g != 1.0:
                    a = array.array("h", data)
                    data = array.array("h", (int(v * g) for v in a)).tobytes()
                data = data[: self.SLOT]
                h = self.hdrs[slot]
                if h.dwFlags & self.WHDR_PREPARED:  # played (or reset): ready to use again
                    self.lib.waveOutUnprepareHeader(self.handle, ctypes.byref(h), self.hdr_size)
                ctypes.memmove(self.bufs[slot], data, len(data))
                h.lpData = ctypes.cast(self.bufs[slot], ctypes.c_void_p)
                h.dwBufferLength = len(data)
                h.dwFlags = 0
                self.lib.waveOutPrepareHeader(self.handle, ctypes.byref(h), self.hdr_size)
                self.lib.waveOutWrite(self.handle, ctypes.byref(h), self.hdr_size)
                slot = (slot + 1) % self.NBUF


class Sound:
    """The game's sound out of the PC's speakers (Qt Multimedia), kept close to the picture:
    when more than MAX_BEHIND is waiting to be played, what comes is dropped until it catches up."""
    RATE, MAX_BEHIND = 44100, 0.12

    def __init__(self):
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        fmt = QAudioFormat()
        fmt.setSampleRate(self.RATE)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        if dev.isNull() or not dev.isFormatSupported(fmt):
            raise RuntimeError("no sound output that plays 44.1 kHz stereo")
        self.sink = QAudioSink(dev, fmt)
        self.sink.setBufferSize(int(self.RATE * 4 * 0.25))
        self.io = None
        self.volume = 1.0
        self.muted = False

    def play(self, data: bytes):
        if self.io is None:
            self.io = self.sink.start()
            self._apply()
        waiting = self.sink.bufferSize() - self.sink.bytesFree()
        if waiting > self.RATE * 4 * self.MAX_BEHIND:
            return  # behind: this piece is dropped, the next ones catch up
        self.io.write(data)

    def stop(self):
        if self.io is not None:
            self.sink.stop()
            self.io = None

    def close(self):
        self.stop()

    def set_volume(self, volume: float, muted: bool):
        self.volume, self.muted = volume, muted
        self._apply()

    def _apply(self):
        from PySide6.QtMultimedia import QtAudio
        lin = QtAudio.convertVolume(self.volume, QtAudio.VolumeScale.LogarithmicVolumeScale,
                                    QtAudio.VolumeScale.LinearVolumeScale)
        self.sink.setVolume(0.0 if self.muted else lin)


class Picture(QWidget):
    """The frame, as big as fits at the PSP's shape, black around it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image: QImage | None = None
        self.sharp = True
        self.setMinimumSize(240, 136)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.message = ""

    def sizeHint(self) -> QSize:
        return QSize(960, 544)

    def target(self) -> QRect:
        w, h = self.width(), self.height()
        if self.sharp and w >= 480 and h >= H:  # whole steps: every PSP pixel the same size
            k = min(w // 480, h // H)
            sw, sh = 480 * k, H * k
            if sw * 1.25 < min(w, h * 480 / H):  # far from filling it: scale freely after all
                sw, sh = self._fit(w, h)
        else:
            sw, sh = self._fit(w, h)
        return QRect((w - sw) // 2, (h - sh) // 2, sw, sh)

    @staticmethod
    def _fit(w: int, h: int) -> tuple[int, int]:
        if w * H > h * 480:
            return h * 480 // H, h
        return w, w * H // 480

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self.image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not self.sharp)
            p.drawImage(self.target(), self.image)
        if self.message:
            p.setPen(QColor(235, 219, 178))
            p.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       self.message)
        p.end()

    def mouseDoubleClickEvent(self, ev):
        w = self.window()
        w.toggle_full()


class CastWindow(QWidget):
    def __init__(self, settings=None):
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("XLT Cast")
        self.settings = settings
        self.picture = Picture(self)
        self.picture.sharp = self._setting("cast_sharp", True)
        self.picture.message = "Looking for the PSP…"
        self.fps_times: list[float] = []
        self.fmt = -1
        self.state = "waiting"
        self.last_audio = 0.0

        self.status = QLabel("Looking for the PSP…")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.sharp_box = QCheckBox("Sharp pixels")
        self.sharp_box.setToolTip("Every PSP pixel shown as a square block, in whole steps (off: smooth scaling)")
        self.sharp_box.setChecked(self.picture.sharp)
        self.sharp_box.toggled.connect(self._sharp)
        self.frames_box = QComboBox()
        self.frames_box.addItem("All frames", 1)
        self.frames_box.addItem("Every other frame (smoother on the PSP)", 2)
        self.frames_box.setToolTip("Every other frame: half the frames here, but the game on the PSP stays smoother. "
                                   "For games that already use the whole PSP (some homebrew, 32-bit colour)")
        self.frames_box.setCurrentIndex(1 if str(self.settings.value("cast_every", 1) if self.settings else 1) == "2"
                                        else 0)
        self.frames_box.currentIndexChanged.connect(self._frames)
        self.shot_btn = QPushButton("Screenshot")
        self.shot_btn.setToolTip("Saves the picture as it comes from the PSP (480×272) in Pictures/XLT Cast (Ctrl+S)")
        self.shot_btn.clicked.connect(self.screenshot)
        self.shot_btn.setEnabled(False)
        self.full_btn = QPushButton("Full screen")
        self.full_btn.setToolTip("F11 or a double click; Esc leaves it")
        self.full_btn.clicked.connect(self.toggle_full)
        from .window import set_icon  # (window imports this module)
        set_icon(self.shot_btn, "camera-photo-symbolic", "camera-photo")
        set_icon(self.full_btn, "view-fullscreen-symbolic", "view-fullscreen")

        self.bar = QWidget()
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(12, 8, 12, 8)
        bar.addWidget(self.status, 1)
        # the sound: a speaker button (mute) and the volume, if this PC can play it
        self.sound = None
        self.sound_error = ""
        # Qt Multimedia if there, else the sound server itself (XLTTOOL_SOUND=pulse: that one only)
        backends = ((PulseSound,) if os.environ.get("XLTTOOL_SOUND") == "pulse"
                    else (Sound, WinSound) if sys.platform == "win32" else (Sound, PulseSound))
        for backend in backends:
            try:
                self.sound = backend()
                break
            except Exception as e:  # noqa: BLE001 - not there, or no output: the next, or the picture alone
                self.sound_error = self.sound_error or str(e)
        self.mute_btn = QToolButton()
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Sound on or off (M)")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        self.volume.setToolTip("Volume of the game's sound")
        self.volume.setValue(int(self.settings.value("cast_volume", 80)) if self.settings else 80)
        self.mute_btn.setChecked(self._setting("cast_muted", False))
        self.volume.valueChanged.connect(self._volume)
        self.mute_btn.toggled.connect(self._volume)
        if self.sound is None:
            self.mute_btn.setEnabled(False)
            self.volume.setEnabled(False)
            self.mute_btn.setToolTip(f"No sound: {self.sound_error}")
        bar.addWidget(self.mute_btn)
        bar.addWidget(self.volume)
        bar.addSpacing(8)
        bar.addWidget(self.frames_box)
        bar.addWidget(self.sharp_box)
        bar.addWidget(self.shot_btn)
        bar.addWidget(self.full_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.picture, 1)
        lay.addWidget(self.bar)

        for keys, fn in ((QKeySequence.StandardKey.FullScreen, self.toggle_full), ("F11", self.toggle_full),
                         ("Escape", self._leave_full), (QKeySequence.StandardKey.Save, self.screenshot),
                         ("M", lambda: self.mute_btn.toggle())):
            act = QAction(self)
            act.setShortcut(QKeySequence(keys))
            act.triggered.connect(fn)
            self.addAction(act)

        geo = self.settings.value("cast_geometry") if self.settings else None
        if geo is None or not self.restoreGeometry(geo):
            self.resize(980, 620)

        self.reader = Reader(self)
        self.reader.every = self.frames_box.currentData()
        self.reader.frame.connect(self._frame)
        self.reader.audio.connect(self._audio)
        self._volume()
        self.reader.status.connect(self._status)
        self.reader.info.connect(self._info)
        self.reader.start()
        self.tick = QTimer(self)
        self.tick.timeout.connect(self._update_status)
        self.tick.start(500)

    def _setting(self, key: str, default: bool) -> bool:
        if not self.settings:
            return default
        return str(self.settings.value(key, "true" if default else "false")).lower() == "true"

    # -- from the reader
    def _audio(self, data: bytes):
        self.last_audio = time.monotonic()
        if self.sound is not None:
            self.sound.play(data)

    def _volume(self, *_):
        muted = self.mute_btn.isChecked()
        from .window import set_icon
        set_icon(self.mute_btn, *(("audio-volume-muted-symbolic", "audio-volume-muted") if muted or not self.volume.value()
                                  else ("audio-volume-high-symbolic", "audio-volume-high")))
        if self.mute_btn.icon().isNull():
            self.mute_btn.setText("Muted" if muted else "Sound")
        if self.sound is not None:
            self.sound.set_volume(self.volume.value() / 100, muted)
        if self.settings:
            self.settings.setValue("cast_volume", self.volume.value())
            self.settings.setValue("cast_muted", "true" if muted else "false")

    def _frame(self, img: QImage):
        self.picture.image = img
        self.picture.message = ""
        self.picture.update()
        now = time.monotonic()
        self.fps_times.append(now)
        while self.fps_times and now - self.fps_times[0] > 1.0:
            self.fps_times.pop(0)

    def _status(self, state: str, text: str):
        self.state = state
        if state != "casting" and self.sound is not None:
            self.sound.stop()  # nothing old left to play when it comes back
        if state != "casting":
            self.fps_times.clear()
            self.picture.message = text
            self.picture.update()
            self.status.setText("Not casting" if state == "waiting" else text)  # (the picture says how)
        self.shot_btn.setEnabled(state == "casting" and self.picture.image is not None)
        self._update_status()

    def _info(self, fmt: int):
        self.fmt = fmt

    def _update_status(self):
        if self.state == "casting":
            fps = len(self.fps_times)
            sound = time.monotonic() - self.last_audio < 1.0
            self.status.setText(f"Casting · {fps} fps" + (f" · {COLOUR[self.fmt]}" if self.fmt in COLOUR else "")
                                + (" · sound" if sound else ""))
            self.shot_btn.setEnabled(self.picture.image is not None)

    # -- buttons
    def _frames(self, *_):
        every = self.frames_box.currentData()
        self.reader.every = every
        if self.settings:
            self.settings.setValue("cast_every", every)

    def _sharp(self, on: bool):
        self.picture.sharp = on
        self.picture.update()
        if self.settings:
            self.settings.setValue("cast_sharp", "true" if on else "false")

    def toggle_full(self):
        if self.isFullScreen():
            self._leave_full()
        else:
            self.bar.hide()
            self.showFullScreen()

    def _leave_full(self):
        if self.isFullScreen():
            self.showNormal()
            self.bar.show()

    def screenshot(self):
        img = self.picture.image
        if img is None or self.state != "casting":
            return
        folder = os.path.join(_pictures_dir(), "XLT Cast")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("XLT Cast %Y-%m-%d %H.%M.%S.png"))
        ok = img.save(path, "PNG")
        self.status.setText(f"Saved {path}" if ok else f"Couldn't save {path}")
        self.tick.stop()
        QTimer.singleShot(2500, lambda: self.tick.start(500))

    def closeEvent(self, ev):
        if self.settings and not self.isFullScreen():
            self.settings.setValue("cast_geometry", self.saveGeometry())
        self.reader.requestInterruption()
        self.reader.wait(3000)
        if self.sound is not None:
            self.sound.close()
        super().closeEvent(ev)


def _pictures_dir() -> str:
    from PySide6.QtCore import QStandardPaths
    d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    return d or os.path.expanduser("~")
