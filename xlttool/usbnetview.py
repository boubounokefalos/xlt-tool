"""The PSP's internet over the cable, from the window: USBNet's gateway run by XLT Tool.

While it is on, XLT Tool fetches USBNet (usbnet.py: the latest release, or the copy kept),
leaves the PSP's plugin in XLT's folder when the PSP is on sync and hasn't got this version,
and runs the gateway (pspkit-usbnetd) for as long as it stays on. The gateway waits for the
PSP and finds it again after each restart by itself. It runs out of sight and its log tells
the button how far it is. On Windows, only when it has no access to the PSP's USBNet device
(its WinUSB driver not installed yet) does it start again in a window of its own, where it
installs the driver and Windows asks first.
"""
from __future__ import annotations

import collections
import hashlib
import os
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QProcess, Signal

from . import games, usbnet, xltlink
from .pspsync import Task

STATS = ["-v", "--stats"]


class UsbNet(QObject):
    """state(kind, text): kind "off", "starting", "waiting", "online", "no_access", "busy" or "error".
    line(text): a log line. staged(text): what was done with the PSP's plugin ("" = nothing)."""
    state = Signal(str, str)
    line = Signal(str)
    staged = Signal(str)
    _fetched = Signal(object, str)  # Kept or None, error
    _exited = Signal(object, int)   # Windows: the gateway's Popen, its exit code

    def __init__(self, parent=None, psp=None):
        super().__init__(parent)
        self.psp = psp
        self.lines = collections.deque(maxlen=400)
        self.kind, self.text = "off", ""
        self.kept: usbnet.Kept | None = None
        self._proc: QProcess | subprocess.Popen | None = None
        self._want = False
        self._buf = b""
        self._staged_for = ""   # the tag whose plugin was left on the PSP in this run
        self._fetched.connect(self._got)
        self._exited.connect(self._console_ended)
        if psp is not None:
            psp.state.connect(lambda status, _t: status == "ready" and self._want and self.offer_plugin())

    def running(self) -> bool:
        return self._want

    # -- the window's side

    def start(self):
        if self._want:
            return
        self._want = True
        self._set("starting", "getting USBNet")
        threading.Thread(target=self._fetch, name="usbnet-fetch", daemon=True).start()

    def stop(self):
        self._want = False
        p = self._proc
        self._proc = None
        if isinstance(p, subprocess.Popen):
            # Windows: no console handler to ask nicely; its waiter thread ends with it
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                pass
        elif p is not None:
            p.finished.disconnect()
            if sys.platform == "win32":
                p.kill()  # terminate() sends WM_CLOSE, which a program without a window never sees
            else:
                p.terminate()
            if not p.waitForFinished(3000):
                p.kill()
                p.waitForFinished(1000)
        self._set("off", "")

    # -- the worker

    def _log(self, text: str):
        line = time.strftime("%H:%M:%S ") + text
        self.lines.append(line)
        self.line.emit(text)
        try:  # also in a file, for when something has to be looked into (cache/usbnet.log)
            path = os.path.join(games.cache_dir(), "usbnet.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
                os.replace(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass

    def _set(self, kind: str, text: str):
        self.kind, self.text = kind, text
        self.state.emit(kind, text)

    def _fetch(self):
        try:
            k = usbnet.fetch(self._log)
            self._fetched.emit(k, "")
        except Exception as e:  # noqa: BLE001 - shown on the button
            self._fetched.emit(None, str(e))

    def _got(self, k, err: str):
        if not self._want:
            return
        if k is None:
            self._log(err)
            self._want = False
            self._set("error", err)
            return
        self.kept = k
        self._log(f"USBNet {k.version}: {k.gateway}")
        self.offer_plugin()
        self._run_gateway()

    def _run_gateway(self):
        """Out of sight, its log read for the button (on Windows too: QProcess gives a console
        program no window when XLT Tool has no console of its own)."""
        p = QProcess(self)
        p.setProgram(self.kept.gateway)
        p.setArguments(STATS)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyRead.connect(lambda: self._read(p))
        p.finished.connect(lambda code, _status: self._ended(p, code))
        p.errorOccurred.connect(lambda e: e == QProcess.ProcessError.FailedToStart and self._ended(p, -1))
        self._proc = p
        self._buf = b""
        p.start()
        self._set("waiting", "waiting for the PSP")

    def _to_console(self):
        """Windows, the PSP's USBNet device found without the WinUSB driver: the gateway out of
        sight can't set it up, so it starts again in a window of its own, where it installs the
        driver (Windows asks) and then serves the PSP as usual."""
        p = self._proc
        self._proc = None
        if isinstance(p, QProcess):
            p.finished.disconnect()
            p.kill()
            p.waitForFinished(1000)
        self._log("no access to the PSP's USBNet device: the gateway opens its own window to install its driver")
        self._run_gateway_console()

    def _run_gateway_console(self):
        """The gateway in a console of its own (see _to_console). Started with subprocess, not
        QProcess: PySide6 has no setCreateProcessArgumentsModifier, so QProcess can't give it one."""
        try:
            p = subprocess.Popen([self.kept.gateway, "--stats"], creationflags=CREATE_NEW_CONSOLE)
        except OSError as e:
            self._proc = None
            self._want = False
            text = f"the gateway didn't start ({e})"
            self._log(text)
            self._set("error", text)
            return
        self._proc = p
        self._log(f"gateway started in its own window (pid {p.pid})")
        threading.Thread(target=lambda: self._exited.emit(p, p.wait()), name="usbnet-wait", daemon=True).start()
        self._set("waiting", "running in its own window")

    def _console_ended(self, p, code: int):
        if p is not self._proc:
            return
        self._proc = None
        self._want = False
        if code == 1:
            text = "the gateway stopped (exit 1: another USBNet gateway is running, or it hit an error)"
        else:
            text = f"the gateway's window was closed (exit {code})"
        self._log(text)
        self._set("error" if code == 1 else "off", text if code == 1 else "")

    def _read(self, p: QProcess):
        self._buf += bytes(p.readAll())
        *done, self._buf = self._buf.split(b"\n")
        for raw in done:
            text = raw.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self._log(text)
            said = usbnet.read_line(text)
            if said is None:
                continue
            kind, words = said
            if kind == "no_access" and sys.platform == "win32":
                self._to_console()
                return
            if kind == "stats":
                if self.kind == "online":
                    self._set("online", words)
            else:
                self._set(kind, words)

    def _ended(self, p: QProcess, code: int):
        if p is not self._proc:
            return
        self._proc = None
        self._want = False
        last = self.lines[-1][9:] if self.lines else ""
        if code == 1 and "already running" in last:
            text = "another USBNet gateway is running on this computer"
        else:
            text = f"the gateway stopped ({last or f'exit {code}'})"
        self._log(text)
        self._set("error", text)

    # -- the PSP's plugin

    def offer_plugin(self):
        """When the PSP is on sync: if USBNet isn't installed there (PSPDX's folder or the
        README's file), leaves this release's plugin files in XLT's folder for XLT to install.
        An installed USBNet, whoever put it there and whichever version, is left alone."""
        k = self.kept
        if k is None or self.psp is None or not self.psp.ready() or self._staged_for == k.tag:
            return
        self._staged_for = k.tag
        want = usbnet.sha256_file(k.prx)
        staged_prx = f"{usbnet.STAGED_DIR}/{usbnet.PRX}"

        def run(link: xltlink.Link, task: Task):
            for path in usbnet.INSTALLED:
                if link.stat(path) is not None:
                    return f"USBNet is installed on the PSP already ({path}): left as it is"
            if link.stat(staged_prx) is not None and hashlib.sha256(link.get(staged_prx)).hexdigest() == want:
                return ""  # waiting for XLT already
            try:
                link.mkdir(usbnet.STAGED_DIR)
            except xltlink.XltsError:
                pass  # there already
            for f in k.psp_files:
                link.put(f, f"{usbnet.STAGED_DIR}/{os.path.basename(f)}", replace=True)
            return (f"USBNet {k.version} is on the PSP, for XLT to install. On the PSP: XLT > Settings > "
                    "Network > Internet over USB on, restart XLT (it installs USBNet), then restart the PSP.")

        task = Task("usbnet-plugin", run)
        self.psp.task_done.connect(self._task_done)
        self.psp.run_task(task)

    def _task_done(self, task: Task):
        if task.name != "usbnet-plugin":
            return
        self.psp.task_done.disconnect(self._task_done)
        if task.error:
            self._staged_for = ""
            self._log(f"the PSP's plugin wasn't sent: {task.error}")
            return
        if task.result.startswith("USBNet is installed"):
            self._log(task.result)
        elif task.result:
            self._log(task.result)
            self.staged.emit(task.result)


CREATE_NEW_CONSOLE = 0x00000010  # subprocess.CREATE_NEW_CONSOLE, which exists only on Windows
