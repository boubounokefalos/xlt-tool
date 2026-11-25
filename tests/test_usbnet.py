"""Tests for xlttool/usbnet.py (fetching USBNet's release, reading its gateway's log) and
usbnetview.py (the window's side: the gateway run as a program, the PSP's plugin left in
XLT's folder), with a pretend GitHub, a pretend gateway (a script) and a pretend PSP.
Qt runs offscreen."""
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from xlttool import update, usbnet, usbnetview, xltlink  # noqa: E402

APP = QApplication.instance() or QApplication([])
GW = usbnet.gateway_asset()


def wait_for(cond, secs=5.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        APP.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    APP.processEvents()
    return cond()


def plugin_zip(prx=b"PRX", extra=(("ICON0.PNG", b"png"), ("LICENSE", b"MIT"))):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("usbnet.prx", prx)
        for name, data in extra:
            z.writestr(name, data)
    return b.getvalue()


class FakeGitHub:
    """update._get: the release JSON, and each asset's bytes by its address."""

    def __init__(self, tag="v1.0.0", files=None, bad=()):
        self.files = files or {usbnet.ZIP: plugin_zip(b"PRX " + tag.encode()), GW: b"#!/bin/sh\necho gw\n"}
        self.tag, self.bad, self.calls, self.offline = tag, set(bad), [], False

    def release(self):
        return {"tag_name": self.tag, "html_url": "https://example/rel", "assets": [
            {"name": n, "browser_download_url": f"https://dl/{n}", "size": len(b),
             "digest": "sha256:" + hashlib.sha256(b"x" if n in self.bad else b).hexdigest()}
            for n, b in self.files.items()]}

    def __call__(self, url, timeout=20):
        self.calls.append(url)
        if self.offline:
            raise OSError("network unreachable")
        if url.endswith("/releases/latest"):
            data = json.dumps(self.release()).encode()
        else:
            data = self.files[url.rsplit("/", 1)[1]]
        return _Resp(data)


class _Resp(io.BytesIO):
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Places(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name, "XDG_CACHE_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


@unittest.skipUnless(GW, "USBNet has no gateway for this computer")
class Fetch(Places):
    def test_downloads_checks_and_keeps(self):
        gh = FakeGitHub()
        with mock.patch.object(update, "_get", gh):
            k = usbnet.fetch()
        self.assertEqual(k.tag, "v1.0.0")
        with open(k.prx, "rb") as f:
            self.assertEqual(f.read(), b"PRX v1.0.0")
        self.assertEqual([os.path.basename(f) for f in k.psp_files], ["ICON0.PNG", "LICENSE", "usbnet.prx"])
        if not GW.endswith(".exe"):
            self.assertTrue(os.stat(k.gateway).st_mode & stat.S_IXUSR)
        # the same release again: nothing downloaded
        with mock.patch.object(update, "_get", gh):
            gh.calls.clear()
            self.assertEqual(usbnet.fetch().tag, "v1.0.0")
        self.assertEqual(len(gh.calls), 1)

    def test_newer_release_replaces_the_old_copy(self):
        with mock.patch.object(update, "_get", FakeGitHub("v1.0.0")):
            usbnet.fetch()
        with mock.patch.object(update, "_get", FakeGitHub("v1.1.0")):
            k = usbnet.fetch()
        self.assertEqual(k.tag, "v1.1.0")
        self.assertEqual(os.listdir(usbnet.home()), ["v1.1.0"])

    def test_only_a_newer_release_is_downloaded(self):
        with mock.patch.object(update, "_get", FakeGitHub("v1.1.0")):
            usbnet.fetch()
        gh = FakeGitHub("v1.0.0")  # GitHub's latest is older than the copy kept (a release taken back)
        with mock.patch.object(update, "_get", gh):
            self.assertEqual(usbnet.fetch().tag, "v1.1.0")
        self.assertEqual(len(gh.calls), 1)  # only the look at the latest release

    def test_zip_with_a_path_refused(self):
        bad = plugin_zip(extra=(("../evil", b"x"),))
        with mock.patch.object(update, "_get", FakeGitHub(files={usbnet.ZIP: bad, GW: b"g"})):
            with self.assertRaisesRegex(RuntimeError, "unexpected file"):
                usbnet.fetch()
        self.assertIsNone(usbnet.kept())

    def test_wrong_checksum_not_used(self):
        with mock.patch.object(update, "_get", FakeGitHub(bad=[usbnet.ZIP])):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                usbnet.fetch()
        self.assertIsNone(usbnet.kept())
        self.assertFalse(any(n.endswith(".part") for _, _, fs in os.walk(usbnet.home()) for n in fs))

    def test_offline_uses_the_kept_copy(self):
        gh = FakeGitHub()
        with mock.patch.object(update, "_get", gh):
            usbnet.fetch()
            gh.offline = True
            lines = []
            self.assertEqual(usbnet.fetch(lines.append).tag, "v1.0.0")
        self.assertIn("kept from before", lines[0])

    def test_offline_and_nothing_kept(self):
        gh = FakeGitHub()
        gh.offline = True
        with mock.patch.object(update, "_get", gh):
            with self.assertRaisesRegex(RuntimeError, "couldn't be downloaded"):
                usbnet.fetch()

    def test_release_without_the_gateway(self):
        with mock.patch.object(update, "_get", FakeGitHub(files={usbnet.ZIP: plugin_zip()})):
            with self.assertRaisesRegex(RuntimeError, "lacks"):
                usbnet.fetch()


class Words(unittest.TestCase):
    def test_gateway_for_each_computer(self):
        self.assertEqual(usbnet.gateway_asset("linux", "x86_64"), "pspkit-usbnetd-linux-x86_64")
        self.assertEqual(usbnet.gateway_asset("linux", "aarch64"), "pspkit-usbnetd-linux-aarch64")
        self.assertEqual(usbnet.gateway_asset("win32", "AMD64"), "pspkit-usbnetd-windows-x86_64.exe")
        self.assertEqual(usbnet.gateway_asset("darwin", "arm64"), "pspkit-usbnetd-macos")
        self.assertEqual(usbnet.gateway_asset("linux", "riscv64"), "")

    def test_log_lines(self):
        r = usbnet.read_line
        self.assertEqual(r("    0.512 usb: device found (bus 3 port 2, interface 0, ...)"), ("online", "PSP connected"))
        self.assertEqual(r("   12.000 PSP Go | usb: device lost (unplugged)"), ("waiting", "waiting for the PSP"))
        self.assertEqual(r("    0.100 usb: waiting for the device: no PSP (054c:01c9) on USB")[0], "waiting")
        self.assertEqual(r("    0.100 usb: waiting for the device: PSP present, but no access to it: start "
                           "the gateway with sudo")[0], "no_access")
        self.assertEqual(r("    0.1 usb: waiting for the device: another program is using the usbnet interface")[0], "busy")
        st = ("   20.0 stats: from PSP 9 frames 900 B (0 ignored), to PSP 8 frames 800 B (0 lost); tcp 2 open, "
              "0 connecting, 3 opened, 0 failed, 100000 B up, 2400000 B down; udp 1 out 1 in, 0 flows; "
              "dns 2 queries 2 answers; dhcp 1 leases")
        self.assertEqual(r(st), ("stats", "PSP online · 2 connections · 2.5 MB"))
        self.assertIsNone(r("    0.000 pspkit-usbnetd 1.0.0: gateway 10.77.0.1/24"))

    def test_parse_release(self):
        rel = usbnet.parse_release({"tag_name": "v1.0.0", "assets": [
            {"name": "usbnet.prx", "browser_download_url": "u", "size": 3, "digest": "sha256:" + "ab" * 32}]})
        self.assertEqual(rel.version, "1.0.0")
        self.assertEqual(rel.assets["usbnet.prx"].sha256, "ab" * 32)
        self.assertIsNone(usbnet.parse_release({"tag_name": "v2", "prerelease": True}))
        self.assertIsNone(usbnet.parse_release({"tag_name": "../../x"}))

    def test_udev(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(usbnet.udev_ok(d))
            with open(os.path.join(d, "60-xltsync.rules"), "w") as f:
                f.write('SUBSYSTEM=="usb", ATTRS{idVendor}=="054c", ATTRS{idProduct}=="01cb"\n')
            self.assertFalse(usbnet.udev_ok(d))
            from xlttool import usbaccess
            with open(os.path.join(d, "60-xltsync.rules"), "w") as f:
                f.write(usbaccess.RULE)
            self.assertTrue(usbnet.udev_ok(d))


# ------------------------------------------------------------------ the window's side

GATEWAY = """#!/bin/sh
echo "    0.000 pspkit-usbnetd 1.0.0: gateway 10.77.0.1/24"
echo "    0.001 usb: waiting for the device: no PSP (054c:01c9) on USB"
sleep 0.3
echo "    0.500 usb: device found (bus 3, interface 0, says \\"PSP Street\\")"
sleep 0.3
echo "   10.000 stats: from PSP 1 frames 1 B (0 ignored), to PSP 1 frames 1 B (0 lost); tcp 1 open, 0 connecting, 1 opened, 0 failed, 10 B up, 300000 B down; udp 0 out 0 in, 0 flows; dns 1 queries 1 answers; dhcp 1 leases"
exec sleep 30
"""


class FakeLink:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.made = []

    def stat(self, path):
        return object() if path in self.files else None

    def get(self, path):
        return self.files[path]

    def mkdir(self, path):
        self.made.append(path)

    def put(self, local, remote, replace=False):
        with open(local, "rb") as f:
            self.files[remote] = f.read()


class FakePsp(QObject):
    state = Signal(str, str)
    task_done = Signal(object)

    def __init__(self, link):
        super().__init__()
        self.link, self.status = link, "ready"

    def ready(self):
        return self.status == "ready"

    def run_task(self, task):
        try:
            task.result = task.fn(self.link, task)
        except xltlink.XltsError as e:
            task.error = str(e)
        self.task_done.emit(task)


@unittest.skipIf(sys.platform == "win32", "the pretend gateway is a shell script")
class Window(Places):
    def setUp(self):
        super().setUp()
        d = os.path.join(usbnet.home(), "v1.0.0")
        os.makedirs(os.path.join(d, "psp"))
        for name, data in ((usbnet.PRX, b"THE PLUGIN"), ("ICON0.PNG", b"png"), ("LICENSE", b"MIT")):
            with open(os.path.join(d, "psp", name), "wb") as f:
                f.write(data)
        gw = os.path.join(d, GW or "pspkit-usbnetd-linux-x86_64")
        with open(gw, "w") as f:
            f.write(GATEWAY)
        os.chmod(gw, 0o755)
        self.kept = usbnet.Kept("v1.0.0", d, os.path.join(d, "psp", usbnet.PRX), gw)
        self.patch = mock.patch.object(usbnet, "fetch", lambda log=None, cancel=None: self.kept)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def run_net(self, link):
        psp = FakePsp(link)
        net = usbnetview.UsbNet(psp=psp)
        states, staged = [], []
        net.state.connect(lambda k, t: states.append((k, t)))
        net.staged.connect(staged.append)
        self.addCleanup(net.stop)
        net.start()
        return net, states, staged

    def test_gateway_states_and_stop(self):
        net, states, staged = self.run_net(FakeLink())
        self.assertTrue(wait_for(lambda: ("online", "PSP connected") in states))
        self.assertTrue(wait_for(lambda: ("online", "PSP online · 1 connection · 0.3 MB") in states))
        self.assertEqual(states[0], ("starting", "getting USBNet"))
        self.assertTrue(net.running())
        net.stop()
        self.assertFalse(net.running())
        self.assertEqual(states[-1], ("off", ""))

    def test_plugin_left_for_xlt(self):
        link = FakeLink()
        net, states, staged = self.run_net(link)
        self.assertTrue(wait_for(lambda: staged))
        d = usbnet.STAGED_DIR
        self.assertEqual(link.files[f"{d}/usbnet.prx"], b"THE PLUGIN")
        self.assertEqual(sorted(link.files), sorted(f"{d}/{n}" for n in ("ICON0.PNG", "LICENSE", "usbnet.prx")))
        self.assertIn("Internet over USB", staged[0])

    def test_installed_by_pspdx_left_alone(self):
        for path in usbnet.INSTALLED:
            with self.subTest(path=path):
                link = FakeLink({path: b"AN OLDER ONE"})  # any version: never replaced
                net, states, staged = self.run_net(link)
                self.assertTrue(wait_for(lambda: any("installed on the PSP already" in x for x in net.lines)))
                self.assertEqual(staged, [])
                self.assertEqual(list(link.files), [path])
                net.stop()

    def test_waiting_for_xlt_already(self):
        link = FakeLink({f"{usbnet.STAGED_DIR}/usbnet.prx": b"THE PLUGIN"})
        net, states, staged = self.run_net(link)
        self.assertTrue(wait_for(lambda: ("online", "PSP connected") in states))
        self.assertEqual(staged, [])
        self.assertEqual(len(link.files), 1)

    def test_gateway_already_running(self):
        with open(self.kept.gateway, "w") as f:
            f.write("#!/bin/sh\necho 'pspkit-usbnetd: another pspkit-usbnetd is already running on this "
                    "computer; end that one first' >&2\nexit 1\n")
        net, states, staged = self.run_net(FakeLink())
        self.assertTrue(wait_for(lambda: states and states[-1][0] == "error"))
        self.assertIn("another USBNet gateway", states[-1][1])
        self.assertFalse(net.running())


if __name__ == "__main__":
    unittest.main()
