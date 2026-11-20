from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QObject, QProcess, QRunnable, QSettings, QSize, Qt,
                            QThreadPool, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QDesktopServices, QIcon, QImage, QKeySequence, QPainter, QPalette,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTabBar,
                               QToolButton, QVBoxLayout, QWidget)

from . import __version__, convert, devices, games, media, update
from .gamesview import GamesView
from . import xltlink
from .pspsync import PspSync, Upload
from .tagdialog import TagDialog

APP_NAME = "XLT Tool"
VIEW_LIBRARY, VIEW_CONVERT = 0, 1   # the tabs at the top

# which settings tab each kind of job belongs to
KIND_TAB = {"video": 0, "audio": 1, "image": 2, "text": 3, "sub": 3, "archive": 3, "lyrics": 1,
            "ps1": 3, "pspgame": 3}
KIND_ICON = {"audio": ("audio-x-generic", "audio-x-generic-symbolic"),
             "text": ("text-x-generic", "x-office-document"),
             "sub": ("text-x-generic", "x-office-document"),
             "lyrics": ("text-x-generic", "x-office-document"),
             "archive": ("package-x-generic", "application-zip", "folder"),
             "ps1": ("media-optical", "drive-optical", "applications-games"),
             "pspgame": ("applications-games", "input-gaming", "media-optical")}


def _luma(r: float, g: float, b: float) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _readable(ic: QIcon) -> QIcon:
    """Symbolic icons are drawn in one fixed colour and the desktop normally
    recolours them, which a bundled Qt can't ask it to do. So Adwaita's come out
    dark on a dark window. An icon too close to the window colour is repainted in
    the text colour; icons with colours of their own are left alone."""
    palette = QApplication.palette()
    image = ic.pixmap(24, 24).toImage().convertToFormat(QImage.Format.Format_ARGB32)
    total = weight = 0.0
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            total += _luma(c.redF(), c.greenF(), c.blueF()) * c.alphaF()
            weight += c.alphaF()
    if not weight:
        return ic
    back = palette.color(QPalette.ColorRole.Window)
    if abs(total / weight - _luma(back.redF(), back.greenF(), back.blueF())) >= 0.25:
        return ic
    tinted = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        pm = ic.pixmap(size, size)
        painter = QPainter(pm)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pm.rect(), palette.color(QPalette.ColorRole.WindowText))
        painter.end()
        tinted.addPixmap(pm)
    return tinted


def icon(*names: str) -> QIcon:
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return _readable(ic)
    return QIcon()


def set_icon(button: QAbstractButton, *names: str) -> None:
    """Sets a button's icon and remembers the names, to redo it if the theme changes."""
    button.setIcon(icon(*names))
    button.setProperty("iconNames", list(names))


# ------------------------------------------------------------------ jobs ----

class Job:
    def __init__(self, path: str, kind: str | None = None):
        self.path = path
        self.kind = kind or convert.kind_of(path)
        self.info = None         # what probing found; its type depends on kind
        self.outputs: list[str] = []
        self.sidecar = ""        # a finished .srt waiting to go next to the video
        self.edit = convert.TagEdit()   # the user's changes to a song
        self.work_label = "Copying to Memory Stick"
        self.audio: media.Track | None = None
        self.sub = media.SubChoice()
        self.sub_set_by_user = False
        self.state = "probing"   # probing ready queued running done error cancelled
        self.progress = 0.0
        self.eta = ""
        self.output = ""
        self.error = ""
        self.item: QListWidgetItem | None = None
        self.row: JobRow | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name


class FitScroll(QScrollArea):
    """A page whose wrapped labels always get the height their text needs: on a narrow
    card (or with a large font) it scrolls instead of squeezing them together."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.inner = QWidget()
        self.inner_layout = QVBoxLayout(self.inner)
        self.setWidget(self.inner)

    def _fit(self):
        w = self.viewport().width()
        lay = self.inner_layout
        h = lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.sizeHint().height()
        self.inner.setMinimumHeight(max(h, 0))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._fit)


class UpdateSignals(QObject):
    checked = Signal(object, str, bool)   # Release or None, error, asked by the user
    progress = Signal(float)
    done = Signal(str, str)               # version, error ("" = in place, "cancelled")


class CopySignals(QObject):
    progress = Signal(object, float)        # job, 0..1
    done = Signal(object, str)              # job, error ("" = ok, "cancelled")


def copy_to_stick(job, src: str, dst: str, signals: "CopySignals",
                  cancel: threading.Event):
    """Copy a finished file onto a (slow) memory stick with real progress:
    flush to the device every 32 MB so the bar tracks what is really written."""
    tmp = dst + ".part"
    try:
        total = os.path.getsize(src) or 1
        done = 0
        since_sync = 0
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                if cancel.is_set():
                    raise InterruptedError
                buf = fi.read(4 << 20)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                since_sync += len(buf)
                if since_sync >= 32 << 20:
                    fo.flush()
                    os.fsync(fo.fileno())
                    since_sync = 0
                signals.progress.emit(job, done / total)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dst)
        try:
            fd = os.open(os.path.dirname(dst), os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass
        signals.done.emit(job, "")
    except InterruptedError:
        MainWindow._remove_quiet(tmp)
        signals.done.emit(job, "cancelled")
    except OSError as e:
        MainWindow._remove_quiet(tmp)
        signals.done.emit(job, f"Copy to Memory Stick failed: {e.strerror or e}")


def python_job(job, out_dir, tmp_dir: str, signals: "CopySignals", cancel: threading.Event):
    """Text, subtitles and ZIPs: no ffmpeg run to watch, so a thread does them."""
    def progress(frac: float):
        signals.progress.emit(job, frac)
    try:
        if job.kind == "text":
            outputs = convert.convert_text(job.path, out_dir, progress, cancel)
        elif job.kind == "sub":
            outputs = convert.convert_subtitle(job.path, out_dir, tmp_dir, progress, cancel)
        elif job.kind == "lyrics":
            outputs = convert.convert_lyrics(job.path, out_dir, progress, cancel)
        elif job.kind == "audio":
            outputs = convert.save_in_place(job.info, job.edit, progress, cancel)
        elif job.kind in ("ps1", "pspgame"):
            outputs = convert.convert_game(job.info, job.kind, out_dir, progress, cancel)
            if job.kind == "ps1" and getattr(job, "psp_stage", ""):
                job.psp_folder = f"{convert.FOLDERS['ps1']}/{job.info.game_id}"  # the game's own folder
        else:
            outputs = convert.make_zip(job.path, job.info, out_dir, progress, cancel)
        job.outputs, job.output = outputs, outputs[0]
        signals.done.emit(job, "")
    except InterruptedError:
        signals.done.emit(job, "cancelled")
    except Exception as e:              # noqa: BLE001 - damaged files; reported in the UI
        signals.done.emit(job, f"Couldn't convert this: {e}")


def tag_and_deliver(job, part: str, output: str, copy_to: str, signals: "CopySignals",
                    cancel: threading.Event):
    """After ffmpeg: write the song's tags, then put the file where it belongs."""
    try:
        convert.apply_tags(part, os.path.splitext(output)[1], job.info, job.edit)
        if copy_to:
            copy_to_stick(job, part, copy_to, signals, cancel)
            return
        os.replace(part, output)
        signals.done.emit(job, "")
    except Exception as e:              # noqa: BLE001 - reported in the UI
        signals.done.emit(job, f"Couldn't write the tags: {e}")


def probe_job(job):
    if job.kind == "audio":
        return convert.probe_audio(job.path)
    if job.kind == "image":
        return convert.probe_image(job.path)
    if job.kind in ("text", "sub", "lyrics"):
        return convert.probe_text(job.path)
    if job.kind == "archive":
        return convert.probe_archive(job.path)
    if job.kind == "ps1":
        return convert.probe_ps1(job.path, getattr(job, "discs", None))
    if job.kind == "pspgame":
        return convert.probe_pspgame(job.path)
    try:
        return media.probe(job.path)
    except RuntimeError as e:
        if "no video stream" not in str(e):
            raise
        job.kind = "audio"              # e.g. an .mp4 or .webm that only holds sound
        return convert.probe_audio(job.path)


def work_dir() -> str:
    d = os.path.join(games.cache_dir(), "work")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------- from PSP Video Converter --
# The app was called PSP Video Converter ("pspconv") up to 1.2. Its settings and
# its applications-menu entry are carried over once; its cache only held
# unfinished conversions and is dropped.

def open_settings() -> QSettings:
    s = QSettings("xlt-tool", "xlt-tool")
    if not s.contains("migrated_from_pspconv"):
        old = QSettings("pspconv", "pspconv")
        if not s.allKeys():
            for k in old.allKeys():
                s.setValue(k, old.value(k))
        s.setValue("migrated_from_pspconv", True)
        s.sync()
        shutil.rmtree(os.path.join(os.path.dirname(games.cache_dir()), "pspconv"), ignore_errors=True)
    return s


def _data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def remove_old_desktop_entry() -> bool:
    """Removes PSP Video Converter's menu entry and icon; True if there was one."""
    data = _data_home()
    entry = os.path.join(data, "applications", "pspconv.desktop")
    had = os.path.exists(entry)
    for p in (entry, os.path.join(data, "icons", "hicolor", "scalable", "apps", "pspconv.svg")):
        try:
            os.remove(p)
        except OSError:
            pass
    return had


class NvencSignals(QObject):
    result = Signal(bool)


class ProbeSignals(QObject):
    done = Signal(object, object, str)      # job, info, error


class ProbeTask(QRunnable):
    def __init__(self, job: Job, signals: ProbeSignals):
        super().__init__()
        self.job, self.signals = job, signals

    def run(self):
        try:
            self.signals.done.emit(self.job, probe_job(self.job), "")
        except Exception as e:          # noqa: BLE001 - reported in the UI
            self.signals.done.emit(self.job, None, str(e))


class ElidedLabel(QLabel):
    """Single-line label that shortens with "…" instead of clipping."""

    def __init__(self, text: str = ""):
        super().__init__()
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text: str):
        self._full = text
        self.setToolTip(text if text else "")
        self._elide()

    def text(self) -> str:
        return self._full

    def _elide(self):
        fm = self.fontMetrics()
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                      max(10, self.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (e.Type.FontChange, e.Type.StyleChange):
            self._elide()


class JobRow(QWidget):
    remove_clicked = Signal()

    def __init__(self, job: Job):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 6, 6)
        lay.setSpacing(10)
        self.poster = QLabel()
        self.poster.setFixedSize(96, 54)
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet("background: rgba(0,0,0,0.35); border-radius: 6px;")
        lay.addWidget(self.poster)

        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = ElidedLabel(job.name)
        self.title.setObjectName("heading")
        self.detail = ElidedLabel("Reading…")
        self.detail.setObjectName("muted")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.status = ElidedLabel("")
        self.status.setObjectName("muted")
        col.addWidget(self.title)
        col.addWidget(self.detail)
        col.addWidget(self.bar)
        col.addWidget(self.status)
        lay.addLayout(col, 1)

        self.remove = QToolButton()
        self.remove.setObjectName("flat")
        set_icon(self.remove, "window-close-symbolic", "window-close", "edit-delete")
        if self.remove.icon().isNull():
            self.remove.setText("✕")
        self.remove.setToolTip("Remove from list (Delete)")
        self.remove.clicked.connect(self.remove_clicked)
        lay.addWidget(self.remove, 0, Qt.AlignmentFlag.AlignTop)

    def set_status(self, text: str, kind: str = "muted"):
        self.status.setText(text)
        if self.status.objectName() != kind:
            self.status.setObjectName(kind)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)


# ---------------------------------------------------------------- window ----

class MainWindow(QMainWindow):
    def __init__(self, files: list[str] | None = None):
        super().__init__()
        self._icons_for = QApplication.palette().color(QPalette.ColorRole.Window).rgb()
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.settings = open_settings()
        self.jobs: list[Job] = []
        self.pool = QThreadPool.globalInstance()
        self.probe_signals = ProbeSignals()
        self.probe_signals.done.connect(self._probed)
        self.proc: QProcess | None = None
        self.current: Job | None = None
        self.running = False
        self.stop_requested = False
        self.tmpdir: str | None = None
        self.custom_dir = self.settings.value("custom_dir", "", str)
        self._sticks: list[devices.Stick] = []
        self._poster_procs: set[QProcess] = set()

        self._build_ui()
        self._load_settings()
        self.refresh_destinations()
        self._stick_timer = QTimer(self, interval=3000)
        self._stick_timer.timeout.connect(self.refresh_destinations)
        self._stick_timer.start()
        self.resize(self.settings.value("size", QSize(980, 620)))
        self._update_music_hint()
        if files:
            QTimer.singleShot(0, lambda: self.add_paths(files))
        self._check_ffmpeg()
        self.copy_signals = CopySignals()
        self.copy_signals.progress.connect(self._copy_progress)
        self.copy_signals.done.connect(self._copy_done)
        self.copy_cancel = threading.Event()
        self.copy_thread: threading.Thread | None = None
        self._clean_work_dir()
        self.migrate_desktop_entry()
        # the PSP over USB (XLT's USB sync): looked for in the background
        self.psp = PspSync()
        self.psp.state.connect(self._psp_state)
        self.psp.progress.connect(self._psp_progress)
        self.psp.sent.connect(self._psp_sent)
        self.psp.drained.connect(self._psp_drained)
        self.psp.power.connect(lambda _p: self.refresh_destinations())
        self.games_view.attach_psp(self.psp)
        self._psp_was_ready = False
        self.update_signals = UpdateSignals()
        self.update_signals.checked.connect(self._update_checked)
        self.update_signals.progress.connect(
            lambda f: self._update_progress and self._update_progress.setValue(int(f * 1000)))
        self.update_signals.done.connect(self._update_done)
        self._update_progress = None
        self._restart_after = ""
        QTimer.singleShot(4000, self.check_updates)
        self.nvenc_signals = NvencSignals()
        self.nvenc_signals.result.connect(self._nvenc_result)
        threading.Thread(target=lambda: self.nvenc_signals.result.emit(media.nvenc_available()),
                         daemon=True).start()
        if self.intro_action.isChecked() and not os.environ.get("XLTTOOL_NO_INTRO"):
            self._intro()

    def _intro(self):
        """XLT's boot screen over the window, waiting a little for the Library's first look."""
        from . import brand
        gv = self.games_view
        overlay = brand.BootOverlay(self, busy=lambda: gv._scanning)
        gv.signals.progress.connect(overlay.progress)
        gv.signals.done.connect(overlay.done)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # top bar: the view (converting, or the game library), then the view's buttons
        top = QHBoxLayout()
        self.view_tabs = QTabBar()
        self.view_tabs.setDrawBase(False)
        self.view_tabs.addTab("Library")   # VIEW_LIBRARY
        self.view_tabs.addTab("Convert")   # VIEW_CONVERT
        top.addWidget(self.view_tabs)
        top.addSpacing(12)
        self.add_btn = QPushButton("Add files")
        set_icon(self.add_btn, "list-add-symbolic", "list-add")
        self.add_btn.clicked.connect(self.pick_files)
        self.folder_btn = QPushButton("Add folder")
        set_icon(self.folder_btn, "folder-open-symbolic", "folder-open")
        self.folder_btn.clicked.connect(self.pick_folder)
        self.zip_btn = QPushButton("Make ZIP")
        set_icon(self.zip_btn, "package-x-generic-symbolic", "package-x-generic")
        self.zip_btn.setToolTip("Pack a folder into a ZIP that XLT's Files can extract on the PSP.")
        self.zip_btn.clicked.connect(self.pick_zip_folder)
        self.clear_btn = QPushButton("Clear finished")
        set_icon(self.clear_btn, "edit-clear-all-symbolic", "edit-clear-all")
        self.clear_btn.clicked.connect(self.clear_finished)
        top.addWidget(self.add_btn)
        top.addWidget(self.folder_btn)
        top.addWidget(self.zip_btn)
        top.addWidget(self.clear_btn)
        top.addStretch(1)
        self.cast_btn = QPushButton("Cast")
        set_icon(self.cast_btn, "video-display-symbolic", "video-display", "computer")
        self.cast_btn.setToolTip("XLT Cast: the game you play on the PSP, live in a window. On the PSP: "
                                 "XLT > Settings > XLT > XLT Cast on, then L+R+SELECT in a game")
        self.cast_btn.clicked.connect(self.open_cast)
        top.addWidget(self.cast_btn)
        menu_btn = QToolButton()
        set_icon(menu_btn, "open-menu-symbolic", "application-menu", "open-menu")
        if menu_btn.icon().isNull():
            menu_btn.setText("☰")
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(menu_btn)
        self.integrate_action = QAction("Add to applications menu", self)
        self.integrate_action.triggered.connect(self.integrate_desktop)
        menu.addAction(self.integrate_action)
        menu.addSeparator()
        check_now = QAction("Check for updates", self)
        check_now.triggered.connect(lambda: self.check_updates(manual=True))
        menu.addAction(check_now)
        self.auto_update_action = QAction("Check for updates at start", self, checkable=True)
        self.auto_update_action.setChecked(str(self.settings.value("update_check", "true")).lower() == "true")
        self.auto_update_action.toggled.connect(
            lambda on: self.settings.setValue("update_check", "true" if on else "false"))
        menu.addAction(self.auto_update_action)
        menu.addSeparator()
        self.art_action = QAction("Box art for games", self, checkable=True)
        self.art_action.setToolTip("Covers for PSP and PS1 games from the libretro project, in the Library "
                                   "and as icons of PS1 EBOOTs made here (downloaded)")
        menu.addAction(self.art_action)
        self.intro_action = QAction("Start-up animation", self, checkable=True)
        self.intro_action.setChecked(str(self.settings.value("intro", "true")).lower() == "true")
        self.intro_action.toggled.connect(lambda on: self.settings.setValue("intro", "true" if on else "false"))
        menu.addAction(self.intro_action)
        menu.addSeparator()
        about = QAction(f"About {APP_NAME}", self)
        about.triggered.connect(self.about)
        menu.addAction(about)
        menu_btn.setMenu(menu)
        top.addWidget(menu_btn)
        outer.addLayout(top)

        # the Convert view: queue + settings, then where to save
        self.convert_page = QWidget()
        cv = QVBoxLayout(self.convert_page)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(10)
        mid = QHBoxLayout()
        mid.setSpacing(12)

        self.queue = QListWidget()
        self.queue.setObjectName("queue")
        self.queue.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.queue.itemSelectionChanged.connect(self._selection_changed)
        self.queue.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue.customContextMenuRequested.connect(self._queue_menu)
        # Delete (and Backspace on keyboards without Delete) removes the selected rows, as their ✕ does
        for key in (QKeySequence.StandardKey.Delete, QKeySequence(Qt.Key.Key_Backspace)):
            sc = QShortcut(key, self.queue, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self.remove_selected)
        self.queue.setAcceptDrops(False)
        self.empty = QLabel(
            "<div style='font-size:15pt; font-weight:600'>Drop files here</div>"
            "<div>Videos, music, pictures, books and subtitles, or use <b>Add files</b>.</div>")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setParent(self.queue.viewport())
        mid.addWidget(self.queue, 3)

        side = QFrame()
        side.setObjectName("card")
        side.setFixedWidth(330)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        self.kind_tabs = QTabBar()
        self.kind_tabs.setExpanding(True)
        self.kind_tabs.setDrawBase(False)
        for name in ("Video", "Music", "Pictures", "Other"):
            self.kind_tabs.addTab(name)
        sl.addWidget(self.kind_tabs)
        self.pages = QStackedWidget()
        self.kind_tabs.currentChanged.connect(self.pages.setCurrentIndex)
        self.kind_tabs.currentChanged.connect(lambda _i: self._selection_changed() if hasattr(self, "queue") else None)
        sl.addWidget(self.pages)
        video_page = QWidget()
        vp = QVBoxLayout(video_page)
        vp.setContentsMargins(0, 0, 0, 0)
        vp.setSpacing(8)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.res_box = QComboBox()
        self.res_box.addItem("480×272 · every PSP", "480x272")
        self.res_box.addItem("720×480 · PSP-2000/3000/Go", "720x480")
        self.res_box.setToolTip(
            "480×272 is the PSP's screen size and plays on every model.\n"
            "720×480 needs a PSP-2000 or later. Files are larger, and it looks no sharper on the PSP screen.")
        self.fit_box = QComboBox()
        self.fit_box.addItem("Fit · black bars", "fit")
        self.fit_box.addItem("Fill · crop edges", "fill")
        self.fit_box.addItem("Stretch", "stretch")
        self.q_box = QComboBox()
        self.q_box.addItem("Small files", "small")
        self.q_box.addItem("Balanced", "balanced")
        self.q_box.addItem("High quality", "high")
        self.speed_box = QComboBox()
        self.speed_box.addItem("Faster", "fast")
        self.speed_box.addItem("Balanced", "balanced")
        self.speed_box.addItem("Slower · smaller files", "slow")
        self.speed_box.setToolTip(
            "How hard the encoder works. Faster is about twice as quick as Balanced\n"
            "with a slightly larger file or slightly softer picture.")
        self.enc_box = QComboBox()
        self.enc_box.addItem("CPU · x264", "x264")
        self.enc_box.setToolTip(
            "x264 on the CPU is the proven PSP-compatible choice.\n"
            "NVIDIA NVENC is much faster but experimental: test a file on your PSP first.")
        form.addRow("Screen", self.res_box)
        form.addRow("Picture", self.fit_box)
        form.addRow("Quality", self.q_box)
        form.addRow("Speed", self.speed_box)
        form.addRow("Encoder", self.enc_box)
        self.subs_box = QComboBox()
        self.subs_box.addItem("Off", "off")
        self.subs_box.addItem("Burn into the picture", "burn")
        self.subs_box.addItem("Separate .srt for XLT", "srt")
        self.subs_box.setToolTip(
            "Burned-in subtitles show in every player but can't be turned off.\n"
            "A separate .srt shows in XLT only, where □ turns it on and off.\n"
            "Uses a matching .srt/.ass file next to the video first, then forced, default\n"
            "or first embedded subtitles. Change the track per video below.")
        form.addRow("Subtitles", self.subs_box)
        vp.addLayout(form)
        self.norm_chk = QCheckBox("Normalize volume")
        self.norm_chk.setToolTip("Evens out loudness so dialogue is audible on the PSP speakers.")
        self.thm_chk = QCheckBox("Create XMB thumbnail (.THM)")
        self.thm_chk.setToolTip("A 160×120 preview the PSP shows in the video list.")
        for w in (self.norm_chk, self.thm_chk):
            vp.addWidget(w)
        vp.addStretch(1)
        self.pages.addWidget(video_page)
        self._build_other_pages()
        for w in (self.res_box, self.fit_box, self.q_box, self.speed_box, self.enc_box,
                  self.music_fmt, self.music_q, self.pic_fmt, self.pic_size):
            w.currentIndexChanged.connect(self._options_changed)
        for w in (self.norm_chk, self.thm_chk, self.music_cover):
            w.toggled.connect(self._options_changed)
        self.subs_box.currentIndexChanged.connect(self._subs_mode_changed)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: palette(mid);")
        sl.addSpacing(4)
        sl.addWidget(line)
        sl.addSpacing(4)
        self.sel_heading = QLabel("Selected item")
        self.sel_heading.setObjectName("heading")
        sl.addWidget(self.sel_heading)
        sform = QFormLayout()
        sform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        sform.setHorizontalSpacing(10)
        self.sel_form = sform
        self.as_box = QComboBox()
        self.as_box.addItem("Video", "video")
        self.as_box.addItem("Music only", "audio")
        self.as_box.setToolTip("Keep just the soundtrack, as a music file.")
        self.as_box.currentIndexChanged.connect(self._as_picked)
        self.audio_box = QComboBox()
        self.audio_box.currentIndexChanged.connect(self._audio_picked)
        self.sub_box = QComboBox()
        self.sub_box.currentIndexChanged.connect(self._sub_picked)
        for b in (self.audio_box, self.sub_box):
            b.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            b.setMinimumContentsLength(10)
        sform.addRow("Convert to", self.as_box)
        sform.addRow("Audio", self.audio_box)
        sform.addRow("Subtitles", self.sub_box)
        sl.addLayout(sform)
        self.tags_btn = QPushButton("Edit tags, cover and lyrics…")
        self.tags_btn.clicked.connect(self.edit_tags)
        self.tags_btn.hide()
        sl.addWidget(self.tags_btn)
        self.combine_btn = QPushButton("Combine into one EBOOT")
        self.combine_btn.setToolTip("The selected PS1 discs are one game: make one EBOOT with all of them "
                                    "(up to 5). On the PSP, HOME → Reset game chooses the disc.")
        self.combine_btn.clicked.connect(self.combine_discs)
        self.combine_btn.hide()
        sl.addWidget(self.combine_btn)
        self.separate_btn = QPushButton("Separate the discs")
        self.separate_btn.setToolTip("Make one EBOOT for each disc instead.")
        self.separate_btn.clicked.connect(self.separate_discs)
        self.separate_btn.hide()
        sl.addWidget(self.separate_btn)
        self.sel_info = QLabel("")
        self.sel_info.setObjectName("muted")
        self.sel_info.setWordWrap(True)
        sl.addWidget(self.sel_info)
        sl.addStretch(1)
        mid.addWidget(side)
        cv.addLayout(mid, 1)

        # bottom bar
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(QLabel("Save to"))
        self.dest_box = QComboBox()
        self.dest_box.setMinimumWidth(300)
        self.dest_box.setToolTip("On a Memory Stick each kind goes where XLT looks for it: VIDEO, "
                                 "MUSIC, PICTURE,\nBOOKS for text, and DOWNLOADS for ZIPs.")
        self.dest_box.activated.connect(self._dest_activated)
        bottom.addWidget(self.dest_box, 1)
        self.open_btn = QToolButton()
        set_icon(self.open_btn, "folder-symbolic", "folder")
        if self.open_btn.icon().isNull():
            self.open_btn.setText("Open")
        self.open_btn.setToolTip("Open the destination folder")
        self.open_btn.clicked.connect(self.open_destination)
        bottom.addWidget(self.open_btn)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        bottom.addWidget(self.summary)
        self.go_btn = QPushButton("Convert")
        self.go_btn.setObjectName("primary")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self.toggle_run)
        bottom.addWidget(self.go_btn)
        cv.addLayout(bottom)

        self.views = QStackedWidget()
        self.games_view = GamesView(self.settings)
        self.art_action.setChecked(self.games_view.covers_on())
        self.art_action.toggled.connect(self.games_view._set_covers)
        self.games_view.covers_changed.connect(self._covers_changed)
        self.views.addWidget(self.games_view)      # VIEW_LIBRARY
        self.views.addWidget(self.convert_page)    # VIEW_CONVERT
        outer.addWidget(self.views, 1)
        self.view_tabs.currentChanged.connect(self._view_changed)
        name = self.settings.value("view_name", "")
        if not name:  # before Library came first: "view" was 0 Convert, 1 Games
            name = "library" if str(self.settings.value("view", 1)) == "1" else "convert"
        self.view_tabs.setCurrentIndex(VIEW_CONVERT if name == "convert" else VIEW_LIBRARY)
        self._view_changed(self.view_tabs.currentIndex())

        self.setCentralWidget(root)
        self._selection_changed()
        self._update_controls()

    def _covers_changed(self, on: bool):
        self.art_action.blockSignals(True)
        self.art_action.setChecked(on)
        self.art_action.blockSignals(False)

    def combine_discs(self):
        """The selected PS1 rows are one game's discs: one job for them all, in disc order."""
        sel = [j for j in self.selected_jobs() if j.kind == "ps1"]
        if not 2 <= len(sel) <= 5:
            return
        paths = sorted((j.path for j in sel), key=lambda p: (convert.disc_number(p) or 99, os.path.basename(p)))
        for j in sel:
            self.remove_job(j)
        job = Job(paths[0], "ps1")
        job.discs = paths
        self._add_job(job)
        self.queue.setCurrentItem(job.item)
        self._update_controls()

    def separate_discs(self):
        sel = self.selected_jobs()
        job = sel[0] if len(sel) == 1 else None
        if not job or not getattr(job, "discs", None):
            return
        paths = list(job.discs)
        self.remove_job(job)
        for p in paths:
            self._add_job(Job(p, "ps1"))
        self._update_controls()

    def _ensure_ps1_parts(self, multi: bool = False) -> bool:
        """DATA.PSP and STARTDAT (and, for a game of several discs, the PSTITLEIMG header)
        from a PS1 EBOOT of the user's (psx.take_parts): from the game library if it has
        one, else from a file the user picks."""
        from . import psx

        def enough() -> bool:
            return psx.have_parts() and (not multi or psx.have_title_part())
        if enough():
            return True
        for g in self.games_view.games:
            if g.kind == "ps1" and g.eboot:
                try:
                    psx.take_parts(g.eboot)
                    if enough():
                        return True
                except (OSError, ValueError):
                    continue
        QMessageBox.information(
            self, "A PS1 EBOOT of yours",
            "Every PS1 EBOOT carries a small program Sony made (DATA.PSP), which XLT Tool doesn't "
            "include. Pick a PS1 game's EBOOT.PBP you already have: XLT Tool keeps a copy of that part "
            "and uses it for the EBOOTs it makes.")
        while True:
            f, _ = QFileDialog.getOpenFileName(self, "A PS1 game's EBOOT.PBP", os.path.expanduser("~"),
                                               "EBOOT (*.PBP *.pbp);;All files (*)")
            if not f:
                return False
            try:
                psx.take_parts(f)
                if enough():
                    return True
                QMessageBox.warning(self, APP_NAME, "That one has no multi-disc header (PSTITLEIMG). For a game "
                                    "of several discs, pick a PS1 EBOOT made by pop-fe, or of several discs.")
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, APP_NAME, f"That one can't be used: {e}")

    def _view_changed(self, i: int):
        self.views.setCurrentIndex(i)
        self.settings.setValue("view_name", "convert" if i == VIEW_CONVERT else "library")
        for b in (self.add_btn, self.folder_btn, self.zip_btn, self.clear_btn):
            b.setVisible(i == VIEW_CONVERT)

    def _build_other_pages(self):
        music = QWidget()
        lay = QVBoxLayout(music)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.music_fmt = QComboBox()
        self.music_fmt.addItem("MP3 · plays everywhere", "mp3")
        self.music_fmt.addItem("M4A · AAC, smaller files", "m4a")
        self.music_fmt.addItem("FLAC · lossless, XLT only", "flac")
        self.music_fmt.setToolTip("Files already in the chosen format at a rate the PSP accepts\n"
                                  "are kept as they are, so they lose no quality.")
        self.music_q = QComboBox()
        self.music_q.addItem("Small files", "small")
        self.music_q.addItem("Balanced", "balanced")
        self.music_q.addItem("High quality", "high")
        form.addRow("Format", self.music_fmt)
        form.addRow("Quality", self.music_q)
        lay.addLayout(form)
        self.music_cover = QCheckBox("Keep the album cover")
        lay.addWidget(self.music_cover)
        self.music_hint = QLabel("The PSP has no FLAC hardware, so FLAC needs XLT and uses more "
                                 "battery than MP3 or M4A.")
        self.music_hint.setObjectName("accent")
        self.music_hint.setWordWrap(True)
        lay.addWidget(self.music_hint)
        note = QLabel("Tags and a matching .lrc lyrics file come along. Unusual sample rates "
                      "are converted to ones the PSP can play.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        self.pages.addWidget(music)

        pictures = QWidget()
        lay = QVBoxLayout(pictures)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.pic_fmt = QComboBox()
        self.pic_fmt.addItem("JPEG · small files", "jpeg")
        self.pic_fmt.addItem("PNG · keeps transparency", "png")
        self.pic_size = QComboBox()
        self.pic_size.addItem("Up to 1920 · room to zoom", "zoom")
        self.pic_size.addItem("PSP screen · smallest", "screen")
        self.pic_size.addItem("Original size", "full")
        form.addRow("Format", self.pic_fmt)
        form.addRow("Size", self.pic_size)
        lay.addLayout(form)
        note = QLabel("WebP, HEIC, AVIF, TIFF, GIF and others become pictures the PSP can open. "
                      "Photos are turned the right way up.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        self.pages.addWidget(pictures)

        other = FitScroll()
        lay = other.inner_layout
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)
        for title, text in (
                ("Books and text", "EPUB, FB2, DOCX, ODT, HTML or text become UTF-8 .txt for "
                                   "XLT's Reader, split if long."),
                ("Subtitles", "SRT, ASS and VTT become UTF-8 .srt. Name it after its video."),
                ("ZIP", "Make ZIP packs a folder for XLT's Files; a .tar becomes a ZIP."),
                ("PS1 games", "A .cue (or .bin) becomes an EBOOT.PBP, with its CD music.\n"
                              "A game on several discs: add all its discs at once, or select "
                              "their rows and press Combine into one EBOOT."),
                ("PSP games", "An ISO becomes a smaller CSO. A CSO or ZSO becomes an ISO again.")):
            head = QLabel(title)
            head.setObjectName("heading")
            body = QLabel(text)
            body.setObjectName("muted")
            body.setWordWrap(True)
            lay.addSpacing(4)
            lay.addWidget(head)
            lay.addWidget(body)
        lay.addStretch(1)
        self.pages.addWidget(other)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() not in (e.Type.ApplicationPaletteChange, e.Type.PaletteChange):
            return
        window = QApplication.palette().color(QPalette.ColorRole.Window).rgb()
        if window != self._icons_for:           # dark/light or colours changed
            for button in self.findChildren(QAbstractButton):
                names = button.property("iconNames")
                if names:
                    button.setIcon(icon(*names))
        self._icons_for = window

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty.resize(self.queue.viewport().size())

    def showEvent(self, e):
        super().showEvent(e)
        self.empty.resize(self.queue.viewport().size())

    # ----------------------------------------------------------- settings --
    def _load_settings(self):
        s = self.settings
        widgets = (self.res_box, self.fit_box, self.q_box, self.speed_box, self.enc_box,
                   self.norm_chk, self.subs_box, self.thm_chk, self.music_fmt, self.music_q,
                   self.music_cover, self.pic_fmt, self.pic_size)
        for w in widgets:
            w.blockSignals(True)

        def pick(box, key, default):
            i = box.findData(s.value(key, default, str))
            box.setCurrentIndex(max(i, 0))
        pick(self.res_box, "resolution", "480x272")
        pick(self.fit_box, "fit", "fit")
        pick(self.q_box, "quality", "balanced")
        pick(self.speed_box, "speed", "balanced")
        self.norm_chk.setChecked(s.value("normalize", True, bool))
        pick(self.subs_box, "subs_mode", "burn" if s.value("burn_subs", True, bool) else "off")
        pick(self.music_fmt, "music_format", "mp3")
        pick(self.music_q, "music_quality", "balanced")
        pick(self.pic_fmt, "picture_format", "jpeg")
        pick(self.pic_size, "picture_size", "zoom")
        self.music_cover.setChecked(s.value("music_cover", True, bool))
        self.thm_chk.setChecked(s.value("thumbnail", True, bool))
        for w in widgets:
            w.blockSignals(False)
        self._update_music_hint()

    def _save_settings(self):
        s = self.settings
        s.setValue("resolution", self.res_box.currentData())
        s.setValue("fit", self.fit_box.currentData())
        s.setValue("quality", self.q_box.currentData())
        s.setValue("speed", self.speed_box.currentData())
        if self.enc_box.count() > 1:      # only once NVENC detection finished
            s.setValue("encoder", self.enc_box.currentData())
        s.setValue("normalize", self.norm_chk.isChecked())
        s.setValue("subs_mode", self.subs_box.currentData())
        s.setValue("music_format", self.music_fmt.currentData())
        s.setValue("music_quality", self.music_q.currentData())
        s.setValue("music_cover", self.music_cover.isChecked())
        s.setValue("picture_format", self.pic_fmt.currentData())
        s.setValue("picture_size", self.pic_size.currentData())
        s.setValue("thumbnail", self.thm_chk.isChecked())
        s.setValue("custom_dir", self.custom_dir)
        s.setValue("size", self.size())

    def options(self) -> media.Options:
        return media.Options(
            resolution=self.res_box.currentData(), fit=self.fit_box.currentData(),
            quality=self.q_box.currentData(), normalize=self.norm_chk.isChecked(),
            thumbnail=self.thm_chk.isChecked(), speed=self.speed_box.currentData(),
            encoder=self.enc_box.currentData())

    def music_options(self) -> convert.MusicOptions:
        return convert.MusicOptions(self.music_fmt.currentData(), self.music_q.currentData(),
                                    self.music_cover.isChecked())

    def picture_options(self) -> convert.PictureOptions:
        return convert.PictureOptions(self.pic_fmt.currentData(), self.pic_size.currentData())

    def _estimate(self, job: Job) -> float:
        """Expected size of a job's output in MB (NaN if unknown)."""
        if job.kind == "video":
            return media.estimate_size_mb(job.info, self.options())
        if job.kind == "audio":
            if job.edit.in_place:
                return 0.0
            return convert.music_estimate_mb(job.info, self.music_options())
        if job.kind == "image":
            return convert.picture_estimate_mb(job.info, self.picture_options())
        return job.info.size / 1048576

    def _total_estimate(self, jobs) -> float:
        return sum(mb for mb in (self._estimate(j) for j in jobs) if mb == mb)

    def _auto_sub(self, info) -> media.SubChoice:
        mode = self.subs_box.currentData()
        if mode == "off":
            return media.SubChoice()
        return info.auto_sub(text_only=mode == "srt")

    def _update_music_hint(self):
        flac = self.music_fmt.currentData() == "flac"
        self.music_hint.setVisible(flac)
        self.music_q.setEnabled(not flac and not self.running)

    def _options_changed(self):
        self._update_music_hint()
        self._save_settings()
        self._update_summary()
        self._selection_changed()

    def _subs_mode_changed(self):
        for j in self.jobs:
            if j.kind == "video" and j.info and not j.sub_set_by_user:
                j.sub = self._auto_sub(j.info)
                self._refresh_row(j)
        self._options_changed()

    # ------------------------------------------------------------- cast --
    def open_cast(self):
        """The XLT Cast window (one): the PSP's game, live."""
        from .castview import CastWindow
        w = getattr(self, "_cast_win", None)
        if w is None or not w.isVisible():
            w = self._cast_win = CastWindow(self.settings)
            w.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            w.destroyed.connect(lambda *_: setattr(self, "_cast_win", None))
            w.show()
        w.raise_()
        w.activateWindow()

    # -------------------------------------------------------- destinations --
    def refresh_destinations(self):
        sticks = devices.find_sticks()
        psp_key = (self.psp.status, self.psp.model) if hasattr(self, "psp") else ("none", "")
        if ([s.mount for s in sticks] == [s.mount for s in self._sticks] and self.dest_box.count()
                and psp_key == getattr(self, "_psp_key", None)):
            # update free space and battery text only
            first = 1 if psp_key[0] in ("ready", "no_access") else 0
            if psp_key[0] == "ready":
                self.dest_box.setItemText(0, self._psp_label())
            for i, s in enumerate(sticks):
                self.dest_box.setItemText(first + i, self._stick_label(s))
            self._sticks = sticks
            return
        prev = self.dest_box.currentData()
        had_stick = bool(self._sticks)
        self._sticks = sticks
        self._psp_key = psp_key
        self.dest_box.blockSignals(True)
        self.dest_box.clear()
        if psp_key[0] == "ready":
            self.dest_box.addItem(icon("phone", "multimedia-player", "drive-removable-media"),
                                  self._psp_label(), "psp:")
        elif psp_key[0] == "no_access":
            self.dest_box.addItem(icon("dialog-password", "dialog-warning"),
                                  "PSP over USB: set up the driver…" if sys.platform == "win32"
                                  else "PSP over USB: allow access…", "pspsetup:")
        drive = icon("media-flash-memory-stick", "media-flash", "drive-removable-media")
        for s in sticks:
            self.dest_box.addItem(drive, self._stick_label(s), "stick:" + s.mount)
        self.dest_box.addItem(icon("folder-videos", "folder"), "Next to each source file", "beside:")
        if self.custom_dir:
            self.dest_box.addItem(icon("folder"), self._short(self.custom_dir), "custom:" + self.custom_dir)
        self.dest_box.addItem(icon("folder-open"), "Choose a folder…", "choose:")
        idx = self.dest_box.findData(prev) if prev else -1
        if sticks and prev is not None and not had_stick:
            idx = self.dest_box.findData("stick:" + sticks[0].mount)  # a stick was just plugged in: prefer it
        if psp_key[0] == "ready" and not self._psp_was_ready and prev is not None:
            idx = 0                       # the PSP just came: prefer it
        self._psp_was_ready = psp_key[0] == "ready"
        if idx < 0:
            saved = self.settings.value("dest_kind", "stick", str)
            idx = next((i for i in range(self.dest_box.count())
                        if self._dest(i)[0] == saved), 0)
            if self._dest(idx)[0] in ("choose", "pspsetup"):
                idx = next((i for i in range(self.dest_box.count())
                            if self._dest(i)[0] not in ("choose", "pspsetup")), 0)
        self.dest_box.setCurrentIndex(idx)
        self.dest_box.blockSignals(False)

    def _dest(self, idx: int) -> tuple[str, str]:
        data = self.dest_box.itemData(idx) if idx >= 0 else None
        kind, _, val = (data or "beside:").partition(":")
        return kind, val

    @staticmethod
    def _short(path: str) -> str:
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home) else path

    def _psp_label(self) -> str:
        """'PSP-E1000 over USB (XLT) · battery 64%, charging'"""
        p = self.psp.power_info
        return f"{self.psp.model} over USB (XLT)" + (f" · battery {xltlink.power_text(p)}" if p else "")

    def _stick_label(self, s: devices.Stick) -> str:
        return f"PSP Memory Stick ({s.label}) · {devices.human_size(s.free_bytes)} free"

    def _dest_activated(self, idx: int):
        kind, _ = self._dest(idx)
        if kind == "pspsetup":
            self.setup_psp_access()
            self.dest_box.setCurrentIndex(next((i for i in range(self.dest_box.count())
                                                if self._dest(i)[0] not in ("choose", "pspsetup")), 0))
            return
        if kind == "choose":
            d = QFileDialog.getExistingDirectory(self, "Save PSP videos to",
                                                 self.custom_dir or os.path.expanduser("~/Videos"))
            if d:
                self.custom_dir = d
                self._sticks = []          # force rebuild
                self.dest_box.clear()
                self.refresh_destinations()
                self.dest_box.setCurrentIndex(self.dest_box.findData("custom:" + d))
            else:
                self.dest_box.setCurrentIndex(0)
        kind, _ = self._dest(self.dest_box.currentIndex())
        self.settings.setValue("dest_kind", kind)
        self._save_settings()
        self._update_summary()

    def destination_dir(self, job_kind: str = "video") -> str | None:
        """None means "next to the source file"."""
        kind, val = self._dest(self.dest_box.currentIndex())
        if kind == "stick":
            st = next((s for s in self._sticks if s.mount == val), None)
            return st.folder(convert.FOLDERS[job_kind]) if st else None
        if kind == "custom":
            return val
        return None

    def open_destination(self):
        if self._dest(self.dest_box.currentIndex())[0] == "psp":
            QMessageBox.information(self, APP_NAME, "The files are on the PSP: XLT shows them in its library.")
            return
        sel = self.selected_jobs() or self.jobs
        d = self.destination_dir(sel[0].kind if sel else "video")
        if d is None:
            if not sel:
                return
            d = os.path.dirname(sel[0].output or os.path.abspath(sel[0].path))
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # -------------------------------------------------------- adding files --
    def pick_files(self):
        known = (media.VIDEO_EXTENSIONS | convert.AUDIO_EXTENSIONS | convert.IMAGE_EXTENSIONS
                 | convert.TEXT_EXTENSIONS | convert.SUB_EXTENSIONS | convert.PS1_EXTENSIONS
                 | convert.PSPGAME_EXTENSIONS)
        exts = " ".join(f"*{e}" for e in sorted(known)) + " " + " ".join(f"*{e}" for e in convert.TAR_SUFFIXES)
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add files", self.settings.value("last_dir", os.path.expanduser("~"), str),
            f"Everything it can convert ({exts} {exts.upper()});;All files (*)")
        if files:
            self.settings.setValue("last_dir", os.path.dirname(files[0]))
            self.add_paths(files)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Add a folder", self.settings.value("last_dir", os.path.expanduser("~"), str))
        if d:
            self.settings.setValue("last_dir", d)
            self.add_paths([d])

    def pick_zip_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Folder to pack as a ZIP", self.settings.value("last_dir", os.path.expanduser("~"), str))
        if d and not any(j.path == d and j.state != "done" for j in self.jobs):
            self.settings.setValue("last_dir", d)
            self._add_job(Job(d, "archive"))
            self._update_controls()

    def add_paths(self, paths: list[str]):
        files = []
        for p in paths:
            p = os.path.abspath(os.path.expanduser(p))
            if os.path.isdir(p):
                found = []
                for dirpath, dirs, names in os.walk(p):
                    dirs.sort()
                    found += [os.path.join(dirpath, n) for n in sorted(names)]
                files += convert.from_folder(found)
            elif os.path.isfile(p):
                files.append(p)
        files = convert.ps1_sources(files)  # a .cue stands for its tracks' images
        # a .lrc arriving with its song travels with that song
        stems = {os.path.splitext(f)[0] for f in files if convert.kind_of(f) == "audio"}
        stems |= {os.path.splitext(j.path)[0] for j in self.jobs if j.kind == "audio"}
        files = [f for f in files if not (convert.kind_of(f) == "lyrics"
                                          and os.path.splitext(f)[0] in stems)]
        known = {j.path for j in self.jobs if j.state not in ("done",)}
        for group in convert.group_discs(files):  # a PS1 game's discs: one job, one EBOOT
            f = group[0]
            if f in known:
                continue
            known.update(group)
            job = Job(f)
            if len(group) > 1:
                job.discs = group
            self._add_job(job)
        self._update_controls()

    def _add_job(self, job: Job):
        if not self.jobs:
            self.kind_tabs.setCurrentIndex(KIND_TAB[job.kind])
        job.item = QListWidgetItem()
        job.row = JobRow(job)
        job.row.remove_clicked.connect(lambda j=job: self.remove_job(j))
        job.item.setSizeHint(QSize(100, 78))
        job.item.setData(Qt.ItemDataRole.UserRole, id(job))
        self.queue.addItem(job.item)
        self.queue.setItemWidget(job.item, job.row)
        self.jobs.append(job)
        self.pool.start(ProbeTask(job, self.probe_signals))

    def _probed(self, job: Job, info, err: str):
        if job not in self.jobs:
            return
        if err:
            job.state = "error"
            job.error = err
            job.row.detail.setText("Can't read this file")
            job.row.set_status(err, "error")
        else:
            job.info = info
            if job.kind == "video":
                job.audio = info.default_audio()
                job.sub = self._auto_sub(info)
            job.state = "queued" if self.running else "ready"
            self._load_poster(job)
            self._refresh_row(job)
        self._update_controls()
        self._selection_changed()
        if self.running and not self.current:
            self._start_next()

    def _load_poster(self, job: Job):
        from_video = job.kind == "video" or Path(job.path).suffix.lower() in media.VIDEO_EXTENSIONS
        if job.kind in ("text", "sub", "archive", "lyrics", "ps1", "pspgame") or (
                job.kind == "audio" and not from_video and job.info.cover_stream < 0):
            pm = icon(*KIND_ICON[job.kind]).pixmap(36, 36)
            if job.row and not pm.isNull():
                job.row.poster.setPixmap(pm)
            return
        proc = QProcess(self)
        at = min(job.info.duration * 0.15, 90.0) if from_video and job.info.duration else 0
        cmd = media.poster_command(job.path, at)
        self._poster_procs.add(proc)

        def done(*_):
            data = bytes(proc.readAllStandardOutput())
            self._poster_procs.discard(proc)
            proc.deleteLater()
            if job.row is None:
                return
            pm = QPixmap()
            if data and pm.loadFromData(data):
                dpr = self.devicePixelRatioF()
                pm = pm.scaled(QSize(96, 54) * dpr, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                pm.setDevicePixelRatio(dpr)
                job.row.poster.setPixmap(pm)
        proc.finished.connect(done)
        proc.start(cmd[0], cmd[1:])

    def _refresh_row(self, job: Job):
        r = job.row
        if not r or not job.info:
            return
        bits = [job.info.summary(job.edit) if job.kind == "audio" else job.info.summary()]
        if job.kind == "video" and job.sub.kind != "none":
            bits.append("subs: " + (job.sub.track.language.upper() if job.sub.track and job.sub.track.language
                                    else Path(job.sub.path).suffix[1:] if job.sub.path else "on"))
        r.detail.setText(" · ".join(bits))
        if job.state in ("ready", "queued"):
            mb = self._estimate(job)
            waiting = "Waiting" if job.state == "queued" else "Ready"
            if job.kind == "audio" and job.edit.in_place:
                r.set_status(f"{waiting} · will update the original file")
            else:
                r.set_status(waiting + (f" · about {devices.human_size(mb * 1048576)}" if mb == mb else ""))

    def remove_job(self, job: Job):
        if job is self.current:
            if QMessageBox.question(self, "Stop this conversion?",
                                    f"“{job.name}” is being converted. Stop and remove it?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self._kill_current()
        row = self.queue.row(job.item)
        self.queue.takeItem(row)
        job.row = None
        self.jobs.remove(job)
        self._update_controls()

    def _queue_menu(self, pos):
        """Right click on queue rows: what can be done with them (the row clicked is selected
        first, unless it is part of the selection already)."""
        it = self.queue.itemAt(pos)
        if it is None:
            return
        if not it.isSelected():
            self.queue.clearSelection()
            it.setSelected(True)
        sel = self.selected_jobs()
        if not sel:
            return
        menu = QMenu(self)
        job = sel[0] if len(sel) == 1 else None
        for btn in (self.combine_btn, self.separate_btn, self.tags_btn):  # as the panel offers them
            if btn.isVisible():
                act = menu.addAction(btn.text(), btn.click)
                act.setEnabled(btn.isEnabled())
        if job and job.state == "done" and job.output and os.path.exists(job.output):
            menu.addAction("Show the result in its folder",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(job.output))))
        if job and os.path.exists(job.path):
            where = job.path if os.path.isdir(job.path) else os.path.dirname(job.path)
            menu.addAction("Show the original in its folder",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(where)))
        if not menu.isEmpty():
            menu.addSeparator()
        rm = menu.addAction("Remove from the list" if len(sel) == 1 else f"Remove {len(sel)} from the list",
                            self.remove_selected)
        rm.setShortcut(QKeySequence.StandardKey.Delete)
        menu.exec(self.queue.viewport().mapToGlobal(pos))

    def remove_selected(self):
        """The selected rows out of the queue (one being converted asks first, as its ✕ does)."""
        sel = self.selected_jobs()
        running = [j for j in sel if j is self.current]
        for j in [j for j in sel if j is not self.current]:
            self.remove_job(j)
        for j in running:
            self.remove_job(j)
        self._update_controls()

    def clear_finished(self):
        for j in [j for j in self.jobs if j.state in ("done", "error", "cancelled")]:
            self.remove_job(j)

    # ----------------------------------------------------------- selection --
    def selected_jobs(self) -> list[Job]:
        ids = {it.data(Qt.ItemDataRole.UserRole) for it in self.queue.selectedItems()}
        return [j for j in self.jobs if id(j) in ids]

    def _selection_changed(self):
        sel = self.selected_jobs()
        job = sel[0] if len(sel) == 1 else None
        editable = bool(job and job.info and job.state in ("ready", "queued", "error", "cancelled", "done"))
        boxes = (self.audio_box, self.sub_box, self.as_box)
        for b in boxes:
            b.blockSignals(True)
        self.audio_box.clear()
        self.sub_box.clear()
        if job:
            self.kind_tabs.setCurrentIndex(KIND_TAB[job.kind])
        is_video = bool(job and job.kind == "video")
        from_video = bool(job and job.kind in ("video", "audio")
                          and Path(job.path).suffix.lower() in media.VIDEO_EXTENSIONS)
        self.sel_form.setRowVisible(self.as_box, from_video)
        # audio and subtitle tracks only for a video (empty boxes for the rest just take room)
        self.sel_form.setRowVisible(self.audio_box, is_video)
        self.sel_form.setRowVisible(self.sub_box, is_video)
        songs = bool(sel) and all(j.kind == "audio" and j.info for j in sel)
        self.tags_btn.setVisible(songs)
        self.tags_btn.setEnabled(songs and not self.running)
        waiting = ("ready", "queued", "error", "cancelled")
        discs = 2 <= len(sel) <= 5 and all(j.kind == "ps1" and j.state in waiting and not getattr(j, "discs", None)
                                           for j in sel)
        self.combine_btn.setVisible(discs)
        self.combine_btn.setEnabled(discs and not self.running)
        multi = bool(job and job.kind == "ps1" and getattr(job, "discs", None) and job.state in waiting)
        self.separate_btn.setVisible(multi)
        self.separate_btn.setEnabled(multi and not self.running)
        self.as_box.setCurrentIndex(max(0, self.as_box.findData(job.kind if job else "video")))
        self.as_box.setEnabled(editable or bool(job and job.state == "error"))
        if job and job.info:
            self.sel_heading.setText("Selected item")
            lines = [job.info.summary()]
            if is_video:
                if job.info.audio:
                    for t in job.info.audio:
                        self.audio_box.addItem(t.label("audio"), t.index)
                    self.audio_box.setCurrentIndex(max(0, self.audio_box.findData(job.audio.index if job.audio else -1)))
                else:
                    self.audio_box.addItem("No audio track", -1)
                for i, c in enumerate(job.info.sub_choices()):
                    self.sub_box.addItem(c.label(), i)
                    if (c.kind == job.sub.kind and c.path == job.sub.path
                            and (c.track.index if c.track else None) == (job.sub.track.index if job.sub.track else None)):
                        self.sub_box.setCurrentIndex(i)
                if job.info.interlaced:
                    lines.append("Interlaced: will be deinterlaced.")
                if job.info.hdr:
                    lines.append("HDR: will be tone-mapped to SDR.")
                if job.sub.is_bitmap and self.subs_box.currentData() == "srt":
                    lines.append("These subtitles are pictures, so they can only be burned in.")
            elif job.kind == "audio":
                lines = [job.info.summary(job.edit)]
                if job.edit.in_place:
                    lines.append("The original file will be updated.")
                elif convert.music_can_copy(job.info, self.music_options()):
                    lines.append("Already fits the PSP: the sound is kept as it is.")
                if job.edit.lyrics or (job.edit.lyrics is None and (job.info.lyrics_file or job.info.lyrics_embedded)):
                    lines.append("Has lyrics.")
            if job.kind == "ps1" and job.info.discs:
                lines.append("Discs: " + "; ".join(f"{i + 1}. {os.path.basename(d[0])}"
                                                   for i, d in enumerate(job.info.discs)))
            mb = self._estimate(job)
            if mb == mb and job.state != "done":
                lines.append(f"Output about {devices.human_size(mb * 1048576)}.")
            if job.state == "done" and getattr(job, "psp_written", None):
                lines.append("On the PSP as " + ", ".join(w.split(":/", 1)[-1] for w in job.psp_written))
            elif job.output and job.state == "done":
                lines.append("Saved as " + self._short(job.output))
            if len(job.outputs) > 1 and job.state == "done":
                lines.append(f"In {len(job.outputs)} parts, as XLT's Reader stops at 2 MB.")
            if job.error and job.state == "error":
                lines.append(job.error)
            self.sel_info.setText("\n".join(lines))
        else:
            self.sel_heading.setText(f"{len(sel)} items selected" if sel else "Selected item")
            self.audio_box.addItem("—")
            self.sub_box.addItem("—")
            self.sel_info.setText("" if sel else "Select a video to choose its audio and subtitle tracks."
                                  if self.kind_tabs.currentIndex() == 0 else "")
        # nothing selected, outside the Video tab: no empty "Selected item" heading either
        self.sel_heading.setVisible(bool(sel) or self.kind_tabs.currentIndex() == 0)
        self.audio_box.setEnabled(editable and is_video and bool(job.info.audio))
        self.sub_box.setEnabled(editable and is_video and len(job.info.sub_choices()) > 1)
        for b in boxes:
            b.blockSignals(False)

    def edit_tags(self):
        jobs = [j for j in self.selected_jobs() if j.kind == "audio" and j.info]
        if not jobs or TagDialog(self, jobs).exec() != TagDialog.DialogCode.Accepted:
            return
        for j in jobs:
            if j.state == "done":
                j.state = "ready"
            self._refresh_row(j)
        self._update_controls()
        self._selection_changed()

    def _as_picked(self, idx: int):
        sel = self.selected_jobs()
        kind = self.as_box.itemData(idx)
        if len(sel) != 1 or sel[0].kind == kind:
            return
        job = sel[0]
        job.kind, job.info, job.state, job.error = kind, None, "probing", ""
        job.sub, job.sub_set_by_user = media.SubChoice(), False
        job.row.detail.setText("Reading…")
        job.row.set_status("")
        self.pool.start(ProbeTask(job, self.probe_signals))
        self._update_controls()
        self._selection_changed()

    def _audio_picked(self, idx: int):
        sel = self.selected_jobs()
        if len(sel) == 1 and sel[0].info and sel[0].kind == "video":
            data = self.audio_box.itemData(idx)
            sel[0].audio = next((t for t in sel[0].info.audio if t.index == data), None)

    def _sub_picked(self, idx: int):
        sel = self.selected_jobs()
        if len(sel) == 1 and sel[0].info and sel[0].kind == "video":
            sel[0].sub = sel[0].info.sub_choices()[self.sub_box.itemData(idx)]
            sel[0].sub_set_by_user = True
            self._refresh_row(sel[0])

    def _nvenc_result(self, ok: bool):
        if not ok:
            return
        self.enc_box.blockSignals(True)
        self.enc_box.addItem("NVIDIA GPU · NVENC (experimental)", "nvenc")
        if self.settings.value("encoder", "x264", str) == "nvenc":
            self.enc_box.setCurrentIndex(1)
        self.enc_box.blockSignals(False)

    # ---------------------------------------------------------- conversion --
    def _check_ffmpeg(self):
        if not media.ffmpeg_path() or not media.ffprobe_path():
            QMessageBox.critical(self, "ffmpeg not found",
                                 "This build could not find ffmpeg/ffprobe, so it can't convert anything.")

    def toggle_run(self):
        if self.running or self.psp.busy():
            self.stop_requested = self.running
            self.running = False
            self.psp.cancel_all()
            self._kill_current()
            for j in self.jobs:
                if j.state == "queued":
                    j.state = "ready"
                    self._refresh_row(j)
            self._update_controls()
            return
        pending = [j for j in self.jobs if j.state in ("ready", "cancelled", "error") and j.info]
        if not pending:
            return
        kind, val = self._dest(self.dest_box.currentIndex())
        if kind == "custom":
            try:
                os.makedirs(val, exist_ok=True)
            except OSError as e:
                QMessageBox.warning(self, "Can't use that folder", str(e))
                return
        st = next((s for s in self._sticks if s.mount == val), None) if kind == "stick" else None
        need = self._total_estimate(pending) * 1024 * 1024
        if st and need > st.free_bytes:
            if QMessageBox.question(
                    self, "Memory Stick may fill up",
                    f"These files may need about {devices.human_size(need)}, but only "
                    f"{devices.human_size(st.free_bytes)} is free. Convert anyway?") \
                    != QMessageBox.StandardButton.Yes:
                return
        for j in pending:
            j.state = "queued"
            j.error = ""
            j.progress = 0
            self._refresh_row(j)
        self.running = True
        self.stop_requested = False
        self._update_controls()
        self._start_next()

    def _start_next(self):
        if not self.running:
            return
        job = next((j for j in self.jobs if j.state == "queued"), None)
        if job is None:
            if not any(j.state == "probing" for j in self.jobs):
                self._all_done()
            return
        in_place = job.kind == "audio" and job.edit.in_place
        out_dir = None if in_place else self.destination_dir(job.kind)
        job.psp_stage = ""
        if not in_place and self._dest(self.dest_box.currentIndex())[0] == "psp":
            # converted on this PC, then sent: into a folder of its own, named as the PSP's
            job.psp_folder = convert.FOLDERS[job.kind]
            job.psp_stage = os.path.join(work_dir(), "to-psp", f"{os.getpid()}-{id(job)}")
            out_dir = os.path.join(job.psp_stage, job.psp_folder)
        ext = None if in_place else {
            "video": ".mp4", "audio": convert.MUSIC_EXT[self.music_fmt.currentData()],
            "image": convert.PICTURE_EXT[self.pic_fmt.currentData()]}.get(job.kind)
        try:
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            if ext:
                job.output = media.output_path(job.path, out_dir, ext)
        except OSError as e:
            self._fail(job, str(e))
            QTimer.singleShot(0, self._start_next)
            return
        self.tmpdir = tempfile.mkdtemp(prefix="xlt-tool-")
        job.state = "running"
        job.progress = 0
        job.started = time.monotonic()
        job.stderr = ""
        job.part = ""
        job.copy_to = ""
        job.outputs = []
        job.work_label = "Copying to Memory Stick"
        job.row.bar.setRange(0, 1000)
        job.row.bar.setValue(0)
        job.row.bar.show()
        job.row.set_status("Starting…")
        self.current = job
        if job.kind == "ps1" and job.info:  # the same game's icon and background, if the library has them
            gv = self.games_view
            job.info.covers = gv.covers_on()
            for g in list(gv.games) + (list(gv.psp_state.games) if gv.psp_state else []):
                if g.kind == "ps1" and g.game_id.upper() == job.info.game_id and g.icon:
                    job.info.icon0, job.info.pic1 = g.icon, g.pic
                    break
        if job.kind == "ps1" and not self._ensure_ps1_parts(multi=bool(job.info and job.info.discs)):
            self.current = None
            self._fail(job, "Needs a PS1 EBOOT of yours first (for the part every PS1 EBOOT has)")
            QTimer.singleShot(0, self._start_next)
            return
        if not ext:
            job.work_label = "Updating" if in_place else "Converting"
            job.copy_started = time.monotonic()
            self.copy_cancel.clear()
            self.copy_thread = threading.Thread(
                target=python_job, args=(job, out_dir, self.tmpdir, self.copy_signals, self.copy_cancel),
                daemon=True)
            self.copy_thread.start()
            self._update_controls()
            return
        # Writing straight to a USB stick is slow, and ffmpeg's faststart pass
        # rewrites the whole file at the end. Encode on the local disk, then
        # copy once with real progress.
        job.part = job.output + ".part"
        if any(job.output.startswith(st.mount + os.sep) for st in self._sticks):
            try:
                wd = work_dir()
                need = self._estimate(job) * 1.6 * 1048576 + (256 << 20)
                if not need == need or shutil.disk_usage(wd).free > need:
                    job.part = os.path.join(wd, f"{os.getpid()}-{id(job)}.mp4.part")
                    job.copy_to = job.output
            except OSError:
                pass
        if job.kind == "audio":
            cmd = convert.music_command(job.info, job.part, self.music_options())
        elif job.kind == "image":
            cmd = convert.picture_command(job.info, job.part, self.picture_options())
        else:
            cmd = self._video_command(job)
        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self._read_progress)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._proc_error)
        self.proc.start(cmd[0], cmd[1:])
        self._update_controls()

    def _video_command(self, job: Job) -> list[str]:
        job.sidecar = ""
        burn, srt_track = job.sub, None
        if self.subs_box.currentData() == "srt" and job.sub.kind != "none" and not job.sub.is_bitmap:
            burn = media.SubChoice()            # XLT shows it from the .srt instead
            job.sidecar = os.path.join(self.tmpdir, "sidecar.srt")
            if job.sub.kind == "embedded":
                srt_track = job.sub.track
            else:
                try:
                    utf8 = os.path.join(self.tmpdir, "sub" + Path(job.sub.path).suffix.lower())
                    media.subtitle_to_utf8(job.sub.path, utf8)
                    convert.srt_from_text_file(utf8, job.sidecar)
                except (OSError, ValueError):
                    job.sidecar = ""
        sub_file = None
        try:
            if burn.kind == "embedded":
                sub_file = os.path.join(self.tmpdir, "src" + Path(job.path).suffix.lower())
                os.symlink(job.path, sub_file)
            elif burn.kind == "external":
                sub_file = os.path.join(self.tmpdir, "sub" + Path(burn.path).suffix.lower())
                media.subtitle_to_utf8(burn.path, sub_file)
        except OSError:
            sub_file = None
        return media.build_command(job.info, job.part, self.options(), job.audio, burn, sub_file,
                                   job.sidecar if srt_track else None, srt_track)

    def _read_progress(self):
        job, proc = self.current, self.proc
        if not job or not proc:
            return
        text = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        for line in text.splitlines():
            k, _, v = line.partition("=")
            if k == "out_time_us" and v.strip().lstrip("-").isdigit() and job.info.duration:
                job.progress = max(0.0, min(1.0, int(v) / 1e6 / job.info.duration))
            elif k == "speed":
                job.speed = v.strip()
            elif k == "progress":
                if v.strip() == "end":
                    job.row.bar.setRange(0, 0)          # busy animation
                    job.row.set_status("Finishing · writing the file index…"
                                       if job.kind == "video" else "Finishing…", "accent")
                else:
                    self._show_progress(job)

    def _show_progress(self, job: Job):
        job.row.bar.setValue(int(job.progress * 1000))
        elapsed = time.monotonic() - job.started
        bits = [f"{job.progress * 100:.0f}%"]
        if job.progress > 0.02:
            left = elapsed / job.progress - elapsed
            bits.append(f"{media.format_time(left)} left")
        sp = getattr(job, "speed", "")
        if sp and sp != "N/A":
            bits.append(sp)
        job.row.set_status("Converting · " + " · ".join(bits), "accent")
        self._update_summary()

    def _read_stderr(self):
        if self.current and self.proc:
            self.current.stderr += bytes(self.proc.readAllStandardError()).decode(errors="replace")
            self.current.stderr = self.current.stderr[-4000:]

    def _proc_error(self, err):
        if err == QProcess.ProcessError.FailedToStart and self.current:
            self._fail(self.current, "Could not start ffmpeg")
            self._cleanup_proc()
            QTimer.singleShot(0, self._start_next)

    def _finished(self, code: int, status):
        job = self.current
        if job is None:
            return
        part = getattr(job, "part", job.output + ".part")
        if self.stop_requested or job.state == "cancelled":
            self._remove_quiet(part)
            job.state = "cancelled" if job in self.jobs else job.state
            if job.row:
                job.row.bar.hide()
                job.row.set_status("Stopped")
        elif (code == 0 and status == QProcess.ExitStatus.NormalExit and os.path.exists(part)
              and job.kind == "audio" and convert.needs_tagging(job.info, job.edit)):
            if self.proc:
                self.proc.deleteLater()
            self.proc = None
            job.row.bar.setRange(0, 1000)
            job.row.bar.setValue(0)
            job.row.set_status("Writing tags…", "accent")
            job.copy_started = time.monotonic()
            self.copy_cancel.clear()
            self.copy_thread = threading.Thread(
                target=tag_and_deliver,
                args=(job, part, job.output, job.copy_to, self.copy_signals, self.copy_cancel), daemon=True)
            self.copy_thread.start()
            return                                   # _copy_done continues
        elif (code == 0 and status == QProcess.ExitStatus.NormalExit and os.path.exists(part)
              and getattr(job, "copy_to", "")):
            if self.proc:
                self.proc.deleteLater()
            self.proc = None
            job.row.bar.setRange(0, 1000)
            job.row.bar.setValue(0)
            job.row.set_status(f"{job.work_label}…", "accent")
            job.copy_started = time.monotonic()
            self.copy_cancel.clear()
            self.copy_thread = threading.Thread(
                target=copy_to_stick, args=(job, part, job.copy_to, self.copy_signals,
                                            self.copy_cancel), daemon=True)
            self.copy_thread.start()
            return                                   # _copy_done continues
        elif code == 0 and status == QProcess.ExitStatus.NormalExit and os.path.exists(part):
            try:
                os.replace(part, job.output)
            except OSError as e:
                self._fail(job, str(e))
            else:
                self._finish_ok(job)
        else:
            self._remove_quiet(part)
            lines = [ln.strip() for ln in job.stderr.splitlines()
                     if ln.strip() and not ln.strip().startswith("Last message repeated")]
            # The first message is the cause; the last is usually a generic
            # "Error opening output files".
            self._fail(job, lines[0] if lines else f"ffmpeg exited with code {code}")
        self._cleanup_proc()
        self.stop_requested = False
        if self.running:
            QTimer.singleShot(0, self._start_next)
        else:
            self._update_controls()

    def _copy_progress(self, job: Job, frac: float):
        if job is not self.current or not job.row:
            return
        job.row.bar.setValue(int(frac * 1000))
        el = time.monotonic() - job.copy_started
        left = f" · {media.format_time(el / frac - el)} left" if frac > 0.03 else ""
        job.row.set_status(f"{job.work_label} · {frac * 100:.0f}%{left}", "accent")

    def _copy_done(self, job: Job, err: str):
        self._remove_quiet(job.part)
        self.copy_thread = None
        if err == "cancelled" or self.stop_requested:
            job.state = "cancelled"
            if job.row:
                job.row.bar.hide()
                job.row.set_status("Stopped")
        elif err:
            self._fail(job, err)
        else:
            self._finish_ok(job)
        self._cleanup_proc()
        self.stop_requested = False
        if self.running:
            QTimer.singleShot(0, self._start_next)
        else:
            self._update_controls()

    def _clean_work_dir(self):
        """Remove leftovers from conversions interrupted by a crash."""
        shutil.rmtree(os.path.join(work_dir(), "to-psp"), ignore_errors=True)  # never sent: nothing waits for them
        try:
            wd = work_dir()
            for n in os.listdir(wd):
                p = os.path.join(wd, n)
                if n.endswith(".part") and time.time() - os.path.getmtime(p) > 6 * 3600:
                    os.remove(p)
        except OSError:
            pass

    def _finish_ok(self, job: Job):
        note = ""
        to_psp = bool(getattr(job, "psp_stage", ""))
        thm_cmd = None
        if job.kind == "video":
            if self.thm_chk.isChecked() and to_psp:
                thm_cmd = media.thumbnail_command(job.info, os.path.splitext(job.output)[0] + ".THM")
            elif self.thm_chk.isChecked():
                thm = os.path.splitext(job.output)[0] + ".THM"
                cmd = media.thumbnail_command(job.info, thm)
                QProcess.startDetached(cmd[0], cmd[1:])
            if job.sidecar:
                try:
                    if os.path.getsize(job.sidecar) == 0:
                        raise OSError("empty")
                    shutil.copyfile(job.sidecar, os.path.splitext(job.output)[0] + ".srt")
                    note = " · with .srt"
                except OSError:
                    note = " · subtitles not saved"
        elif job.kind == "audio" and not job.edit.in_place:
            try:
                convert.write_lyrics(job.info, job.edit, job.output)
            except OSError:
                note = " · lyrics not saved"
        elif job.kind == "audio":               # the original changed: read it afresh
            try:
                job.info = convert.probe_audio(job.path)
                job.edit = convert.TagEdit()
                self._load_poster(job)
                self._refresh_row(job)
            except (OSError, RuntimeError):
                pass
        job.outputs = job.outputs or [job.output]
        if to_psp:
            files = sorted(os.path.join(job.psp_stage, job.psp_folder, n)
                           for n in os.listdir(os.path.join(job.psp_stage, job.psp_folder)))
            job.state = "sending"
            job.psp_note = note
            if job.row:
                job.row.bar.setRange(0, 1000)
                job.row.bar.setValue(0)
                job.row.bar.show()
                job.row.set_status("Waiting to send to the PSP…", "accent")
            self.psp.send(Upload(job, job.output, files, job.psp_folder, job.psp_stage, thm_cmd))
            self._selection_changed()
            return
        job.state = "done"
        job.progress = 1
        if job.kind in ("ps1", "pspgame"):  # a game made here: the Library shows it (if in its folders)
            self.games_view.refresh(psp=False)
            out = os.path.abspath(job.output)
            if not any(out.startswith(os.path.abspath(d).rstrip(os.sep) + os.sep) for d in self.games_view.folders()):
                note += " · not in the Library's folders"
        if job.row:
            job.row.bar.hide()
            size = sum(os.path.getsize(p) for p in job.outputs)
            took = media.format_time(time.monotonic() - job.started)
            if len(job.outputs) > 1:
                note += f" · {len(job.outputs)} parts"
            job.row.set_status(f"Done · {devices.human_size(size)} in {took} · "
                               f"{Path(job.output).name}{note}", "ok")
        if any(job.output.startswith(s.mount) for s in self._sticks):
            threading.Thread(target=os.sync, daemon=True).start()
        self._selection_changed()

    # ------------------------------------------------------------ the PSP --
    def _psp_state(self, status: str, text: str):
        self.refresh_destinations()

    def _psp_progress(self, job: Job, frac: float):
        if job.row and job.state == "sending":
            job.row.bar.setValue(int(frac * 1000))
            job.row.set_status(f"Sending to the PSP · {frac * 100:.0f}%", "accent")

    def _psp_sent(self, job: Job, err: str, written: list):
        if job.state != "sending":
            return
        if err == "cancelled":
            job.state = "cancelled"
            if job.row:
                job.row.bar.hide()
                job.row.set_status("Stopped before it was on the PSP")
        elif err:
            self._fail(job, err)
        else:
            job.state = "done"
            job.progress = 1
            job.psp_written = written
            if job.kind in ("ps1", "pspgame"):  # a game sent to the PSP: the Library sees it there
                self.games_view.refresh(pc=False)
            if job.row:
                job.row.bar.hide()
                took = media.format_time(time.monotonic() - job.started)
                where = written[0].split(":/", 1)[-1] if written else ""
                more = f" · {len(written)} files" if len(written) > 1 else ""
                job.row.set_status(f"On the PSP in {took} · {where}{more}{getattr(job, 'psp_note', '')}", "ok")
        self._selection_changed()
        self._update_controls()

    def _psp_drained(self):
        if not self.running:
            self._all_done()

    def setup_psp_access(self):
        from . import usbaccess
        usbaccess.offer(self)

    def _fail(self, job: Job, msg: str):
        job.state = "error"
        job.error = msg
        if job.row:
            job.row.bar.hide()
            job.row.set_status("Failed · " + msg, "error")
            job.row.status.setToolTip(getattr(job, "stderr", "").strip() or msg)

    @staticmethod
    def _remove_quiet(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    def _kill_current(self):
        if self.copy_thread and self.copy_thread.is_alive():
            if self.current:
                self.current.state = "cancelled"
            self.stop_requested = True
            self.copy_cancel.set()
            self.copy_thread.join(10)
            QApplication_process_events()
            return
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            if self.current:
                self.current.state = "cancelled"
            self.stop_requested = True
            self.proc.terminate()
            if not self.proc.waitForFinished(3000):
                self.proc.kill()
                self.proc.waitForFinished(2000)

    def _cleanup_proc(self):
        if self.proc:
            self.proc.deleteLater()
        self.proc = None
        self.current = None
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def _all_done(self):
        self.running = False
        self._update_controls()
        sending = sum(j.state == "sending" for j in self.jobs)
        if sending:
            self.summary.setText(f"Sending {sending} to the PSP…")
            return                       # _psp_drained comes back here
        done = sum(j.state == "done" for j in self.jobs)
        failed = sum(j.state == "error" for j in self.jobs)
        self.summary.setText(f"{done} converted" + (f", {failed} failed" if failed else ""))
        QApplication_alert(self)

    # ------------------------------------------------------------ helpers --
    def _update_controls(self):
        self.empty.setVisible(not self.jobs)
        busy = self.running or (hasattr(self, "psp") and self.psp.busy())
        pending = any(j.state in ("ready", "cancelled", "error") and j.info for j in self.jobs)
        self.go_btn.setText("Stop" if busy else "Convert")
        self.go_btn.setObjectName("destructive" if busy else "primary")
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(busy or pending)
        for w in (self.res_box, self.fit_box, self.q_box, self.speed_box, self.enc_box,
                  self.norm_chk, self.subs_box, self.thm_chk, self.dest_box,
                  self.music_fmt, self.music_cover, self.pic_fmt, self.pic_size):
            w.setEnabled(not busy)
        self.music_q.setEnabled(not busy and self.music_fmt.currentData() != "flac")
        self.tags_btn.setEnabled(not busy)
        self.clear_btn.setEnabled(any(j.state in ("done", "error", "cancelled") for j in self.jobs))
        self._update_summary()

    def _update_summary(self):
        if self.running:
            total = [j for j in self.jobs if j.state in ("queued", "running", "sending", "done") and j.info]
            n_done = sum(j.state in ("done", "sending") for j in total)
            self.summary.setText(f"{n_done + (1 if self.current else 0)} of {len(total)}")
            return
        ready = [j for j in self.jobs if j.state in ("ready", "cancelled", "error") and j.info]
        if ready:
            mb = self._total_estimate(ready)
            self.summary.setText(f"{len(ready)} file{'s' if len(ready) != 1 else ''} · ~{devices.human_size(mb * 1048576)}")
        elif not any(j.state == "done" for j in self.jobs):
            self.summary.setText("")
        for j in self.jobs:
            if j.state in ("ready", "queued"):
                self._refresh_row(j)

    # ------------------------------------------------------------- d & d --
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.view_tabs.setCurrentIndex(VIEW_CONVERT)  # dropped files are for converting
            self.add_paths(paths)
            e.acceptProposedAction()

    def closeEvent(self, e):
        if self.games_view.copying():
            if QMessageBox.question(self, "Stop copying?",
                                    "A game or its saves are being copied between this PC and the PSP. Stop and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        if not self.running and self.psp.busy():
            if QMessageBox.question(self, "Stop sending?",
                                    "Files are still being sent to the PSP. Stop and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        if self.running:
            if QMessageBox.question(self, "Stop converting?",
                                    "A conversion is running. Stop it and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.running = False
            self._kill_current()
            if self.current:
                self._remove_quiet(getattr(self.current, "part", self.current.output + ".part"))
            self._cleanup_proc()
        self._save_settings()
        if getattr(self, "_cast_win", None) is not None:
            self._cast_win.close()  # it stops casting and lets the PSP go
        self.psp.stop()
        super().closeEvent(e)
        if e.isAccepted() and self._restart_after:
            update.restart(self._restart_after)

    # --------------------------------------------------------------- misc --
    def integrate_desktop(self, quiet: bool = False):
        appimage = os.environ.get("APPIMAGE")
        if not appimage:
            if not quiet:
                QMessageBox.information(self, APP_NAME, "This only works when running the AppImage.")
            return
        data = _data_home()
        icon_dir = os.path.join(data, "icons", "hicolor", "scalable", "apps")
        app_dir = os.path.join(data, "applications")
        os.makedirs(icon_dir, exist_ok=True)
        os.makedirs(app_dir, exist_ok=True)
        src_icon = os.path.join(os.path.dirname(__file__), "xlt-tool.svg")
        shutil.copyfile(src_icon, os.path.join(icon_dir, "xlt-tool.svg"))
        exec_path = appimage.replace('"', '\\"')
        with open(os.path.join(app_dir, "xlt-tool.desktop"), "w", encoding="utf-8") as f:
            f.write(DESKTOP_ENTRY.format(exec=f'"{exec_path}"'))
        remove_old_desktop_entry()
        if not quiet:
            QMessageBox.information(
                self, APP_NAME,
                f"{APP_NAME} is now in your applications menu.\n\n"
                f"If you move the AppImage, choose this again.")

    def migrate_desktop_entry(self):
        """PSP Video Converter was in the applications menu: XLT Tool takes its place."""
        if os.environ.get("APPIMAGE") and os.path.exists(os.path.join(_data_home(), "applications", "pspconv.desktop")):
            try:
                self.integrate_desktop(quiet=True)
            except OSError:
                pass

    # ------------------------------------------------------------ updates --
    def check_updates(self, manual: bool = False):
        """Asks GitHub for the latest XLT Tool (update.py) on a thread; at start at most
        once a day and only if "Check for updates at start" is on."""
        if not manual:
            last = float(self.settings.value("update_last", 0) or 0)
            if not self.auto_update_action.isChecked() or time.time() - last < 86400:
                return
        self.settings.setValue("update_last", time.time())

        def run():
            try:
                rel = update.latest()
                self.update_signals.checked.emit(rel, "", manual)
            except Exception as e:  # noqa: BLE001 - offline: said only when asked
                self.update_signals.checked.emit(None, str(e), manual)
        threading.Thread(target=run, daemon=True).start()

    def _update_checked(self, rel, err: str, manual: bool):
        if err:
            if manual:
                QMessageBox.warning(self, APP_NAME, f"Couldn't ask GitHub for updates: {err}")
            return
        if not update.newer(rel):
            if manual:
                QMessageBox.information(self, APP_NAME, f"{APP_NAME} {__version__} is the latest."
                                        if rel else f"No {APP_NAME} release has been published yet.")
            return
        if not manual and self.settings.value("update_skip", "") == rel.version:
            return
        target = update.appimage()
        box = QMessageBox(self)
        box.setWindowTitle("Update")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"{APP_NAME} {rel.version} is out (you have {__version__}).")
        box.setDetailedText(update.plain_notes(rel.notes) or "")
        if target and rel.url:
            box.setInformativeText("Update now? The new version is downloaded and checked, then takes "
                                   "this one's place; it starts the next time, or now.")
            go = box.addButton("Update", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setInformativeText("Download it from its release page.")
            go = box.addButton("Open the release page", QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(go)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip:
            self.settings.setValue("update_skip", rel.version)
        if clicked is not go:
            return
        if not (target and rel.url):
            QDesktopServices.openUrl(QUrl(rel.page))
            return
        self._update_progress = QProgressDialog(f"Downloading {APP_NAME} {rel.version}…", "Stop", 0, 1000, self)
        self._update_progress.setWindowTitle("Update")
        self._update_progress.setMinimumDuration(0)
        self._update_cancel = threading.Event()
        self._update_progress.canceled.connect(self._update_cancel.set)

        def run():
            try:
                part = update.download(rel, target, lambda f: self.update_signals.progress.emit(f),
                                       self._update_cancel.is_set)
                update.install(part, target)
                self.update_signals.done.emit(rel.version, "")
            except InterruptedError:
                self.update_signals.done.emit(rel.version, "cancelled")
            except Exception as e:  # noqa: BLE001 - reported
                self.update_signals.done.emit(rel.version, str(e))
        threading.Thread(target=run, daemon=True).start()

    def _update_done(self, version: str, err: str):
        if getattr(self, "_update_progress", None):
            self._update_progress.close()
            self._update_progress = None
        if err == "cancelled":
            return
        if err:
            QMessageBox.warning(self, APP_NAME, f"The update didn't happen: {err}")
            return
        if QMessageBox.question(self, "Update", f"{APP_NAME} {version} is in place. Restart it now?") \
                == QMessageBox.StandardButton.Yes:
            self._restart_after = update.appimage() or os.environ.get("APPIMAGE", "")
            self.close()

    def about(self):
        from PySide6.QtCore import QByteArray
        from PySide6.QtSvg import QSvgRenderer
        from . import brand
        ring = self.palette().color(QPalette.ColorRole.WindowText).name()
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(144 * dpr), int(80 * dpr))
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        QSvgRenderer(QByteArray(brand.logo(ring).encode())).render(painter)
        painter.end()
        pm.setDevicePixelRatio(dpr)
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(pm)
        box.setText(f"<h3>{APP_NAME} {__version__}</h3>"
                    "<p>The PC companion of XLT, the PSP launcher: a library for your PSP games, "
                    "and conversions of videos, music, pictures, books and PS1 games into "
                    "files the PSP and XLT can open.</p>"
                    f"<p>ffmpeg: {media.ffmpeg_path() or 'not found'}</p>"
                    "<p>Free software under the GPL-3.0. "
                    "<a href='https://github.com/boubounokefalos/xlt-tool/blob/main/docs/Credits.md'>"
                    "Credits and licences</a></p>")
        box.setTextFormat(Qt.TextFormat.RichText)
        for label in box.findChildren(QLabel):
            label.setOpenExternalLinks(True)
        box.exec()


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=XLT Tool
GenericName=PSP Media Converter
Comment=Convert videos, music, pictures and books for the PlayStation Portable and send them to XLT
Exec={exec} %F
Icon=xlt-tool
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;
MimeType=video/mp4;video/x-matroska;video/webm;video/x-msvideo;video/quicktime;video/mpeg;video/x-ms-wmv;video/x-flv;video/mp2t;video/ogg;video/3gpp;
Keywords=psp;xlt;video;music;convert;mp4;
StartupWMClass=xlt-tool
"""


def QApplication_process_events():
    """Deliver the queued copy-done signal now (used when stopping)."""
    QApplication.processEvents()


def QApplication_alert(w: QWidget):
    QApplication.alert(w, 0)
