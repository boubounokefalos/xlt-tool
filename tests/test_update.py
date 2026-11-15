"""Tests for xlttool/update.py against a pretend GitHub on this PC: the release
check, version numbers, the download with its checks, and the swap."""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from xlttool import update  # noqa: E402


def make_program(path: str, says: str):
    """A tiny Linux program that prints a version, standing in for an AppImage."""
    src = path + ".c"
    with open(src, "w") as f:
        f.write('#include <stdio.h>\nint main(void){puts("%s");return 0;}\n' % says)
    subprocess.run(["cc", "-o", path, src], check=True)
    os.remove(src)


class Server:
    def __init__(self, root: str):
        self.root, self.release = root, None
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith("/releases/latest"):
                    if outer.release is None:
                        self.send_error(404)
                        return
                    body = json.dumps(outer.release).encode()
                else:
                    p = os.path.join(outer.root, os.path.basename(self.path))
                    if not os.path.isfile(p):
                        self.send_error(404)
                        return
                    with open(p, "rb") as f:
                        body = f.read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


@unittest.skipUnless(shutil.which("cc"), "needs a C compiler for the pretend AppImage")
class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name
        self.srv = Server(self.t)
        os.environ["XLTTOOL_UPDATE_API"] = self.srv.url
        os.environ["XLTTOOL_UPDATE_REPO"] = "someone/xlt-tool"

    def tearDown(self):
        self.srv.stop()
        for k in ("XLTTOOL_UPDATE_API", "XLTTOOL_UPDATE_REPO"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def release(self, version: str, says: str, digest: bool = True, size_off: int = 0):
        name = f"XLT_Tool-{version}-x86_64.AppImage"
        make_program(os.path.join(self.t, name), says)
        with open(os.path.join(self.t, name), "rb") as f:
            data = f.read()
        self.srv.release = {"tag_name": f"v{version}", "body": "## New\n- **Faster** [link](http://x)\n",
                            "html_url": "https://example/rel", "assets": [
                                {"name": "notes.txt", "browser_download_url": self.srv.url + "/notes.txt"},
                                {"name": name, "size": len(data) + size_off,
                                 "browser_download_url": f"{self.srv.url}/{name}",
                                 "digest": "sha256:" + hashlib.sha256(data).hexdigest() if digest else None}]}

    def test_versions(self):
        c = update.version_cmp
        self.assertGreater(c("1.10.0", "1.9.2"), 0)
        self.assertEqual(c("v1.1", "1.1.0"), 0)
        self.assertLess(c("1.1.0-beta", "1.1.0"), 0)
        self.assertLess(c("1.0.0", "1.0.1"), 0)

    def test_no_release_yet(self):
        self.assertIsNone(update.latest())
        self.assertFalse(update.newer(None))

    def test_check_download_install(self):
        self.release("1.2.0", "1.2.0")
        rel = update.latest()
        self.assertEqual((rel.version, rel.asset.endswith(".AppImage"), len(rel.sha256)), ("1.2.0", True, 64))
        self.assertTrue(update.newer(rel, "1.0.0"))
        self.assertFalse(update.newer(rel, "1.2.0"))
        self.assertEqual(update.plain_notes(rel.notes), "New\n- Faster link")
        target = os.path.join(self.t, "XLT_Tool.AppImage")
        with open(target, "wb") as f:
            f.write(b"old")
        part = update.download(rel, target)
        update.install(part, target)
        self.assertEqual(subprocess.run([target], capture_output=True, text=True).stdout.strip(), "1.2.0")
        self.assertFalse(os.path.exists(part))

    def test_checks_refuse(self):
        target = os.path.join(self.t, "XLT_Tool.AppImage")
        self.release("1.3.0", "1.2.9")  # says another version
        with self.assertRaises(RuntimeError):
            update.download(update.latest(), target)
        self.release("1.3.0", "1.3.0", size_off=5)  # not the size the release says
        with self.assertRaises(RuntimeError):
            update.download(update.latest(), target)
        self.release("1.3.0", "1.3.0")
        rel = update.latest()
        rel.sha256 = "0" * 64  # not the file GitHub describes
        with self.assertRaises(RuntimeError):
            update.download(rel, target)
        self.assertFalse(os.path.exists(target + ".part"))
        self.release("1.3.0", "1.3.0", digest=False)  # no digest: the other checks still run
        self.assertTrue(update.download(update.latest(), target).endswith(".part"))

    def test_appimage(self):
        os.environ.pop("APPIMAGE", None)
        self.assertEqual(update.appimage(), "")
        p = os.path.join(self.t, "a.AppImage")
        open(p, "w").close()
        os.environ["APPIMAGE"] = p
        try:
            self.assertEqual(update.appimage(), p)
        finally:
            del os.environ["APPIMAGE"]


if __name__ == "__main__":
    unittest.main()
