"""The editor for a song's tags, cover and lyrics (one song, or several at once)."""

from __future__ import annotations

import os
import re
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from . import convert

LABELS = (("title", "Title"), ("artist", "Artist"), ("album", "Album"),
          ("track", "Track"), ("date", "Year"), ("genre", "Genre"))
ONE_SONG_ONLY = ("title", "track")
COVER_SIZE = 180
TIMED = re.compile(r"^\s*\[\d+:\d+(?:[.:]\d+)?\]", re.M)


class TagDialog(QDialog):
    def __init__(self, parent, jobs: list):
        super().__init__(parent)
        self.jobs = jobs
        self.single = jobs[0] if len(jobs) == 1 else None
        self.setWindowTitle(f"Edit {len(jobs)} songs" if not self.single else "Edit song")
        self.setMinimumWidth(560)
        self.art = None                 # None keep, "" remove, otherwise a file

        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(16)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fields: dict[str, QLineEdit] = {}
        self.start: dict[str, str] = {}
        for name, label in LABELS:
            edit = QLineEdit()
            values = {j.info.value(name, j.edit) for j in jobs}
            value = values.pop() if len(values) == 1 else ""
            if len(jobs) > 1 and name in ONE_SONG_ONLY:
                edit.setEnabled(False)
                edit.setPlaceholderText("Set per song")
            elif len(jobs) > 1 and not value:
                edit.setPlaceholderText("Keep each song's own")
            edit.setText(value)
            self.fields[name], self.start[name] = edit, value
            form.addRow(label, edit)
        top.addLayout(form, 1)

        art_col = QVBoxLayout()
        self.cover = QLabel()
        self.cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setObjectName("muted")
        self.cover.setStyleSheet("background: rgba(0,0,0,0.25); border-radius: 8px;")
        art_col.addWidget(self.cover)
        buttons = QHBoxLayout()
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_art)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_art)
        buttons.addWidget(choose)
        buttons.addWidget(remove)
        art_col.addLayout(buttons)
        art_col.addStretch(1)
        top.addLayout(art_col)
        outer.addLayout(top)

        self.lyrics = QPlainTextEdit()
        self.lyrics_start = ""
        self.lyrics_note = QLabel()
        self.lyrics_note.setObjectName("muted")
        self.embed = QCheckBox("Also store the lyrics inside the music file")
        self.embed.setToolTip("XLT reads a .lrc file first, then lyrics stored in the song.")
        if self.single:
            head = QHBoxLayout()
            title = QLabel("Lyrics")
            title.setObjectName("heading")
            head.addWidget(title)
            head.addWidget(self.lyrics_note, 1)
            load = QPushButton("Load .lrc…")
            load.clicked.connect(self._load_lyrics)
            clear = QPushButton("Clear")
            clear.clicked.connect(self.lyrics.clear)
            head.addWidget(load)
            head.addWidget(clear)
            outer.addLayout(head)
            job = self.single
            self.lyrics_start = convert.lyrics_text(job.info, job.edit)
            self.lyrics.setPlainText(self.lyrics_start)
            self.lyrics.setPlaceholderText("[00:12.50] A line that shows at 12.5 seconds")
            self.lyrics.setMinimumHeight(150)
            self.lyrics.textChanged.connect(self._count_lyrics)
            self._count_lyrics()
            outer.addWidget(self.lyrics, 1)
            self.embed.setChecked(job.edit.embed_lyrics)
            outer.addWidget(self.embed)
        else:
            self.lyrics.hide()
            self.embed.hide()

        self.in_place = QCheckBox("Update the original file instead of making a copy")
        self.in_place.setToolTip("Changes the file you added. Nothing is converted, and there's no undo.")
        taggable = all(os.path.splitext(j.path)[1].lower() in convert.TAGGABLE for j in jobs)
        self.in_place.setEnabled(taggable)
        self.in_place.setChecked(taggable and all(j.edit.in_place for j in jobs))
        if not taggable:
            self.in_place.setToolTip("Only MP3, M4A and FLAC files can be updated directly.")
        outer.addWidget(self.in_place)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        outer.addWidget(box)
        self._show_starting_art()

    # ------------------------------------------------------------------ cover --
    def _preview(self, path: str, stream: int = -1) -> bool:
        try:
            res = subprocess.run(convert.preview_command(path, COVER_SIZE, stream),
                                 capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        pm = QPixmap()
        if res.returncode != 0 or not pm.loadFromData(res.stdout):
            return False
        self.cover.setText("")
        self.cover.setPixmap(pm)
        return True

    def _show_none(self, text: str):
        self.cover.setPixmap(QPixmap())
        self.cover.setText(text)

    def _show_starting_art(self):
        job = self.single
        if not job:
            self._show_none("Keep each song's own")
            return
        if job.edit.art == "":
            self._show_none("No cover")
        elif job.edit.art:
            self._preview(job.edit.art)
        elif job.info.cover_stream < 0 or not self._preview(job.path, job.info.cover_stream):
            self._show_none("No cover")

    def _choose_art(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a cover", os.path.dirname(self.jobs[0].path),
            "Pictures (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff *.heic *.avif);;All files (*)")
        if not path:
            return
        if self._preview(path):
            self.art = path
        else:
            QMessageBox.warning(self, "Can't use that picture", "This file couldn't be read as a picture.")

    def _remove_art(self):
        self.art = ""
        self._show_none("No cover")

    # ----------------------------------------------------------------- lyrics --
    def _count_lyrics(self):
        text = self.lyrics.toPlainText()
        timed = len(TIMED.findall(text))
        if not text.strip():
            self.lyrics_note.setText("")
        elif timed:
            self.lyrics_note.setText(f"{timed} timed line{'s' if timed != 1 else ''}")
        else:
            self.lyrics_note.setText("No timed lines: XLT only shows lines with a [mm:ss] time")

    def _load_lyrics(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load lyrics", os.path.dirname(self.jobs[0].path),
                                              "Lyrics (*.lrc *.txt);;All files (*)")
        if path:
            try:
                self.lyrics.setPlainText(convert.read_text_file(path))
            except OSError as e:
                QMessageBox.warning(self, "Can't read that file", str(e))

    # ------------------------------------------------------------------- save --
    def save(self):
        for job in self.jobs:
            for name, widget in self.fields.items():
                if not widget.isEnabled():
                    continue
                text = widget.text().strip()
                if self.single:
                    if text != job.info.value(name):
                        job.edit.tags[name] = text
                    else:
                        job.edit.tags.pop(name, None)
                elif text != self.start[name] and text:
                    job.edit.tags[name] = text
            if self.art is not None:
                job.edit.art = self.art
            job.edit.in_place = self.in_place.isChecked()
        if self.single:
            text = self.lyrics.toPlainText()
            if text != self.lyrics_start:
                self.single.edit.lyrics = text
            self.single.edit.embed_lyrics = self.embed.isChecked()
        self.accept()
