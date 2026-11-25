"""The Files view: XLT's folders on the PSP (the ones USB sync may change: Video,
Music, Pictures, Books, Downloads, ISO, PSP/GAME and the saves), browsed over the
cable with XLT running. Send files and folders (buttons or drag and drop), copy to
this PC, open, rename, make folders and delete. The work is xlttool/pspfiles.py,
run on the sync worker as Tasks, so it waits its turn with the Library's."""
from __future__ import annotations

import os

from PySide6.QtCore import QMimeDatabase, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QStackedWidget, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import games, pspfiles, xltlink
from .pspsync import Task

PATH_ROLE = Qt.ItemDataRole.UserRole
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
NAME, SIZE, DATE = range(3)
DEVICE_NAMES = {"ms0:": "Memory Stick", "ef0:": "Internal storage"}


def _cache_dir() -> str:
    return os.path.join(games.cache_dir(), "open")   # files opened from the PSP


class FileItem(QTreeWidgetItem):
    """Folders first, then by the column (sizes and dates as numbers)."""

    def __lt__(self, other):
        a, b = self.data(0, ENTRY_ROLE), other.data(0, ENTRY_ROLE)
        if a is None or b is None:
            return super().__lt__(other)
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else NAME
        up = not tree or tree.header().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
        if a.is_dir != b.is_dir:  # folders stay on top either way
            return a.is_dir if up else b.is_dir
        if col == SIZE and a.size != b.size:
            return a.size < b.size
        if col == DATE and a.mtime != b.mtime:
            return tuple(a.mtime) < tuple(b.mtime)
        return a.name.lower() < b.name.lower()


class FileList(QTreeWidget):
    """Takes files and folders dropped from the PC's file manager."""
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.accept_files = False

    def _ok(self, e) -> bool:
        return self.accept_files and e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls())

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if self._ok(e) else e.ignore()

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if self._ok(e) else e.ignore()

    def dropEvent(self, e):
        if not self._ok(e):
            return e.ignore()
        e.acceptProposedAction()
        self.dropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])


class FilesView(QWidget):
    changed = Signal()    # something changed on the PSP: the Library looks again (games, free space)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.psp = None                 # PspSync, from the window
        self.xlt_folder = lambda: ""    # () -> XLT's own folder on the PSP (never removed), from the Library
        self.space = None               # () -> (free, total) or None, from the Library's look at the PSP
        self.folder = ""                # the PSP folder shown ("" = none yet)
        self.entries: list[xltlink.Entry] = []
        self.devices: list[str] = []
        self._listing: Task | None = None
        self._relist = False
        self._op: Task | None = None    # the one send, copy, rename or delete going on
        self._note = ""
        self._select_after: list[str] = []  # names to select once the folder is listed again
        self._mime = QMimeDatabase()
        self._build()

    # ---------------------------------------------------------------- UI --
    def _build(self):
        from .window import icon, set_icon  # (window imports this module)
        self._icon = icon
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        self.places = QListWidget()
        self.places.setObjectName("places")
        self.places.setFixedWidth(210)
        self.places.setIconSize(QSize(18, 18))
        self.places.itemClicked.connect(self._place_clicked)
        lay.addWidget(self.places)

        right = QVBoxLayout()
        right.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.up_btn = QToolButton()
        set_icon(self.up_btn, "go-up-symbolic", "go-up")
        self.up_btn.setToolTip("Up a folder (Backspace)")
        self.up_btn.clicked.connect(self.go_up)
        self.path_label = QLabel("")
        self.path_label.setObjectName("heading")
        self.path_label.setMinimumWidth(1)
        self.path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(200)
        self.filter.textChanged.connect(self._apply_filter)
        self.send_btn = QPushButton("Send files")
        set_icon(self.send_btn, "document-send-symbolic", "document-send", "list-add-symbolic")
        self.send_btn.setToolTip("Send files from this PC into this folder (or drop them on the list)")
        self.send_btn.clicked.connect(self.pick_files)
        self.send_dir_btn = QPushButton("Send folder")
        set_icon(self.send_dir_btn, "folder-new-symbolic", "folder-open-symbolic", "folder")
        self.send_dir_btn.clicked.connect(self.pick_folder)
        self.mkdir_btn = QToolButton()
        set_icon(self.mkdir_btn, "folder-new-symbolic", "folder-new")
        self.mkdir_btn.setToolTip("New folder")
        self.mkdir_btn.clicked.connect(self.new_folder)
        self.refresh_btn = QToolButton()
        set_icon(self.refresh_btn, "view-refresh-symbolic", "view-refresh")
        self.refresh_btn.setToolTip("Look again (F5)")
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.up_btn)
        bar.addWidget(self.path_label, 1)
        bar.addWidget(self.filter)
        bar.addWidget(self.send_btn)
        bar.addWidget(self.send_dir_btn)
        bar.addWidget(self.mkdir_btn)
        bar.addWidget(self.refresh_btn)
        right.addLayout(bar)

        self.stack = QStackedWidget()
        self.list = FileList()
        self.list.setObjectName("files")
        self.list.setColumnCount(3)
        self.list.setHeaderLabels(["Name", "Size", "Modified"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setIconSize(QSize(20, 20))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setSortingEnabled(True)
        self.list.sortByColumn(NAME, Qt.SortOrder.AscendingOrder)
        head = self.list.header()
        head.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(SIZE, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(DATE, QHeaderView.ResizeMode.ResizeToContents)
        head.setStretchLastSection(False)
        self.list.itemActivated.connect(self._activated)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.dropped.connect(self.send)
        self.empty = QLabel("")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.empty)
        right.addWidget(self.stack, 1)

        status_row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setMinimumWidth(1)  # a long note is cut, never widens the window
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(160)
        self.bar.hide()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.hide()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.bar)
        status_row.addWidget(self.stop_btn)
        right.addLayout(status_row)
        lay.addLayout(right, 1)

        for keys, fn in (("Delete", self.delete_selected), ("F2", self.rename_selected),
                         ("Backspace", self.go_up), ("F5", self.refresh), ("Alt+Up", self.go_up)):
            sc = QShortcut(QKeySequence(keys), self.list)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self._show_state()

    # ----------------------------------------------------------- the PSP --
    def attach_psp(self, psp):
        self.psp = psp
        psp.state.connect(self._psp_changed)
        psp.task_progress.connect(self._task_progress)
        psp.task_done.connect(self._task_done)
        self._psp_changed(psp.status, psp.text)

    def ready(self) -> bool:
        return bool(self.psp and self.psp.ready())

    def busy(self) -> bool:
        return self._op is not None

    def _psp_changed(self, status: str, _text: str = ""):
        if status == "ready":
            self._fill_places()
            if not self.folder or pspfiles.split(self.folder)[0] not in self.devices:
                last = str(self.settings.value("files_folder", "") or "")
                ok = last and pspfiles.place_of(last) and pspfiles.split(last)[0] in self.devices
                self.folder = xltlink.psp_path(last) if ok else self._place_path(pspfiles.PLACES[0][1])
            self._mark_place()
            if self.isVisible():
                self.refresh()
        else:
            self.entries = []
            self.list.clear()
            self.devices = []
            self.places.clear()
        self._show_state()

    def _fill_places(self):
        self.devices = list(getattr(self.psp, "devices", None) or ["ms0:"])
        self.places.clear()
        for dev in self.devices:
            if len(self.devices) > 1:
                head = QListWidgetItem(DEVICE_NAMES.get(dev, dev))
                head.setFlags(Qt.ItemFlag.NoItemFlags)
                f = head.font()
                f.setBold(True)
                head.setFont(f)
                self.places.addItem(head)
            for name, folder, icons in pspfiles.PLACES:
                it = QListWidgetItem(self._icon(*icons), name)
                it.setData(PATH_ROLE, f"{dev}/{folder}")
                it.setToolTip(f"{dev}/{folder}")
                self.places.addItem(it)
        self._mark_place()

    def _place_path(self, folder: str) -> str:
        return f"{self.devices[0] if self.devices else 'ms0:'}/{folder}"

    def _mark_place(self):
        dev = pspfiles.split(self.folder)[0] if self.folder else ""
        place = pspfiles.place_of(self.folder) if self.folder else ""
        for i in range(self.places.count()):
            it = self.places.item(i)
            p = it.data(PATH_ROLE)
            it.setSelected(bool(p) and p.lower() == f"{dev}/{place}".lower())

    def _place_clicked(self, it: QListWidgetItem):
        p = it.data(PATH_ROLE)
        if p:
            self.open_folder(p)

    # --------------------------------------------------------- browsing --
    def showEvent(self, e):
        super().showEvent(e)
        if self.ready() and self.folder and not self._listing and not self.entries:
            self.refresh()

    def open_folder(self, path: str, select: list[str] | None = None):
        path = xltlink.psp_path(path)
        if not pspfiles.place_of(path):
            return
        if path.lower() != self.folder.lower():
            self._note = ""
            self.filter.clear()
            self.entries = []
            self.list.clear()
        self.folder = path
        self.settings.setValue("files_folder", path)
        self._select_after = select or []
        self._mark_place()
        self.refresh()

    def go_up(self):
        if not self.folder:
            return
        dev, rest = pspfiles.split(self.folder)
        place = pspfiles.place_of(self.folder)
        if rest.lower() == place.lower():
            return
        self.open_folder(f"{dev}/{rest.rsplit('/', 1)[0]}", [rest.rsplit("/", 1)[1]])

    def refresh(self):
        if not self.ready() or not self.folder:
            self._show_state()
            return
        if self._listing:
            self._relist = True
            return
        folder = self.folder

        def run(link, task):
            try:
                return folder, link.list(folder)
            except xltlink.XltsError as e:
                if e.status == xltlink.E_NOENT:  # an XLT folder not made yet: shown empty
                    return folder, None
                raise
        self._listing = Task("files-list", run)
        self._show_state()
        self.psp.run_task(self._listing)

    def _show_state(self):
        ready = self.ready()
        self.places.setEnabled(ready)
        if not ready:
            self.path_label.setText("")
            self.empty.setText("Connect the PSP to see its files here.\n\nOn the PSP: XLT > Settings > XLT > "
                               "USB sync on, then the USB cable.")
            self.stack.setCurrentWidget(self.empty)
            self.status.setText("")
        else:
            dev, rest = pspfiles.split(self.folder)
            where = DEVICE_NAMES.get(dev, dev) if len(self.devices) > 1 else ""
            self.path_label.setText((f"{where} · " if where else "") + " / ".join(rest.split("/")))
            self.path_label.setToolTip(self.folder)
            if self._listing and not self.entries:
                self.empty.setText("Looking at the PSP…")
                self.stack.setCurrentWidget(self.empty)
            elif not self.entries:
                self.empty.setText("Nothing here yet." + (
                    "\n\nDrop files here to send them to the PSP." if pspfiles.can_add_files(self.folder) else
                    "\n\nEach save is a folder: send a save's folder here." if pspfiles.place_of(self.folder)
                    == pspfiles.SAVES else ""))
                self.stack.setCurrentWidget(self.empty)
            else:
                self.stack.setCurrentWidget(self.list)
            self._show_status()
        self._update_buttons()

    def _show_status(self):
        if self._op:
            return  # its progress is shown instead
        if not self.ready():
            self.status.setText("")
            return
        files = [e for e in self.entries if not e.is_dir]
        dirs = len(self.entries) - len(files)
        parts = []
        if dirs:
            parts.append(f"{dirs} folder{'s' if dirs != 1 else ''}")
        if files:
            parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''}, {games.human(sum(e.size for e in files))}")
        space = self.space() if self.space else None
        if space and space[1]:
            parts.append(f"{games.human(space[0])} free")
        if self._note:
            parts.insert(0, self._note)
        self.status.setText(" · ".join(parts))

    def _fill(self, entries: list[xltlink.Entry]):
        keep = self._select_after or [it.data(0, ENTRY_ROLE).name for it in self.list.selectedItems()]
        self._select_after = []
        self.entries = entries
        self.list.setSortingEnabled(False)
        self.list.clear()
        for e in entries:
            it = FileItem([e.name, pspfiles.size_text(e), pspfiles.human_time(e)])
            it.setData(0, ENTRY_ROLE, e)
            it.setData(0, PATH_ROLE, pspfiles.join(self.folder, e.name))
            it.setIcon(0, self._entry_icon(e))
            it.setTextAlignment(SIZE, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.list.addTopLevelItem(it)
        self.list.setSortingEnabled(True)
        want = {n.lower() for n in keep}
        first = None
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            if it.text(NAME).lower() in want:
                it.setSelected(True)
                first = first or it
        if first:
            self.list.setCurrentItem(first, 0, self.list.selectionCommand(self.list.currentIndex()))
            self.list.scrollToItem(first)
        self._apply_filter()

    def _entry_icon(self, e: xltlink.Entry) -> QIcon:
        if e.is_dir:
            return self._icon("folder-symbolic", "folder")
        mt = self._mime.mimeTypeForFile(e.name, QMimeDatabase.MatchMode.MatchExtension)
        return self._icon(mt.iconName(), mt.genericIconName(), "text-x-generic-symbolic", "text-x-generic")

    def _apply_filter(self):
        text = self.filter.text().strip().lower()
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            it.setHidden(bool(text) and text not in it.text(NAME).lower())

    def _selected(self) -> list[tuple[str, xltlink.Entry]]:
        return [(it.data(0, PATH_ROLE), it.data(0, ENTRY_ROLE)) for it in self.list.selectedItems()
                if not it.isHidden()]

    def _protected(self, path: str) -> bool:
        """XLT's own folder (or anything holding it): never removed or renamed from here."""
        x = (self.xlt_folder() or "").rstrip("/").lower()
        p = path.lower()
        return bool(x) and (x == p or x.startswith(p + "/"))

    def _update_buttons(self):
        ready = self.ready() and bool(self.folder)
        idle = ready and self._op is None
        place = pspfiles.place_of(self.folder) if self.folder else ""
        self.up_btn.setEnabled(ready and pspfiles.split(self.folder)[1].lower() != place.lower())
        self.refresh_btn.setEnabled(ready)
        self.filter.setEnabled(ready)
        self.send_btn.setEnabled(idle and pspfiles.can_add_files(self.folder))
        self.send_dir_btn.setEnabled(idle and pspfiles.can_add_folders(self.folder))
        self.mkdir_btn.setEnabled(idle and pspfiles.can_add_folders(self.folder))
        self.list.accept_files = idle and (pspfiles.can_add_files(self.folder) or
                                          pspfiles.can_add_folders(self.folder))
        if place == pspfiles.SAVES and pspfiles.save_depth(self.folder) == 0:
            self.send_btn.setToolTip("A save's files go inside its folder: open a save, or send a save's folder")
        else:
            self.send_btn.setToolTip("Send files from this PC into this folder (or drop them on the list)")

    def _activated(self, it: QTreeWidgetItem, _col: int = 0):
        e = it.data(0, ENTRY_ROLE)
        if e.is_dir:
            if pspfiles.place_of(self.folder) == pspfiles.SAVES and pspfiles.save_depth(self.folder) == 1:
                return  # (a save folder holds no folders; nothing to go into)
            self.open_folder(it.data(0, PATH_ROLE))
        else:
            self.open_selected()

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if it and not it.isSelected():
            self.list.clearSelection()
            it.setSelected(True)
        sel = self._selected()
        idle = self.ready() and self._op is None
        menu = QMenu(self)
        if len(sel) == 1:
            e = sel[0][1]
            menu.addAction("Open folder" if e.is_dir else "Open", lambda: self._activated(it)).setEnabled(
                self.ready() and (e.is_dir or idle))
        if sel:
            menu.addAction("Copy to this PC…", self.copy_selected).setEnabled(idle)
            menu.addSeparator()
            changeable = all(pspfiles.can_change(p) and not self._protected(p) for p, _ in sel)
            if len(sel) == 1:
                menu.addAction("Rename…", self.rename_selected).setEnabled(idle and changeable)
            menu.addAction("Delete", self.delete_selected).setEnabled(idle and changeable)
            menu.addSeparator()
        menu.addAction("Send files…", self.pick_files).setEnabled(self.send_btn.isEnabled())
        menu.addAction("Send folder…", self.pick_folder).setEnabled(self.send_dir_btn.isEnabled())
        menu.addAction("New folder…", self.new_folder).setEnabled(self.mkdir_btn.isEnabled())
        menu.addSeparator()
        menu.addAction("Look again", self.refresh).setEnabled(self.ready())
        menu.exec(self.list.viewport().mapToGlobal(pos))

    # ----------------------------------------------------------- the work --
    def _start(self, name: str, label: str, fn, data=None) -> bool:
        if self._op or not self.ready():
            return False
        self._op = Task(name, fn, data)
        self._op.label = label
        self._note = ""
        self.status.setText(f"{label}…")
        self.bar.setValue(0)
        self.bar.show()
        self.stop_btn.show()
        self._update_buttons()
        self.psp.run_task(self._op)
        return True

    def stop(self):
        if self._op:
            self._op.cancel.set()

    def _task_progress(self, task: Task, frac: float, text: str):
        if task is self._op:
            self.bar.setValue(int(frac * 1000))
            self.status.setText(f"{task.label} · {frac * 100:.0f}%" + (f" · {text}" if text else ""))

    def _task_done(self, task: Task):
        if task is self._listing:
            self._listing = None
            if not task.error and task.result:
                folder, entries = task.result
                if folder.lower() == self.folder.lower():
                    self._fill(entries or [])
            elif task.error and task.error != "cancelled":
                self._note = task.error
            if self._relist:
                self._relist = False
                self.refresh()
            self._show_state()
            return
        if task is not self._op:
            return
        self._op = None
        self.bar.hide()
        self.stop_btn.hide()
        if task.error == "cancelled":
            self._note = "Stopped"
        elif task.error:
            self._note = ""
            QMessageBox.warning(self, "XLT Tool", task.error)
        else:
            self._note = task.result.get("note", "") if isinstance(task.result, dict) else ""
            if isinstance(task.result, dict) and task.result.get("open"):
                QDesktopServices.openUrl(QUrl.fromLocalFile(task.result["open"]))
        if task.name in ("files-send", "files-delete", "files-rename", "files-mkdir"):
            self._select_after = task.data or []
            self.changed.emit()
            self.refresh()  # (after an error too: part of it may have happened)
        self._show_state()

    # sending
    def pick_files(self):
        if not self.send_btn.isEnabled():
            return
        start = str(self.settings.value("files_send_dir", "") or os.path.expanduser("~"))
        files, _ = QFileDialog.getOpenFileNames(self, "Send to the PSP", start)
        if files:
            self.settings.setValue("files_send_dir", os.path.dirname(files[0]))
            self.send(files)

    def pick_folder(self):
        if not self.send_dir_btn.isEnabled():
            return
        start = str(self.settings.value("files_send_dir", "") or os.path.expanduser("~"))
        d = QFileDialog.getExistingDirectory(self, "Send a folder to the PSP", start)
        if d:
            self.settings.setValue("files_send_dir", os.path.dirname(d.rstrip("/")))
            self.send([d])

    def send(self, paths: list[str]):
        if self._op or not self.ready() or not paths:
            return
        folder = self.folder
        have = {e.name.lower() for e in self.entries}
        names = [xltlink.psp_name(os.path.basename(p.rstrip(os.sep))) for p in paths]
        clash = [n for n in names if n.lower() in have]
        replace = False
        if clash:
            shown = "\n".join(clash[:8]) + ("\n…" if len(clash) > 8 else "")
            box = QMessageBox(QMessageBox.Icon.Question, "Already on the PSP",
                              f"{len(clash)} of these {'is' if len(clash) == 1 else 'are'} in this folder already:"
                              f"\n\n{shown}", parent=self)
            rep = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
            skip = box.addButton("Skip those", QMessageBox.ButtonRole.AcceptRole) if len(clash) < len(names) else None
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is rep:
                replace = True
            elif skip is None or box.clickedButton() is not skip:
                return
        try:
            plan = pspfiles.plan_upload(None, paths, folder, have, replace)
        except (OSError, xltlink.XltsError) as e:
            QMessageBox.warning(self, "XLT Tool", str(e))
            return
        refused = [(p, why) for p, why in plan.skipped if why != "already there"]
        if refused:
            QMessageBox.information(self, "XLT Tool", "Left out:\n\n" + "\n".join(
                f"{os.path.basename(p)}: {why}" for p, why in refused[:10]))
        if not plan.files and not plan.folders:
            return
        space = self.space() if self.space else None
        if space and space[1] and plan.total > space[0]:
            QMessageBox.warning(self, "XLT Tool", f"That is {games.human(plan.total)}, and the PSP has "
                                f"{games.human(space[0])} free.")
            return

        def run(link, task):
            written = pspfiles.upload(link, plan, replace, lambda f, n: task.report(f, n), task.cancelled)
            n = len(written)
            note = f"Sent {n} file{'s' if n != 1 else ''}" if n else "Folders made"
            if plan.ascii:
                a, m = plan.ascii[0]
                more = f" and {len(plan.ascii) - 1} more" if len(plan.ascii) > 1 else ""
                note += f"; the PSP refused “{a}”{more}, sent in Latin letters as “{m}”"
            return dict(folder=folder, note=note)
        label = f"Sending {len(plan.files)} file{'s' if len(plan.files) != 1 else ''}"
        self._start("files-send", label, run, [n for n in names if n.lower() not in have or replace])

    def new_folder(self):
        if not self.mkdir_btn.isEnabled():
            return
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        name = xltlink.psp_name(name.strip()) if ok and name.strip() else ""
        if not name:
            return
        if name.lower() in {e.name.lower() for e in self.entries}:
            QMessageBox.information(self, "XLT Tool", f"“{name}” is there already.")
            return
        path = pspfiles.join(self.folder, name)
        folder = self.folder
        def run(link, task):
            made = link.mkdir(path)
            made = made.rsplit("/", 1)[-1] if isinstance(made, str) else name
            return dict(folder=folder, note="" if made == name else
                        f"The PSP refused “{name}”: made in Latin letters as “{made}”")
        self._start("files-mkdir", "Making the folder", run, [name])

    # copying to the PC, opening
    def copy_selected(self):
        sel = self._selected()
        if not sel or self._op:
            return
        start = str(self.settings.value("files_copy_dir", "") or os.path.expanduser("~"))
        dest = QFileDialog.getExistingDirectory(self, "Copy to which folder on this PC?", start)
        if not dest:
            return
        self.settings.setValue("files_copy_dir", dest)

        def run(link, task):
            made = pspfiles.download(link, sel, dest, lambda f, n: task.report(f, n), task.cancelled)
            return dict(note=f"Copied {len(made)} to {os.path.basename(dest.rstrip(os.sep)) or dest}")
        self._start("files-copy", f"Copying {len(sel)} to this PC", run)

    def open_selected(self):
        sel = [(p, e) for p, e in self._selected() if not e.is_dir][:1]
        if not sel or self._op:
            return
        path, e = sel[0]
        # a copy in the cache, opened with the PC's app for it; the same file again isn't copied again
        tag = f"{e.size}-{'-'.join(str(x) for x in e.mtime)}"
        dest = os.path.join(_cache_dir(), tag)
        local = os.path.join(dest, pspfiles.local_name(e.name))
        if os.path.exists(local):
            QDesktopServices.openUrl(QUrl.fromLocalFile(local))
            return

        def run(link, task):
            def step(got, size):
                if task.cancelled():
                    raise InterruptedError
                task.report(got / max(size, 1))
            os.makedirs(dest, exist_ok=True)
            link.get(path, local, progress=step)
            return dict(open=local)
        self._start("files-open", f"Opening {e.name}", run)

    # renaming, deleting
    def rename_selected(self):
        sel = self._selected()
        if len(sel) != 1 or self._op:
            return
        path, e = sel[0]
        if not pspfiles.can_change(path) or self._protected(path):
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        name = name.strip() if ok else ""
        if not name or name == e.name:
            return
        new = xltlink.psp_name(name)
        if new.lower() != e.name.lower() and new.lower() in {x.name.lower() for x in self.entries}:
            QMessageBox.information(self, "XLT Tool", f"“{new}” is there already.")
            return
        folder = self.folder
        self._start("files-rename", "Renaming", lambda link, task: dict(
            folder=folder, note=f"Renamed to {pspfiles.rename(link, path, name).rsplit('/', 1)[-1]}"), [new])

    def delete_selected(self):
        sel = self._selected()
        if not sel or self._op or not self.ready():
            return
        if not all(pspfiles.can_change(p) and not self._protected(p) for p, _ in sel):
            if any(self._protected(p) for p, _ in sel):
                QMessageBox.information(self, "XLT Tool", "That is XLT's own folder: it stays.")
            return
        saves = any(pspfiles.save_depth(p) for p, _ in sel)
        what = f"“{sel[0][1].name}”" if len(sel) == 1 else f"these {len(sel)} items"
        dirs = sum(1 for _, e in sel if e.is_dir)
        text = f"Delete {what} from the PSP?"
        if dirs:
            text += "\n\nFolders go with everything in them."
        if saves:
            text += "\n\nThe saves are backed up on this PC first (the Library can put them back)."
        else:
            text += "\n\nThis can't be undone."
        box = QMessageBox(QMessageBox.Icon.Warning, "Delete from the PSP", text, parent=self)
        yes = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return
        folder = self.folder

        def run(link, task):
            kept = []
            if saves:
                kept = pspfiles.back_up_saves(link, [p for p, _ in sel], lambda f: task.report(f * 0.5, "backing up"),
                                              task.cancelled)
            done = pspfiles.remove(link, sel, lambda f, n: task.report((0.5 if saves else 0) + f * (0.5 if saves else 1),
                                                                       n), task.cancelled)
            n = done["files"]
            note = f"Deleted {n} file{'s' if n != 1 else ''}" if n else "Deleted"
            if kept:
                note += f" (saves backed up: {len(kept)})"
            return dict(folder=folder, note=note)
        self._start("files-delete", f"Deleting {what}", run)
