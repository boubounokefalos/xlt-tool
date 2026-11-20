"""Tests for xlttool/castview.py with a pretend PSP: a Cast that sends made-up frames
(16-bit and 32-bit colour), then goes away, so the window's picture, fps, screenshot
and waiting again run without the hardware. Qt runs offscreen."""
import os
import queue
import struct
import sys
import tempfile
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never real windows, whatever the session sets
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from xlttool import castview, xltcast  # noqa: E402

APP = QApplication.instance() or QApplication([])


def frame_565(stride: int = 512) -> xltcast.Frame:
    """Red | green | blue thirds, PSP 565 (red in the low bits)."""
    row = b"".join(struct.pack("<H", 31 if x < 160 else 63 << 5 if x < 320 else 31 << 11) for x in range(stride))
    return xltcast.Frame(0, stride, 1, 0, row * xltcast.H)


def frame_8888() -> xltcast.Frame:
    row = b"".join(bytes((255, 0, 0, 255)) if x < 240 else bytes((0, 0, 255, 255)) for x in range(512))
    return xltcast.Frame(3, 512, 1, 0, row * xltcast.H)


class FakeCast:
    """Sends `frames` frames at 30 a second, then the cable is pulled (a USB error)."""
    opened = 0
    frames = 45
    make = staticmethod(frame_565)
    started = []
    stopped = 0
    once = False  # True: after its cable is pulled, it isn't found again

    def __init__(self, backend=None):
        if FakeCast.once and FakeCast.opened:
            FakeCast.opened += 1
            raise xltcast.CastError("No PSP (054c:01cb).")
        FakeCast.opened += 1
        self.sent = 0

    everys = []

    def command(self, op, a=0, b=0, c=0, d=0, on_frame=None):
        FakeCast.started.append((op, a))
        if op == xltcast.START:
            FakeCast.everys.append(c)
        return 0, None

    def send(self, op, a=0, b=0, c=0, d=0):
        if op == xltcast.STOP:
            FakeCast.stopped += 1
        if op == xltcast.START:
            FakeCast.everys.append(c)

    def read_message(self, timeout=3000):
        if self.sent >= FakeCast.frames:
            raise OSError(19, "No such device (it may have been disconnected)")
        self.toggle = not getattr(self, "toggle", False)
        if self.toggle:  # sound between the frames: 1/30 s of it
            return "audio", (44100, self.sent * 1470, bytes(1470 * 4))
        time.sleep(1 / 30)
        self.sent += 1
        return "frame", FakeCast.make()

    def close(self):
        pass


def pump(seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.01)


class PulseQueueTest(unittest.TestCase):
    """stop() after close() (a late status change while the window closes): no error, the end marker kept."""

    def test_stop_after_close(self):
        for cls in (castview.PulseSound, castview.WinSound):
            snd = cls.__new__(cls)  # no sound server needed: only the queue
            snd.q, snd.waiting, snd._flush, snd._reset = queue.Queue(), 8, False, False
            snd.q.put(b"\0" * 8)
            snd.q.put(None)
            snd.stop()
            self.assertEqual(snd.waiting, 0)
            self.assertIsNone(snd.q.get_nowait(), cls.__name__)


class SyncBesideCastTest(unittest.TestCase):
    """Sync while the PSP casts: told so before opening (on Windows the Cast window holds the
    device and opening it fails as if the driver were wrong)."""

    def test_casting_said_before_open(self):
        from types import SimpleNamespace
        from unittest import mock

        import usb.core

        from xlttool import xltlink

        class Busy:
            def __getitem__(self, i):
                return [SimpleNamespace(bInterfaceSubClass=xltlink.SUBCLASS, bInterfaceProtocol=xltlink.CAST_PROTOCOL)]

            def get_active_configuration(self):
                raise usb.core.USBError("Access denied", errno=13)

        with mock.patch.object(usb.core, "find", return_value=Busy()):
            with self.assertRaisesRegex(xltlink.LinkError, "casting"):
                xltlink.Link()

    def test_sync_lets_the_device_go(self):
        """A sync link that fails halfway, or is dropped, closes the device at once: on Windows a
        handle left for Python to collect kept Cast out ("No access") once the PSP started a game."""
        from types import SimpleNamespace
        from unittest import mock

        import usb.core
        import usb.util

        from xlttool import pspsync, xltlink

        class Half:  # the sync device, gone while being set up
            def __getitem__(self, i):
                return [SimpleNamespace(bInterfaceSubClass=xltlink.SUBCLASS, bInterfaceProtocol=xltlink.PROTOCOL)]

            def get_active_configuration(self):
                raise usb.core.USBError("Pipe error", errno=32)

            set_configuration = get_active_configuration

        dev = Half()
        with mock.patch.object(usb.core, "find", return_value=dev), \
                mock.patch.object(usb.util, "dispose_resources") as dispose:
            with self.assertRaises(usb.core.USBError):
                xltlink.Link()
            dispose.assert_called_once_with(dev)
        # and Cast, looking every second while the PSP is in XLT, never opens sync's device, and
        # lets its own go when it fails halfway (it kept sync out of a waiting Cast window's PSP)
        class Opened(Exception):
            pass

        class SyncDev(Half):
            def get_active_configuration(self):
                raise Opened

        with mock.patch.object(usb.core, "find", return_value=SyncDev()):
            with self.assertRaisesRegex(xltcast.CastError, "as XLT sync"):
                xltcast.Cast()

        class CastHalf(Half):
            def __getitem__(self, i):
                return [SimpleNamespace(bInterfaceSubClass=xltcast.SUBCLASS, bInterfaceProtocol=xltcast.PROTOCOL)]

        dev = CastHalf()
        with mock.patch.object(usb.core, "find", return_value=dev), \
                mock.patch.object(usb.util, "dispose_resources") as dispose:
            with self.assertRaises(usb.core.USBError):
                xltcast.Cast()
            dispose.assert_called_once_with(dev)
        link = mock.Mock()
        self.assertIsNone(pspsync._drop(link))
        link.close.assert_called_once_with()
        self.assertIsNone(pspsync._drop(None))


class StylesheetTest(unittest.TestCase):
    def test_parses_on_windows(self):
        """No backslashes in its url("..."): on Windows they made the whole stylesheet fail to parse."""
        from xlttool import theme
        s = theme.stylesheet(theme.detect())
        self.assertIn('url("', s)
        self.assertNotIn("\\", s)


class FakeSound:
    """Instead of the PC's speakers."""
    played, volume, muted, stops = [], 1.0, False, 0

    def __init__(self):
        FakeSound.played, FakeSound.stops = [], 0

    def play(self, data):
        FakeSound.played.append(len(data))

    def stop(self):
        FakeSound.stops += 1

    def close(self):
        pass

    def set_volume(self, volume, muted):
        FakeSound.volume, FakeSound.muted = volume, muted


class CastViewTest(unittest.TestCase):
    def setUp(self):
        self._real_sound = castview.Sound
        castview.Sound = FakeSound
        self._real = xltcast.Cast
        xltcast.Cast = FakeCast
        FakeCast.opened, FakeCast.started, FakeCast.stopped, FakeCast.once, FakeCast.everys = 0, [], 0, False, []
        self.tmp = tempfile.mkdtemp()
        self.settings = QSettings(os.path.join(self.tmp, "s.ini"), QSettings.Format.IniFormat)

    def tearDown(self):
        xltcast.Cast = self._real
        castview.Sound = self._real_sound

    def test_frame_colours(self):
        img = castview.frame_image(frame_565())
        self.assertEqual((img.width(), img.height()), (480, 272))
        self.assertEqual(img.pixelColor(10, 10).getRgb()[:3], (255, 0, 0))
        self.assertEqual(img.pixelColor(240, 10).getRgb()[:3], (0, 255, 0))
        self.assertEqual(img.pixelColor(470, 10).getRgb()[:3], (0, 0, 255))
        img = castview.frame_image(frame_8888())
        self.assertEqual(img.pixelColor(10, 10).getRgb()[:3], (255, 0, 0))
        self.assertEqual(img.pixelColor(470, 10).getRgb()[:3], (0, 0, 255))

    def test_casting_then_waiting(self):
        FakeCast.frames, FakeCast.make, FakeCast.once = 45, staticmethod(frame_565), True
        w = castview.CastWindow(self.settings)
        w.resize(1000, 640)
        w.show()
        pump(1.0)
        self.assertEqual(w.state, "casting")
        self.assertEqual(FakeCast.started[0], (xltcast.START, xltcast.COPY))
        self.assertIsNotNone(w.picture.image)
        w._update_status()
        self.assertRegex(w.status.text(), r"^Casting · \d+ fps · 16-bit colour · sound$")
        self.assertGreater(len(FakeSound.played), 15, "the sound reached the speakers")
        self.assertEqual(set(FakeSound.played), {1470 * 4})
        # volume and mute: kept in the settings, passed on
        w.volume.setValue(40)
        self.assertAlmostEqual(FakeSound.volume, 0.4)
        w.mute_btn.setChecked(True)
        self.assertTrue(FakeSound.muted)
        self.assertEqual(str(self.settings.value("cast_muted")), "true")
        self.assertEqual(int(self.settings.value("cast_volume")), 40)
        w.mute_btn.setChecked(False)
        fps = len(w.fps_times)
        self.assertGreater(fps, 15, w.status.text())
        # sharp pixels: whole steps (960x544 inside a 1000-wide window)
        t = w.picture.target()
        self.assertEqual((t.width() % 480, t.height() % 272), (0, 0))
        w.grab().save(os.path.join(self.tmp, "casting.png"))
        # a screenshot is the PSP's own 480x272 picture
        os.environ["HOME"] = self.tmp
        castview._pictures_dir = lambda: self.tmp
        w.screenshot()
        shots = os.listdir(os.path.join(self.tmp, "XLT Cast"))
        self.assertEqual(len(shots), 1)
        self.assertTrue(w.status.text().startswith("Saved "))
        # the cable goes: waiting again, and it looks for the PSP again
        pump(2.0)
        self.assertEqual(w.state, "waiting")
        self.assertGreaterEqual(FakeSound.stops, 1, "the sound stops with the casting")
        self.assertIn("L+R+SELECT", w.picture.message)
        self.assertGreaterEqual(FakeCast.opened, 2)
        w.close()
        self.assertFalse(w.reader.isRunning())

    def test_32bit_and_close_sends_stop(self):
        FakeCast.frames, FakeCast.make = 10_000, staticmethod(frame_8888)
        w = castview.CastWindow(self.settings)
        w.show()
        pump(0.6)
        w._update_status()
        self.assertIn("32-bit colour", w.status.text())
        w.close()
        self.assertFalse(w.reader.isRunning())
        self.assertEqual(FakeCast.stopped, 1, "closing the window tells the PSP to stop")

    def test_every_other_frame(self):
        FakeCast.frames, FakeCast.make = 10_000, staticmethod(frame_565)
        w = castview.CastWindow(self.settings)
        w.show()
        pump(0.4)
        self.assertEqual(FakeCast.everys, [1], "starts with every frame")
        w.frames_box.setCurrentIndex(1)
        pump(0.3)
        self.assertEqual(FakeCast.everys, [1, 2], "the change reaches the PSP at once")
        self.assertEqual(int(self.settings.value("cast_every")), 2)
        w.close()
        w2 = castview.CastWindow(self.settings)  # kept for the next time
        self.assertEqual(w2.frames_box.currentData(), 2)
        w2.show()
        pump(0.3)
        self.assertEqual(FakeCast.everys[-1], 2)
        w2.close()

    def test_pulled_cable_as_timeouts(self):
        """The cable pulled, and libusb says 'timeout' for ever instead of 'no device'."""
        class Silent(FakeCast):
            def read_message(self, timeout=3000):
                if self.sent >= 10:
                    time.sleep(timeout / 1000)
                    raise xltcast.usb.core.USBTimeoutError("Operation timed out", -7, 110)
                return super().read_message(timeout)
        xltcast.Cast = Silent
        FakeCast.frames, FakeCast.make, FakeCast.once = 10_000, staticmethod(frame_565), True
        old_present, old_gone = castview.Reader._present, castview.GONE_AFTER
        castview.Reader._present = staticmethod(lambda backend: False)
        castview.GONE_AFTER = 0.5
        try:
            w = castview.CastWindow(self.settings)
            w.show()
            pump(0.4)
            self.assertEqual(w.state, "casting")
            pump(2.5)
            self.assertEqual(w.state, "waiting", "still 'casting' with the cable out")
            w.close()
        finally:
            castview.Reader._present, castview.GONE_AFTER = old_present, old_gone

    def test_not_there_waits(self):
        def missing(backend=None):
            raise xltcast.CastError("No PSP (054c:01cb).")
        xltcast.Cast = missing
        w = castview.CastWindow(self.settings)
        w.show()
        pump(0.5)
        self.assertEqual(w.state, "waiting")
        self.assertEqual(w.status.text(), "Not casting")
        self.assertIn("in a game, press L+R+SELECT", w.picture.message)
        w.close()


if __name__ == "__main__":
    unittest.main()
