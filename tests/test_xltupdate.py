"""Tests for xlttool/xltupdate.py: reading XLT builds (EBOOT.PBP and release zips), the
release check and download against a pretend GitHub, the PSP's version file, and what is
left on the PSP (a pretend link)."""
import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from xlttool import xltupdate  # noqa: E402


def eboot(version: str = "1.4.0", size: int = 300 * 1024) -> bytes:
    body = b"\0PBP" + b"\0" * 1000 + f"XLT {version} started as %.200s (%s, ARK-%d)".encode()
    return body + b"\x55" * (size - len(body))


def release_zip(data: bytes, name: str = "PSP/GAME/XLT/EBOOT.PBP") -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("README.txt", "hi")
        z.writestr("PSP/GAME/OTHER/EBOOT.PBP", eboot("0.1.0"))
        z.writestr(name, data)
    return b.getvalue()


class Builds(unittest.TestCase):
    def test_eboot_and_zip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "EBOOT.PBP")
            with open(p, "wb") as f:
                f.write(eboot("1.3.2"))
            self.assertEqual(xltupdate.read_eboot(p)[1], "1.3.2")
            z = os.path.join(d, "xlt.zip")
            with open(z, "wb") as f:
                f.write(release_zip(eboot("1.4.0-beta")))
            data, v = xltupdate.read_eboot(z)
            self.assertEqual(v, "1.4.0-beta")  # PSP/GAME/XLT wins over PSP/GAME/OTHER
            self.assertEqual(data[:4], b"\0PBP")

    def test_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("small.pbp", eboot(size=1000)), ("other.pbp", b"\0PBP" + b"\0" * 400000),
                               ("text.pbp", b"hello" * 100000)):
                p = os.path.join(d, name)
                with open(p, "wb") as f:
                    f.write(body)
                with self.assertRaises(ValueError):
                    xltupdate.read_eboot(p)

    def test_rank(self):
        self.assertEqual(xltupdate.eboot_rank("PSP/GAME/XLT/EBOOT.PBP"), 2)
        self.assertEqual(xltupdate.eboot_rank("x/psp/game/xlt/eboot.pbp"), 2)
        self.assertEqual(xltupdate.eboot_rank("EBOOT.PBP"), 1)
        self.assertEqual(xltupdate.eboot_rank("EBOOT.PBP.txt"), 0)

    def test_version_txt(self):
        self.assertEqual(xltupdate.parse_version_txt("1.3.1\n"), ("1.3.1", ""))
        self.assertEqual(xltupdate.parse_version_txt("1.3.1\r\nnext 1.4.0\r\n"), ("1.3.1", "1.4.0"))
        self.assertEqual(xltupdate.parse_version_txt(""), ("", ""))

    def test_release_txt_cut(self):
        t = xltupdate.release_txt("1.4.0", "line\n" * 2000)
        self.assertTrue(t.startswith(b"1.4.0\n"))
        self.assertLessEqual(len(t), xltupdate.NOTES_MAX + 8)


class GitHub(unittest.TestCase):
    def setUp(self):
        self.files = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = outer.files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        os.environ["XLTTOOL_UPDATE_API"] = self.base
        os.environ["XLTTOOL_XLT_REPO"] = "me/xlt"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        del os.environ["XLTTOOL_UPDATE_API"], os.environ["XLTTOOL_XLT_REPO"]

    def publish(self, tag: str, zip_bytes: bytes, sha: str | None = None):
        self.files["/x.zip"] = zip_bytes
        self.files["/repos/me/xlt/releases/latest"] = json.dumps({
            "tag_name": tag, "body": "- new", "html_url": "page", "assets": [
                {"name": "XLT.zip", "browser_download_url": self.base + "/x.zip", "size": len(zip_bytes),
                 "digest": "sha256:" + (sha or hashlib.sha256(zip_bytes).hexdigest())}]}).encode()

    def test_none(self):
        self.assertIsNone(xltupdate.latest())

    def test_download(self):
        self.publish("v1.4.0", release_zip(eboot("1.4.0")))
        rel = xltupdate.latest()
        self.assertEqual((rel.version, rel.zip_name), ("1.4.0", "XLT.zip"))
        got = []
        data = xltupdate.download(rel, got.append)
        self.assertEqual(xltupdate.eboot_version(data), "1.4.0")
        self.assertAlmostEqual(got[-1], 1.0)

    def test_bad_sha(self):
        self.publish("v1.4.0", release_zip(eboot("1.4.0")), sha="0" * 64)
        with self.assertRaises(RuntimeError):
            xltupdate.download(xltupdate.latest())

    def test_wrong_version_inside(self):
        self.publish("v1.4.0", release_zip(eboot("1.3.9")))
        with self.assertRaises(RuntimeError):
            xltupdate.download(xltupdate.latest())


class FakeLink:
    def __init__(self, files):
        self.files, self.order = dict(files), []

    def stat(self, path):
        return object() if path in self.files else None

    def get(self, path):
        if path not in self.files:
            raise OSError("no such file")
        return self.files[path]

    def mkdir(self, path):
        return path

    def put(self, local, remote, replace=False, progress=None):
        with open(local, "rb") as f:
            data = f.read()
        if progress:
            progress(len(data), len(data))
        self.files[remote] = data
        self.order.append(remote.rsplit("/", 1)[-1])
        return {}


class Psp(unittest.TestCase):
    def test_find_and_send(self):
        link = FakeLink({"ef0:/PSP/GAME/XLT/EBOOT.PBP": b"x", "ef0:/PSP/GAME/XLT/cache/version.txt": b"1.3.1\n"})
        x = xltupdate.find_xlt(link)
        self.assertEqual((x.folder, x.running, x.next), ("ef0:/PSP/GAME/XLT", "1.3.1", ""))
        xltupdate.send(link, x, eboot("1.4.0"), "1.4.0", "notes")
        self.assertEqual(link.order, ["RELEASE.TXT", "XLT.PBP"])  # the PBP last: XLT waits for it
        self.assertTrue(link.files["ef0:/PSP/GAME/XLT/UPDATE/RELEASE.TXT"].startswith(b"1.4.0\nnotes"))

    def test_old_xlt_and_none(self):
        self.assertEqual(xltupdate.find_xlt(FakeLink({"ms0:/PSP/GAME/XLT/EBOOT.PBP": b"x"})).running, "")
        self.assertIsNone(xltupdate.find_xlt(FakeLink({})))

    def test_cancel(self):
        link = FakeLink({})
        x = xltupdate.PspXlt("ms0:/PSP/GAME/XLT", "1.3.1")
        with self.assertRaises(InterruptedError):
            xltupdate.send(link, x, eboot(), "1.4.0", "", cancel=lambda: True)


if __name__ == "__main__":
    unittest.main()
