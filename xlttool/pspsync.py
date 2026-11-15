"""The PSP over USB: XLT's USB sync (xltlink.py), from the app.

One worker thread owns the link: it looks for the PSP every couple of seconds,
says hello now and then while idle (XLT shows "PC connected" while it hears from
the PC), and sends the files of finished jobs, one job at a time, while the next
job converts. Everything it reports reaches the window as Qt signals.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import xltlink

LOOK_EVERY = 2.0      # s between looks for the PSP while there is none
HELLO_EVERY = 3.0     # s between hellos while idle (XLT's "PC connected" lasts 5 s)
POWER_EVERY = 15.0    # s between asking for the battery while idle


def _backend():
    """libusb from the AppImage (libusb-package) if there, else the system's."""
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:  # noqa: BLE001 - not bundled, or no usable library in it
        return None


class Upload:
    """A finished job's files to send: main is the converted file, the rest go
    with it (.THM, .srt, .lrc share its name on the PSP; book parts have their own)."""

    def __init__(self, job, main: str, files: list[str], folder: str, stage: str, thm_cmd: list[str] | None):
        self.job, self.main, self.files, self.folder, self.stage, self.thm_cmd = job, main, files, folder, stage, thm_cmd
        self.cancel = threading.Event()


class Task:
    """Work for the link other than sending a job's files: fn(link, task) runs on the
    worker, may call task.report(fraction, text) and should check task.cancelled().
    Its result or error ("" = fine, "cancelled") comes back with PspSync.task_done."""

    def __init__(self, name: str, fn, data=None):
        self.name, self.fn, self.data = name, fn, data
        self.cancel = threading.Event()
        self.result = None
        self.error = ""
        self._sync = None

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def report(self, fraction: float, text: str = ""):
        if self._sync:
            self._sync.task_progress.emit(self, fraction, text)


class PspSync(QObject):
    state = Signal(str, str)           # "none" | "ready" | "no_access" | "error", text
    progress = Signal(object, float)   # job, 0..1
    sent = Signal(object, str, list)   # job, error ("" = ok, "cancelled"), the PSP paths written
    drained = Signal()                 # the queue is empty again
    task_progress = Signal(object, float, str)  # Task, 0..1, text
    task_done = Signal(object)                  # Task (its result or error set)
    power = Signal(object)                      # {battery, charging, plugged, minutes}, or None (gone)

    def __init__(self):
        super().__init__()
        self._q: queue.Queue = queue.Queue()
        self._pending = 0
        self._tasks = 0
        self._lock = threading.Lock()
        self._current: Upload | None = None
        self.status, self.text = "none", ""
        self.model = ""
        self.power_info: dict | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="psp-sync", daemon=True)
        self._thread.start()

    # -- from the window
    def ready(self) -> bool:
        return self.status == "ready"

    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    def tasks_busy(self) -> bool:
        with self._lock:
            return self._tasks > 0

    def run_task(self, task: Task):
        task._sync = self
        with self._lock:
            self._tasks += 1
        self._q.put(task)

    def send(self, up: Upload):
        with self._lock:
            self._pending += 1
        self._q.put(up)

    def cancel_all(self, tasks: bool = False):
        """Cancels the uploads (and, with tasks, the other work too)."""
        cur = self._current
        if cur and (tasks or isinstance(cur, Upload)):
            cur.cancel.set()
        drained, keep = [], []
        try:
            while True:
                drained.append(self._q.get_nowait())
        except queue.Empty:
            pass
        for up in drained:
            if isinstance(up, Task):
                if tasks:
                    self._task_finish(up, "cancelled")
                else:
                    keep.append(up)
            elif up is not None:
                self._finish(up, "cancelled", [])
        for t in keep:
            self._q.put(t)

    def _task_finish(self, task: Task, err: str):
        task.error = err
        with self._lock:
            self._tasks -= 1
        self.task_done.emit(task)

    def stop(self):
        self._stop.set()
        self.cancel_all(tasks=True)
        self._q.put(None)

    # -- the worker
    def _set(self, status: str, text: str = ""):
        if (status, text) != (self.status, self.text):
            self.status, self.text = status, text
            try:
                self.state.emit(status, text)
            except RuntimeError:  # the window is gone (quitting): nobody to tell
                pass

    def _finish(self, up: Upload, err: str, written: list[str]):
        shutil.rmtree(up.stage, ignore_errors=True)
        self.sent.emit(up.job, err, written)
        with self._lock:
            self._pending -= 1
            empty = self._pending == 0
        if empty:
            self.drained.emit()

    def _power(self, link):
        try:
            self.power_info = link.power()
        except xltlink.XltsError:
            self.power_info = None  # an XLT without POWER
        try:
            self.power.emit(self.power_info)
        except RuntimeError:
            pass

    def _run(self):
        link, backend, last, last_power = None, _backend(), 0.0, 0.0
        while not self._stop.is_set():
            if link is None:
                try:
                    link = xltlink.Link(backend)
                    h = link.info
                    self.model = xltlink.MODELS.get(h["model"], "PSP")
                    self._power(link)
                    last_power = time.monotonic()
                    self._set("ready", self.model)
                except xltlink.LinkError as e:
                    text = str(e)
                    self._set("no_access" if "ermission" in text else "none" if "No XLT" in text else "error", text)
                    link = None
                except Exception as e:  # noqa: BLE001 - usb errors while opening: try again later
                    self._set("error", str(e))
                    link = None
            try:
                up = self._q.get(timeout=LOOK_EVERY if link is None else HELLO_EVERY)
            except queue.Empty:
                up = False
            if up is None:
                break
            if up is False:  # nothing to send: a hello keeps XLT's "PC connected" on
                if link is not None and time.monotonic() - last >= HELLO_EVERY - 0.1:
                    try:
                        if time.monotonic() - last_power >= POWER_EVERY:
                            self._power(link)  # (counts as talking to XLT too)
                            last_power = time.monotonic()
                        else:
                            link.hello()
                        last = time.monotonic()
                    except Exception:  # noqa: BLE001 - unplugged, or XLT closed
                        link = None
                        self.power_info = None
                        self._set("none")
                continue
            if isinstance(up, Task):
                if link is None:
                    self._task_finish(up, "The PSP isn't connected (XLT with USB sync on, and the cable)")
                    continue
                self._current = up
                try:
                    up.result = up.fn(link, up)
                    self._task_finish(up, "")
                except InterruptedError:
                    self._task_finish(up, "cancelled")
                    link = None  # stopped in the middle of a transfer: start over after the PSP's timeout
                    time.sleep(3.5)
                except xltlink.XltsError as e:
                    self._task_finish(up, f"The PSP said no: {e}")
                except Exception as e:  # noqa: BLE001 - the link went: the next look finds out
                    self._task_finish(up, f"The PSP stopped answering: {e}")
                    link = None
                    self._set("none")
                finally:
                    self._current = None
                    last = time.monotonic()
                continue
            if link is None:
                self._finish(up, "The PSP isn't connected (XLT with USB sync on, and the cable)", [])
                continue
            self._current = up
            try:
                written = self._send(link, up)
                self._finish(up, "", written)
            except InterruptedError:
                self._finish(up, "cancelled", [])
                # stopped in the middle of a file: the PSP waits for the rest until its
                # 3 s timeout, so the link starts over after that
                link = None
                time.sleep(3.5)
            except xltlink.XltsError as e:
                self._finish(up, f"The PSP said no: {e}", [])
            except Exception as e:  # noqa: BLE001 - the link went: the next look finds out
                self._finish(up, f"Sending to the PSP failed: {e}", [])
                link = None
                self._set("none")
            finally:
                self._current = None
                last = time.monotonic()
        try:
            if link is not None:
                import usb.util
                usb.util.dispose_resources(link.dev)
        except Exception:  # noqa: BLE001
            pass

    def _send(self, link: xltlink.Link, up: Upload) -> list[str]:
        if up.thm_cmd:  # the XMB thumbnail, made now so it goes with the video
            try:
                subprocess.run(up.thm_cmd, timeout=60, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                pass
            thm = os.path.splitext(up.main)[0] + ".THM"
            if os.path.exists(thm) and thm not in up.files:
                up.files.append(thm)
        dev = "ms0:" if link.info["devices"] & 1 else "ef0:"
        folder = f"{dev}/{up.folder}"
        link.mkdir(folder)
        stem = os.path.splitext(os.path.basename(up.main))[0]
        mates = [f for f in up.files if os.path.splitext(os.path.basename(f))[0] == stem]
        others = [f for f in up.files if f not in mates]

        # a game's EBOOT keeps its name: a game already there is left alone
        if os.path.basename(up.main).upper() == "EBOOT.PBP":
            target = f"{folder}/EBOOT.PBP"
            if link.stat(target) is not None:
                raise xltlink.XltsError(xltlink.E_EXIST, 0, f"{up.folder} (a game of that ID is on the PSP)")
            plan = [(up.main, target)]
            total = os.path.getsize(up.main) or 1

            def step1(sent, size):
                if up.cancel.is_set():
                    raise InterruptedError
                self.progress.emit(up.job, sent / total)
            link.put(up.main, target, progress=step1)
            return [xltlink.psp_shown(target)]

        # one free name for the file and its companions: "Name", "Name 2", ...
        plan: list[tuple[str, str]] = []
        for n in range(1, 1000):
            s = stem if n == 1 else f"{stem} {n}"
            names = [s + os.path.splitext(f)[1] for f in mates]
            if all(link.stat(f"{folder}/{x}") is None for x in names):
                plan += [(f, f"{folder}/{x}") for f, x in zip(mates, names)]
                break
        else:
            raise xltlink.XltsError(xltlink.E_EXIST, 0, f"{folder}/{stem}")
        for f in others:
            base, ext = os.path.splitext(os.path.basename(f))
            for n in range(1, 1000):
                x = base + ext if n == 1 else f"{base} {n}{ext}"
                if link.stat(f"{folder}/{x}") is None:
                    plan.append((f, f"{folder}/{x}"))
                    break

        total = sum(os.path.getsize(f) for f, _ in plan) or 1
        done, written = 0, []
        for local, remote in plan:
            if up.cancel.is_set():
                raise InterruptedError

            def step(sent, size, base=done):
                if up.cancel.is_set():
                    raise InterruptedError
                self.progress.emit(up.job, (base + sent) / total)

            link.put(local, remote, progress=step)
            done += os.path.getsize(local)
            written.append(xltlink.psp_shown(remote))
        return written
