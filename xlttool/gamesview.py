"""The Library view: the user's game library on the PC (xlttool/games.py) and, with
XLT connected over USB sync, what is on the PSP (xlttool/psplib.py), side by side,
with details and the saves of each game, on the PSP and backed up on the PC."""
from __future__ import annotations

import html
import os
import threading
import time

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QSizePolicy, QToolButton,
                               QCheckBox, QMessageBox, QScrollArea, QVBoxLayout, QWidget)

from . import covers, games, psplib, xltlink
from .pspsync import Task

ICON_W, ICON_H = 144, 80          # ICON0's size
SHOWS = (("All games", None), ("PSP games", ("iso", "cso", "zso")), ("Homebrew", ("homebrew",)),
         ("PSN games", ("psn",)), ("PS1 games", ("ps1",)))
SORTS = ("Name", "Size", "Newest", "Region", "Most played", "Last played", "Favorites first")
WHERES = (("PC and PSP", None), ("On the PSP", "psp"), ("Only on this PC", "pc_only"),
          ("Only on the PSP", "psp_only"))


class ScanSignals(QObject):
    progress = Signal(int, int)
    done = Signal(list)
    cover = Signal(str)          # a game ID whose box art just arrived
    covers_done = Signal(str)    # "" or why it stopped


class GamesView(QWidget):
    covers_changed = Signal(bool)   # box art turned on or off (the ⋮ menu's item follows)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.library = games.Library()
        self.games: list[games.Game] = []
        self.entries: list[psplib.Entry] = []
        self.psp = None                     # PspSync, from the window
        self.psp_lib = psplib.PspLibrary()
        self.psp_state: psplib.PspState | None = None
        self._psp_scanning = False
        self._psp_rescan = False
        self._copy: Task | None = None
        self._note = ""                     # what the last copy, send or removal did
        self._scanning = False
        self._rescan = False
        self._last_scan = 0.0      # monotonic time of the last look at the PC's folders
        self._last_psp_scan = 0.0  # and at the PSP
        self.signals = ScanSignals()
        self.signals.progress.connect(self._scan_progress)
        self.signals.done.connect(self._scan_done)
        self.signals.cover.connect(self._cover_arrived)
        self.signals.covers_done.connect(self._covers_done)
        self._covers_running = False
        self._build()
        self._fill_folder_menu()
        self.rescan()

    # ---------------------------------------------------------------- UI --
    def _build(self):
        global set_icon
        from .window import set_icon  # (window imports this module)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search games, IDs")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(150)
        self.search.textChanged.connect(self._refill)
        self.show_box = QComboBox()
        for name, _ in SHOWS:
            self.show_box.addItem(name)
        self.show_box.setCurrentIndex(int(self.settings.value("games_show", 0)) % len(SHOWS))
        self.show_box.currentIndexChanged.connect(self._filters_changed)
        self.sort_box = QComboBox()
        for name in SORTS:
            self.sort_box.addItem(f"Sort: {name}", name)
        self.sort_box.setCurrentIndex(int(self.settings.value("games_sort", 0)) % len(SORTS))
        self.sort_box.currentIndexChanged.connect(self._filters_changed)
        self.where_box = QComboBox()
        for name, _ in WHERES:
            self.where_box.addItem(name)
        self.where_box.setToolTip("Which games to show, with XLT connected over USB sync.")
        self.where_box.currentIndexChanged.connect(self._refill)
        self.folders_btn = QToolButton()
        self.folders_btn.setText("Folders")
        self.folders_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.folders_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        set_icon(self.folders_btn, "folder-symbolic", "folder")
        self.folder_menu = QMenu(self.folders_btn)
        self.folders_btn.setMenu(self.folder_menu)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.show_box)
        bar.addWidget(self.where_box)
        bar.addWidget(self.sort_box)
        bar.addWidget(self.folders_btn)
        left.addLayout(bar)

        self.grid = QListWidget()
        self.grid.setObjectName("games")
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(QSize(ICON_W, ICON_H))
        self.grid.setGridSize(QSize(ICON_W + 28, ICON_H + 52))
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.currentItemChanged.connect(lambda *_: self._show_details())
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        self.grid.setAcceptDrops(False)
        left.addWidget(self.grid, 1)
        self.empty = QLabel("")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setParent(self.grid.viewport())
        self.empty_btn = QPushButton("Add a folder with games…", self.grid.viewport())
        self.empty_btn.setObjectName("primary")
        self.empty_btn.clicked.connect(self.add_folder)
        self._viewport = self.grid.viewport()
        self._viewport.installEventFilter(self)
        status_row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.psp_status = QLabel("")
        self.psp_status.setObjectName("muted")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_copy)
        self.stop_btn.hide()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.psp_status)
        status_row.addWidget(self.stop_btn)
        left.addLayout(status_row)
        lay.addLayout(left, 3)

        # details
        side = QFrame()
        side.setObjectName("card")
        side.setFixedWidth(330)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()  # a game with saves on both sides can be taller than the window
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        inner = QWidget()
        scroll.setWidget(inner)
        side_lay.addWidget(scroll)
        sl = QVBoxLayout(inner)
        self._inner, self._inner_lay = inner, sl
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        self.pic = QLabel()
        self.pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic.setFixedHeight(168)
        self.pic.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sl.addWidget(self.pic)
        self.title = QLabel("")
        self.title.setObjectName("heading")
        self.title.setWordWrap(True)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.2)
        self.title.setFont(f)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self.title, 1)
        self.cover = QLabel()  # box art, when downloading it is on
        self.cover.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.cover.hide()
        title_row.addWidget(self.cover)
        sl.addLayout(title_row)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.fields: dict[str, QLabel] = {}
        for name in ("Kind", "Game ID", "Region", "Version", "Needs firmware", "Size", "Played"):
            v = QLabel("")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.fields[name] = v
            form.addRow(name, v)
        self.form = form
        sl.addLayout(form)
        self.here = QLabel("")  # where it is on the PC (its own line: a path wraps here, not in the form)
        self.here.setWordWrap(True)
        self.here.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.here)
        self.open_btn = QPushButton("Show in folder")
        self.open_btn.clicked.connect(self._open_folder)
        sl.addWidget(self.open_btn)
        self.where = QLabel("")
        self.where.setWordWrap(True)
        self.where.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.where)
        self.copy_btn = QPushButton("Copy to this PC")
        self.copy_btn.setToolTip("Copies the game from the PSP into the library's first folder, "
                                 "and backs up its saves.")
        self.copy_btn.clicked.connect(self._copy_to_pc)
        sl.addWidget(self.copy_btn)
        self.send_btn = QPushButton("Send to the PSP")
        self.send_btn.setObjectName("primary")
        self.send_btn.setToolTip("Sends the game where XLT finds it (ISO, or PSP/GAME), with the saves "
                                 "backed up on this PC that the PSP doesn't have.")
        self.send_btn.clicked.connect(self._send_to_psp)
        sl.addWidget(self.send_btn)
        self.remove_btn = QPushButton("Remove from the PSP…")
        self.remove_btn.setToolTip("Removes the game from the PSP. Its saves are backed up to this PC first.")
        self.remove_btn.clicked.connect(self._remove_from_psp)
        sl.addWidget(self.remove_btn)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: palette(mid);")
        sl.addSpacing(4)
        sl.addWidget(line)
        self.psp_saves_head = QLabel("Saves on the PSP")
        self.psp_saves_head.setObjectName("heading")
        sl.addWidget(self.psp_saves_head)
        self.psp_saves = QLabel("")
        self.psp_saves.setObjectName("muted")
        self.psp_saves.setWordWrap(True)
        self.psp_saves.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.psp_saves)
        self.backup_btn = QPushButton("Back up saves to this PC")
        self.backup_btn.clicked.connect(self._backup_saves)
        sl.addWidget(self.backup_btn)
        saves_head = QLabel("Saves on this PC")
        saves_head.setObjectName("heading")
        sl.addWidget(saves_head)
        self._saves_head_pc = saves_head
        self.saves = QLabel("")
        self.saves.setObjectName("muted")
        self.saves.setWordWrap(True)
        self.saves.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.saves)
        self.restore_btn = QPushButton("Put saves back on the PSP…")
        self.restore_btn.setToolTip("Sends the newest backup of each of this game's saves to the PSP. "
                                    "A save already there is backed up first, then replaced.")
        self.restore_btn.clicked.connect(self._restore_saves)
        sl.addWidget(self.restore_btn)
        sl.addStretch(1)
        self.side = side
        lay.addWidget(side)
        self._show_details()

    def eventFilter(self, obj, e):
        if obj is self._viewport and e.type() == QEvent.Type.Resize:
            self._place_empty()
        return False

    def _place_empty(self):
        r = self.grid.viewport().rect()
        self.empty.setGeometry(r.adjusted(24, 24, -24, -24 - (60 if self.empty_btn.isVisible() else 0)))
        hint = self.empty_btn.sizeHint()
        self.empty_btn.move((r.width() - hint.width()) // 2, r.height() // 2 + 30)
        self.empty_btn.resize(hint)

    # ------------------------------------------------------------ folders --
    def folders(self) -> list[str]:
        v = self.settings.value("games_dirs", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [d for d in (v or []) if d]

    def _set_folders(self, dirs: list[str]):
        self.settings.setValue("games_dirs", dirs)
        self._fill_folder_menu()
        self.rescan()

    def _fill_folder_menu(self):
        m = self.folder_menu
        m.clear()
        m.addAction("Add a folder with games…", self.add_folder)
        dirs = self.folders()
        if dirs:
            m.addSeparator()
            for d in dirs:
                sub = m.addMenu(d)
                sub.addAction("Open", lambda d=d: QDesktopServices.openUrl(QUrl.fromLocalFile(d)))
                sub.addAction("Remove from the library", lambda d=d: self._set_folders(
                    [x for x in self.folders() if x != d]))
            m.addSeparator()
            m.addAction("Look again", lambda: (self.rescan(), self.scan_psp()))

    # ---------------------------------------------------------- box art --
    def covers_on(self) -> bool:
        return str(self.settings.value("covers_online", "false")).lower() == "true"

    def _set_covers(self, on: bool):
        """Box art on or off, from the ⋮ menu."""
        if on == self.covers_on():
            self._sync_art(on)
            return
        if on and QMessageBox.question(
                self, "Box art",
                "XLT Tool will download covers for your PSP and PS1 games from the libretro project "
                "(thumbnails.libretro.com and its lists on GitHub). It asks by the games' names; nothing "
                "else is sent. Covers are kept on this PC; PS1 EBOOTs it makes can use them as icons."
                "\n\nTurn box art on?") != QMessageBox.StandardButton.Yes:
            self._sync_art(False)
            return
        self.settings.setValue("covers_online", "true" if on else "false")
        self._sync_art(on)
        if on:
            self.fetch_covers()
        self._show_details()

    def _sync_art(self, on: bool):
        self.covers_changed.emit(on)  # the ⋮ menu's item follows

    def fetch_covers(self):
        """Box art for every game in the library (PC and PSP) not yet looked for, in the background."""
        if not self.covers_on() or self._covers_running:
            return
        todo, seen = [], set()
        for e in self.entries:
            g = e.game
            key = (covers.system_of(g), g.game_id.upper())
            if key[0] and key[1] and key not in seen:
                seen.add(key)
                todo.append(g)
        if not todo:
            return
        self._covers_running = True

        def run():
            err = ""
            for g in todo:
                try:
                    if covers.fetch(g):
                        self.signals.cover.emit(g.game_id.upper())
                except Exception as e:  # noqa: BLE001 - offline, or the site is down: try again later
                    err = str(e)
                    break
            self.signals.covers_done.emit(err)
        threading.Thread(target=run, daemon=True).start()

    def _cover_arrived(self, game_id: str):
        e = self._current()
        if e and e.game.game_id.upper() == game_id:
            self._show_details()

    def _covers_done(self, err: str):
        self._covers_running = False
        if err:
            self.status.setText(f"Box art: couldn't reach libretro ({err})")

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "A folder with games", self.settings.value("last_games_dir", os.path.expanduser("~"), str))
        if not d:
            return
        self.settings.setValue("last_games_dir", d)
        dirs = self.folders()
        if any(d == x or d.startswith(x.rstrip("/") + "/") for x in dirs):
            return  # already in the library
        self._set_folders([x for x in dirs if not x.startswith(d.rstrip("/") + "/")] + [d])

    # ------------------------------------------------------------ scanning --
    def rescan(self):
        if self._scanning:
            self._rescan = True
            return
        dirs = [d for d in self.folders() if os.path.isdir(d)]
        self._scanning = True
        self._last_scan = time.monotonic()
        self.status.setText("Looking for games…" if dirs else "")

        def run():
            last = [0.0]

            def progress(i, n, _p):
                if time.monotonic() - last[0] > 0.1:
                    last[0] = time.monotonic()
                    self.signals.progress.emit(i, n)
            try:
                found = self.library.scan(dirs, progress)
            except Exception:  # an unreadable folder must not take the view down
                found = []
            self.signals.done.emit(found)
        threading.Thread(target=run, daemon=True).start()

    def refresh(self, pc: bool = True, psp: bool = True, older_than: float = 0.0):
        """Looks again at the PC's folders and/or the PSP, if the last look is older than
        older_than seconds (0: always). New or changed files only are read, so it is cheap."""
        now = time.monotonic()
        if pc and now - self._last_scan >= older_than:
            self.rescan()
        if psp and self.psp and self.psp.ready() and now - self._last_psp_scan >= max(older_than, 0.0):
            self.scan_psp()

    def showEvent(self, e):
        super().showEvent(e)
        # coming back to the Library: games copied or converted meanwhile show up
        self.refresh(older_than=10.0)

    def _scan_progress(self, i: int, n: int):
        self.status.setText(f"Looking for games… {i} of {n}")

    def _scan_done(self, found: list):
        self._scanning = False
        self.games = found
        self._merge()
        if self._rescan:
            self._rescan = False
            self.rescan()

    # ----------------------------------------------------------------- PSP --
    def attach_psp(self, psp):
        self.psp = psp
        psp.state.connect(self._psp_changed)
        psp.task_progress.connect(self._task_progress)
        psp.task_done.connect(self._task_done)
        psp.power.connect(lambda _p: self._show_psp_status())
        self._psp_changed(psp.status, psp.text)

    def copying(self) -> bool:
        return self._copy is not None

    def _psp_changed(self, status: str, _text: str = ""):
        if status == "ready":
            if self.psp_state is None:
                self.scan_psp()
        elif self.psp_state is not None or self._psp_scanning:
            self.psp_state = None
            self._merge()
        self.where_box.setEnabled(status == "ready")
        self._show_psp_status()

    def scan_psp(self):
        if not self.psp or not self.psp.ready():
            return
        if self._psp_scanning:
            self._psp_rescan = True
            return
        self._psp_scanning = True
        self._last_psp_scan = time.monotonic()
        self._show_psp_status()

        def run(link, task):
            return self.psp_lib.scan(link, lambda i, n, p: task.report(i / max(n, 1), f"{i} of {n}"),
                                     task.cancelled)
        self.psp.run_task(Task("psp-scan", run))

    def _show_psp_status(self):
        st = self.psp_state
        if self._copy:
            return  # the copy's progress is shown instead
        if self._psp_scanning:
            self.psp_status.setText("Looking at the PSP…")
        elif st:
            space = f" · {games.human(st.free)} free of {games.human(st.total)}" if st.total else ""
            note = f"{self._note} · " if self._note else ""
            p = self.psp.power_info if self.psp else None
            batt = f" · battery {xltlink.power_text(p)}" if p else ""
            self.psp_status.setText(f"{note}{st.model}: {len(st.games)} games{space}{batt}")
        else:
            self.psp_status.setText("")

    def _task_progress(self, task: Task, frac: float, text: str):
        if task.name == "psp-scan" and not self._copy and self._psp_scanning:
            self.psp_status.setText(f"Looking at the PSP… {text}")
        elif task is self._copy:
            self.psp_status.setText(f"{task.data}: {frac * 100:.0f}%")

    def _task_done(self, task: Task):
        if task.name == "psp-scan":
            self._psp_scanning = False
            if not task.error and task.result is not None:
                self.psp_state = task.result
                self._merge()
            if self._psp_rescan:
                self._psp_rescan = False
                self.scan_psp()
        elif task is self._copy:
            self._copy = None
            self.stop_btn.hide()
            if task.error and task.error != "cancelled":
                QMessageBox.warning(self, "XLT Tool", task.error)
            elif task.error == "cancelled":
                self._note = "Stopped"
            if task.name == "copy-game" and not task.error:
                self.rescan()
            if task.name in ("send-game", "remove-game", "restore-saves"):
                self.scan_psp()  # (also after an error: part of it may have happened)
            if not task.error and isinstance(task.result, str) and task.result:
                self._note = task.result
            self._show_details()
        self._show_psp_status()

    def _stop_copy(self):
        if self._copy:
            self._copy.cancel.set()

    def _start_copy(self, name: str, label: str, fn):
        if self._copy or not self.psp or not self.psp.ready():
            return
        self._copy = Task(name, fn, label)
        self._note = ""
        self.stop_btn.show()
        self.psp_status.setText(f"{label}…")
        self.psp.run_task(self._copy)
        self._show_details()

    def _copy_to_pc(self):
        e = self._current()
        if not e or not e.psp or e.pc:
            return
        dirs = [d for d in self.folders() if os.path.isdir(d)]
        if not dirs:
            self.add_folder()
            dirs = [d for d in self.folders() if os.path.isdir(d)]
            if not dirs:
                return
        dest, g = dirs[0], e.psp
        need = g.size + 64 * 1024 * 1024
        try:
            import shutil
            if shutil.disk_usage(dest).free < need:
                QMessageBox.warning(self, "XLT Tool", f"Not enough room in {dest} for {g.title} "
                                    f"({games.human(g.size)}).")
                return
        except OSError:
            pass
        saves = e.saves_on_psp(self.psp_state)
        game_id = g.save_prefix()

        def run(link, task):
            total = g.size + sum(s.size for s in saves) or 1
            where = psplib.copy_game_to_pc(link, g, dest, lambda f: task.report(f * g.size / total),
                                           task.cancelled)
            if saves and game_id:
                psplib.backup_saves(link, saves, game_id,
                                    lambda f: task.report((g.size + f * (total - g.size)) / total),
                                    task.cancelled)
            return where
        self._start_copy("copy-game", f"Copying {g.title}", run)

    def _backup_saves(self):
        e = self._current()
        saves = e.saves_on_psp(self.psp_state) if e else []
        game_id = e.game.save_prefix() if e else ""
        if not saves or not game_id:
            return

        def run(link, task):
            return psplib.backup_saves(link, saves, game_id, lambda f: task.report(f), task.cancelled)
        self._start_copy("backup-saves", f"Backing up the saves of {e.game.title}", run)

    def _send_to_psp(self):
        e = self._current()
        if not e or not e.pc or e.psp or not self.psp_state:
            return
        g, state = e.pc, self.psp_state
        on_psp = {s.folder.upper() for s in state.saves}
        backups = [b for b in psplib.newest_backups(g.save_prefix()) if b.folder.upper() not in on_psp] \
            if g.save_prefix() else []

        def run(link, task):
            share = g.size / (g.size + sum(games.folder_size(b.path)[0] for b in backups) or 1)
            where = psplib.send_game_to_psp(link, g, state.device, lambda f: task.report(f * share), task.cancelled)
            if backups:
                psplib.restore_saves(link, backups, state, state.device,
                                     lambda f: task.report(share + f * (1 - share)), task.cancelled)
            saves = f", with {len(backups)} save{'s' if len(backups) != 1 else ''}" if backups else ""
            return f"Sent {g.title}{saves}"
        self._start_copy("send-game", f"Sending {g.title}", run)

    def _remove_from_psp(self):
        e = self._current()
        if not e or not e.psp or not self.psp_state:
            return
        g, saves = e.psp, e.saves_on_psp(self.psp_state)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Remove from the PSP")
        box.setText(f"Remove {g.title} from the PSP?")
        info = []
        if not e.pc:
            info.append("It isn't on this PC: once removed it is gone, unless you copy it here first.")
        if saves:
            info.append(f"Its saves ({len(saves)}) are backed up to this PC first.")
        box.setInformativeText(" ".join(info))
        also = None
        if saves:
            also = QCheckBox("Also delete its saves from the PSP")
            box.setCheckBox(also)
        remove = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not remove:
            return
        drop_saves = bool(also and also.isChecked())
        game_id = g.save_prefix()

        def run(link, task):
            if saves and game_id:
                psplib.backup_saves(link, saves, game_id, lambda f: task.report(f * 0.9), task.cancelled)
            psplib.remove_game(link, g)
            if drop_saves:
                psplib.remove_saves(link, saves)
            task.report(1.0)
            kept = " and its saves" if drop_saves else (", its saves kept" if saves else "")
            return f"Removed {g.title}{kept}"
        self._start_copy("remove-game", f"Removing {g.title}", run)

    def _restore_saves(self):
        e = self._current()
        if not e or not self.psp_state:
            return
        backups = psplib.newest_backups(e.game.save_prefix()) if e.game.save_prefix() else []
        if not backups:
            return
        state = self.psp_state
        on_psp = {s.folder.upper() for s in state.saves}
        replace = [b for b in backups if b.folder.upper() in on_psp]
        lines = []
        for b in backups:
            when = b.when.replace("_", " ")
            if len(when) == 17:
                when = f"{when[:13]}:{when[13:15]}"
            lines.append(f"{b.title or b.folder} ({when})" + ("  replaces the PSP's" if b in replace else ""))
        text = "\n".join(lines)
        if replace:
            text += "\n\nThe PSP's saves that get replaced are backed up to this PC first."
        if QMessageBox.question(self, "Put saves back on the PSP",
                                f"Send these saves of {e.game.title} to the PSP?\n\n{text}") \
                != QMessageBox.StandardButton.Yes:
            return

        def run(link, task):
            done = psplib.restore_saves(link, backups, state, state.device, lambda f: task.report(f), task.cancelled)
            return f"Put back {len(done)} save{'s' if len(done) != 1 else ''} of {e.game.title}"
        self._start_copy("restore-saves", f"Putting back the saves of {e.game.title}", run)

    def _merge(self):
        self.entries = psplib.merge(self.games, self.psp_state.games if self.psp_state else [])
        self._refill()
        self.fetch_covers()

    # ------------------------------------------------------------- the grid --
    def _filters_changed(self):
        self.settings.setValue("games_show", self.show_box.currentIndex())
        self.settings.setValue("games_sort", self.sort_box.currentIndex())
        self._refill()

    def _visible(self) -> list[psplib.Entry]:
        kinds = SHOWS[self.show_box.currentIndex()][1]
        where = WHERES[self.where_box.currentIndex()][1] if self.psp_state else None
        q = self.search.text().strip().lower()

        def wanted(e: psplib.Entry) -> bool:
            g = e.game
            if kinds and g.kind not in kinds:
                return False
            if q and q not in g.title.lower() and q not in g.game_id.lower():
                return False
            return (where is None or (where == "psp" and e.psp) or (where == "pc_only" and not e.psp)
                    or (where == "psp_only" and not e.pc))
        out = [e for e in self.entries if wanted(e)]
        sort = self.sort_box.currentData()
        if sort == "Size":
            out.sort(key=lambda e: -e.game.size)
        elif sort == "Newest":
            out.sort(key=lambda e: -e.game.mtime)
        elif sort == "Region":
            out.sort(key=lambda e: (e.game.region or "~", e.game.title.lower()))
        elif sort == "Favorites first":  # XLT's favorites, then by name
            out.sort(key=lambda e: (not (e.psp and e.psp.extra.get("favorite")), e.game.title.lower()))
        elif sort in ("Most played", "Last played"):  # XLT's play history, on the PSP
            k = "seconds" if sort == "Most played" else "played"
            out.sort(key=lambda e: (-(e.psp.extra.get(k, 0) if e.psp else 0), e.game.title.lower()))
        else:
            out.sort(key=lambda e: e.game.title.lower())
        return out

    def _refill(self):
        cur = self._current()
        keep = psplib.match_key(cur.game) if cur else None
        self.grid.clear()
        shown = self._visible()
        for e in shown:
            g = e.game
            it = QListWidgetItem(self._icon(e), g.title)
            it.setData(Qt.ItemDataRole.UserRole, e)
            where = "on this PC and the PSP" if e.pc and e.psp else "on the PSP only" if e.psp else "on this PC"
            if e.psp and e.psp.extra.get("favorite"):
                where += " · a favorite in XLT"
            it.setToolTip(f"{g.title}\n{g.kind_name}{' · ' + g.game_id if g.game_id else ''} · "
                          f"{games.human(g.size)}\n{where.capitalize()}")
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.grid.addItem(it)
            if keep is not None and psplib.match_key(g) == keep:
                self.grid.setCurrentItem(it)
        if self.grid.currentItem() is None and self.grid.count():
            self.grid.setCurrentRow(0)
        n, total = len(shown), sum(e.game.size for e in shown)
        if not self._scanning:
            self.status.setText(f"{n} game{'s' if n != 1 else ''} · {games.human(total)}" if self.entries else "")
        if not self.folders() and not self.entries:
            self.empty.setText("<div style='font-size:15pt; font-weight:600'>Your games</div>"
                               "<div>Use <b>Folders</b> to add the folders your PSP games, homebrew "
                               "and PS1 games are in.</div>")
        elif not self.entries:
            self.empty.setText("" if self._scanning else "No games found in the library's folders.")
        elif not shown:
            self.empty.setText("Nothing matches.")
        else:
            self.empty.setText("")
        self.empty.setVisible(bool(self.empty.text()))
        self.empty_btn.setVisible(not self.folders() and not self.entries)
        self._show_details()
        self._place_empty()

    def _icon(self, e: psplib.Entry) -> QIcon:
        g = e.game
        pm = QPixmap(g.icon) if g.icon else QPixmap()
        if pm.isNull():
            ic = QIcon.fromTheme("applications-games", QIcon.fromTheme("application-x-executable"))
            pm = ic.pixmap(48, 48)
        canvas = QPixmap(ICON_W, ICON_H)
        canvas.fill(Qt.GlobalColor.transparent)
        from PySide6.QtGui import QPainter
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scaled = pm.scaled(ICON_W, ICON_H, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap((ICON_W - scaled.width()) // 2, (ICON_H - scaled.height()) // 2, scaled)
        # where it is, while the PSP is connected: a coloured pill in the corner
        if self.psp_state is not None:
            if e.pc and e.psp:
                text, back = "PC + PSP", QColor(46, 125, 50, 230)     # green: on both
            elif e.psp:
                text, back = "PSP only", QColor(230, 126, 0, 235)     # orange: not on this PC
            else:
                text, back = "PC only", QColor(90, 90, 90, 220)       # grey: not on the PSP
            f = QFont(self.font())
            f.setPointSizeF(max(7.0, f.pointSizeF() * 0.75))
            f.setBold(True)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(text) + 12
            h = p.fontMetrics().height() + 2
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawRoundedRect(ICON_W - w - 3, ICON_H - h - 3, w, h, h / 2, h / 2)
            p.setPen(QColor("white"))
            p.drawText(ICON_W - w - 3, ICON_H - h - 3, w, h, Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        return QIcon(canvas)

    def _current(self) -> psplib.Entry | None:
        it = self.grid.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    # ------------------------------------------------------------- details --
    def _show_details(self):
        e = self._current()
        self.side.setVisible(e is not None)
        if not e:
            return
        g = e.game
        pic = QPixmap(g.pic) if g.pic else QPixmap()
        if pic.isNull() and g.icon:
            pic = QPixmap(g.icon)
        if pic.isNull():
            self.pic.clear()
            self.pic.hide()
        else:
            self.pic.setPixmap(pic.scaled(296, 168, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            self.pic.show()
        fav = bool(e.psp and e.psp.extra.get("favorite"))
        self.title.setText(g.title + ("  ★" if fav else ""))
        self.title.setToolTip("A favorite in XLT" if fav else "")
        art = (covers.cached(e.pc) if e.pc else "") or (covers.cached(e.psp) if e.psp else "")
        cover = QPixmap(art) if art and self.covers_on() else QPixmap()
        if cover.isNull():
            self.cover.hide()
        else:
            self.cover.setPixmap(cover.scaled(84, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            self.cover.show()
        values = {"Kind": g.kind_name, "Game ID": g.game_id, "Region": g.region, "Version": g.version,
                  "Needs firmware": g.firmware, "Size": games.human(g.size),
                  "Played": psplib.played_text(e.psp.extra) if e.psp else ""}
        for name, label in self.fields.items():
            v = values.get(name, "")
            label.setText(v)
            self.form.setRowVisible(label, bool(v))
        self.open_btn.setVisible(e.pc is not None)
        self.here.setText(f"<b>On this PC</b>: {html.escape(e.pc.path).replace('/', '/&#8203;')}" if e.pc else "")
        self.here.setVisible(e.pc is not None)
        connected = self.psp_state is not None
        if e.psp:
            other = f" ({e.psp.kind_name})" if e.pc and e.pc.kind != e.psp.kind else ""
            self.where.setText(f"<b>On the PSP</b>{other}: {html.escape(e.psp.path).replace('/', '/&#8203;')}")
        elif connected:
            self.where.setText("<b>Not on the PSP</b>")
        else:
            self.where.setText("")
        self.where.setVisible(bool(self.where.text()))
        self.copy_btn.setVisible(bool(e.psp and not e.pc))
        self.copy_btn.setEnabled(self._copy is None)
        saves = e.saves_on_psp(self.psp_state)
        for w in (self.psp_saves_head, self.psp_saves):
            w.setVisible(connected)
        if connected:
            if saves:
                self.psp_saves.setText("<br>".join(
                    f"<b>{html.escape(s.title or s.folder)}</b> · {games.human(s.size)}"
                    + (f"<br>{html.escape(s.detail)}" if s.detail else "")
                    for s in saves[:6]) + (f"<br>and {len(saves) - 6} more" if len(saves) > 6 else ""))
            else:
                self.psp_saves.setText("None")
        self.backup_btn.setVisible(bool(saves))
        self.send_btn.setVisible(bool(connected and e.pc and not e.psp))
        self.remove_btn.setVisible(bool(connected and e.psp))
        for b in (self.send_btn, self.remove_btn, self.restore_btn):
            b.setEnabled(self._copy is None)
        QTimer.singleShot(0, self._fit_details)
        self.backup_btn.setEnabled(self._copy is None)
        prefix = g.save_prefix()
        backups = games.save_backups(prefix) if prefix else []
        self.restore_btn.setVisible(bool(connected and backups))
        if not prefix:
            self.saves.setText("This game has no ID, so its saves can't be told apart.")
        elif not backups:
            self.saves.setText(f"No backups of this game's saves yet. They are kept in "
                               f"<i>{html.escape(games.saves_dir())}</i>, under {html.escape(prefix)}.")
        else:
            lines = []
            for b in backups[:6]:
                when = b.when.replace("_", " ")
                if len(when) == 17:
                    when = f"{when[:13]}:{when[13:15]}"
                name = b.title or b.folder
                lines.append(f"<b>{html.escape(name)}</b> · {when}" + (f"<br>{html.escape(b.detail)}" if b.detail else ""))
            more = len(backups) - 6
            self.saves.setText("<br>".join(lines) + (f"<br>and {more} older" if more > 0 else ""))

    def _grid_menu(self, pos):
        """Right click on a game: the actions its details offer, in a menu."""
        it = self.grid.itemAt(pos)
        if it is None:
            return
        self.grid.setCurrentItem(it)  # (shows its details, which decide what can be done)
        menu = QMenu(self)
        for btn in (self.send_btn, self.copy_btn, self.remove_btn, self.backup_btn, self.restore_btn,
                    self.open_btn):
            if btn.isVisible():
                act = menu.addAction(btn.text(), btn.click)
                act.setEnabled(btn.isEnabled())
        if menu.isEmpty():
            return
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _fit_details(self):
        """The scroll area squeezes its content to the window unless told how tall the
        wrapped labels make it."""
        w = self._inner.width() or 300
        h = self._inner_lay.totalHeightForWidth(w) if self._inner_lay.hasHeightForWidth() else -1
        self._inner.setMinimumHeight(max(h, self._inner_lay.minimumSize().height()))

    def _open_folder(self):
        e = self._current()
        if e and e.pc:
            g = e.pc
            where = g.path if os.path.isdir(g.path) else os.path.dirname(g.path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(where))
