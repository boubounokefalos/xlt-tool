#!/usr/bin/env python3
"""XLT Tool: the README's screenshots, walk-through video and GIF teaser, rendered from the
real window, offscreen, over a made-up library.

    tools/make_demo_media.py [all|stills|video] [OUTDIR]     (default: all, into docs/media)

Run it with the Python run-dev.sh uses (PySide6, jeepney, mutagen); it needs ffmpeg.
Everything shown is invented: the games (their titles are XLT's demo library's), their icons
and backgrounds are drawn here, the songs are tones, the pictures and the book are made here,
and the PSP is a pretend one (a folder that answers like XLT's USB sync, from tests/test_psplib.py).
Box art stays off and nothing is sent anywhere. Settings, caches and save backups live in a
scratch home (/tmp/xlt-tool-demo), so your own are never touched.

Writes OUTDIR/shot_*.png, OUTDIR/xlt_tool_demo.mp4 and OUTDIR/xlt_tool_teaser.gif.
"""
import hashlib
import math
import os
import shutil
import struct
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = "/tmp/xlt-tool-demo"
HOME = os.path.join(WORK, "home")
PSP = os.path.join(WORK, "psp")
CACHE = os.path.join(WORK, "cache")       # generated media, kept between runs
W, H = 1180, 720
FPS = 30
FONT_FILE = "/usr/share/fonts/noto/NotoSans-Medium.ttf"


# ------------------------------------------------------------------- the scratch home --
def prepare_env(intro: bool):
    if os.path.isdir(HOME):
        shutil.rmtree(HOME)
    if os.path.isdir(PSP):
        shutil.rmtree(PSP)
    os.makedirs(HOME)
    os.makedirs(CACHE, exist_ok=True)
    os.environ.update(HOME=HOME, XDG_CONFIG_HOME=f"{HOME}/.config", XDG_CACHE_HOME=f"{HOME}/.cache",
                      XDG_DATA_HOME=f"{HOME}/.local/share", QT_QPA_PLATFORM="offscreen",
                      LANG="en_US.UTF-8", TZ=os.environ.get("TZ", "UTC"))
    os.environ.pop("XLTTOOL_NO_INTRO", None)
    if not intro:
        os.environ["XLTTOOL_NO_INTRO"] = "1"
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "tests"))
    sys.path.append(os.path.join(REPO, "dev"))   # pyusb and jeepney, as run-dev.sh has them


# ----------------------------------------------------------------------- made-up art --
def seed(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16)


def art_png(title: str, w: int, h: int, big: bool = False) -> bytes:
    """A title card: a two-colour gradient, soft circles and the title, from the title's hash."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QRadialGradient
    s = seed(title)
    h1 = (s % 360) / 360
    h2 = (h1 + 0.12 + (s >> 9) % 30 / 100) % 1
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, w * 0.4, h)
    g.setColorAt(0, QColor.fromHsvF(h1, 0.62, 0.62))
    g.setColorAt(1, QColor.fromHsvF(h2, 0.75, 0.30))
    p.fillRect(img.rect(), g)
    for k in range(9):
        r = (0.12 + ((s >> (k * 5)) % 23) / 60) * h
        cx = ((s >> (k * 7)) % 1000) / 1000 * w
        cy = ((s >> (k * 11)) % 1000) / 1000 * h
        rg = QRadialGradient(cx, cy, r)
        c = QColor.fromHsvF((h1 + 0.5 * (k % 2)) % 1, 0.25, 1.0, 0.22)
        rg.setColorAt(0, c)
        c2 = QColor(c)
        c2.setAlphaF(0)
        rg.setColorAt(1, c2)
        p.setBrush(rg)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    p.fillRect(QRectF(0, h * 0.62, w, h * 0.04), QColor(255, 255, 255, 40))
    text = title.split(":")[0].upper()
    f = QFont("Noto Sans")
    f.setWeight(QFont.Weight.Black)
    size = h * (0.16 if big else 0.22)
    while True:
        f.setPixelSize(max(6, int(size)))
        p.setFont(f)
        if p.fontMetrics().horizontalAdvance(text) <= w * 0.86 or size < 7:
            break
        size *= 0.92
    rect = QRectF(0, 0, w, h * 0.92)
    p.setPen(QColor(0, 0, 0, 110))
    p.drawText(rect.translated(1.5, 2), Qt.AlignmentFlag.AlignCenter, text)
    p.setPen(QColor(255, 255, 255, 235))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


# --------------------------------------------------------------------- made-up games --
SECTOR = 2048


def sfo(entries: list) -> bytes:
    keys, data, idx = b"", b"", b""
    for k, v in entries:
        if isinstance(v, int):
            raw, fmt, size = struct.pack("<I", v), 0x0404, 4
        else:
            raw = v.encode() + b"\0"
            fmt, size = 0x0204, (len(raw) + 3) & ~3
        idx += struct.pack("<HHIII", len(keys), fmt, len(raw), size, len(data))
        keys += k.encode() + b"\0"
        data += raw.ljust(size, b"\0")
    keys = keys.ljust((len(keys) + 3) & ~3, b"\0")
    kstart = 20 + len(idx)
    return b"\0PSF" + struct.pack("<IIII", 0x101, kstart, kstart + len(keys), len(entries)) + idx + keys + data


def _both32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def _dirrec(name, lba, size, is_dir):
    n = name if isinstance(name, bytes) else name.encode()
    rec = (b"\0\0" + _both32(lba) + _both32(size) + bytes(7) + bytes([2 if is_dir else 0, 0, 0]) +
           struct.pack("<H", 1) + struct.pack(">H", 1) + bytes([len(n)]) + n)
    if len(rec) % 2:
        rec += b"\0"
    return bytes([len(rec)]) + rec[1:]


def make_iso(path: str, title: str, disc_id: str, mb: int, version: str = "1.00", fw: str = "6.20"):
    """A PSP ISO with PSP_GAME/PARAM.SFO, ICON0.PNG and PIC1.PNG, the rest sparse (mb in size)."""
    param = sfo([("CATEGORY", "UG"), ("DISC_ID", disc_id), ("DISC_VERSION", version),
                 ("PARENTAL_LEVEL", 1), ("PSP_SYSTEM_VER", fw), ("TITLE", title)])
    icon, pic = art_png(title, 144, 80), art_png(title, 480, 272, big=True)
    files = [("ICON0.PNG;1", icon), ("PARAM.SFO;1", param), ("PIC1.PNG;1", pic)]
    root_lba, game_lba, lba = 18, 19, 20
    placed = []
    for name, blob in files:
        placed.append((name, lba, blob))
        lba += (len(blob) + SECTOR - 1) // SECTOR
    total = max(lba, mb * 1024 * 1024 // SECTOR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pvd = bytearray(SECTOR)
        pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
        pvd[80:88] = _both32(total)
        pvd[156:190] = _dirrec(b"\0", root_lba, SECTOR, True)
        f.seek(16 * SECTOR)
        f.write(pvd)
        f.write(b"\xffCD001".ljust(SECTOR, b"\0"))
        root = _dirrec(b"\0", root_lba, SECTOR, True) + _dirrec(b"\1", root_lba, SECTOR, True) + \
            _dirrec("PSP_GAME", game_lba, SECTOR, True)
        f.seek(root_lba * SECTOR)
        f.write(root.ljust(SECTOR, b"\0"))
        game = _dirrec(b"\0", game_lba, SECTOR, True) + _dirrec(b"\1", root_lba, SECTOR, True) + \
            b"".join(_dirrec(n, lb, len(b), False) for n, lb, b in placed)
        f.write(game.ljust(SECTOR, b"\0"))
        for _n, lb, blob in placed:
            f.seek(lb * SECTOR)
            f.write(blob)
        f.truncate(total * SECTOR)


def make_pbp(path: str, title: str, category: str, disc_id: str = "", extra_mb: int = 0):
    entries = [("CATEGORY", category), ("PSP_SYSTEM_VER", "5.00"), ("TITLE", title)]
    if disc_id:
        entries.insert(1, ("DISC_ID", disc_id))
    param = sfo(entries)
    parts = [param, art_png(title, 144, 80), b"", b"", art_png(title, 480, 272, big=True), b"", b"ELF", b""]
    offs, at = [], 40
    for blob in parts:
        offs.append(at)
        at += len(blob)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0PBP" + struct.pack("<I8I", 0x10000, *offs) + b"".join(parts))
        if extra_mb:
            f.truncate(at + extra_mb * 1024 * 1024)


def make_save(folder: str, title: str, detail: str):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "PARAM.SFO"), "wb") as f:
        f.write(sfo([("CATEGORY", "MS"), ("SAVEDATA_DETAIL", detail), ("SAVEDATA_TITLE", title),
                     ("TITLE", title)]))
    with open(os.path.join(folder, "ICON0.PNG"), "wb") as f:
        f.write(art_png(title, 144, 80))
    with open(os.path.join(folder, "DATA.BIN"), "wb") as f:
        f.write(os.urandom(48 * 1024))


GAMES_PC = [  # (file, title, id, MB)
    ("Astral Drift.iso", "Astral Drift", "ULUS99101", 588),
    ("Citadel of Echoes.iso", "Citadel of Echoes: The Long Night", "ULES99102", 1240),
    ("Glowline.iso", "Glowline", "ULUS99103", 214),
    ("Hollow Peaks Expedition Unlimited.iso", "Hollow Peaks Expedition Unlimited", "ULUS99104", 196),
    ("Kite Racer 2.iso", "Kite Racer 2", "ULJM99105", 902),
    ("Paper Racers 2.iso", "Paper Racers 2", "ULES99106", 377),
]


def make_library():
    games = os.path.join(HOME, "Games")
    for fn, title, gid, mb in GAMES_PC:
        make_iso(os.path.join(games, fn), title, gid, mb)
    make_pbp(os.path.join(games, "PS1", "Moonlit Arcade", "EBOOT.PBP"), "Moonlit Arcade", "ME", "SLUS99107", 310)
    make_pbp(os.path.join(games, "Homebrew", "Quiet Orbit", "EBOOT.PBP"), "Quiet Orbit", "MG", extra_mb=2)
    make_pbp(os.path.join(games, "Homebrew", "Bramble", "EBOOT.PBP"), "Bramble", "MG", extra_mb=1)

    # the pretend PSP: some of the same games, one only there, saves, XLT and its play history
    iso = os.path.join(PSP, "ISO")
    make_iso(os.path.join(iso, "Astral Drift.iso"), "Astral Drift", "ULUS99101", 588)
    make_iso(os.path.join(iso, "Kite Racer 2.iso"), "Kite Racer 2", "ULJM99105", 902)
    make_iso(os.path.join(iso, "Paper Racers 2.iso"), "Paper Racers 2", "ULES99106", 377)
    make_iso(os.path.join(iso, "Starfall Tactics.iso"), "Starfall Tactics", "ULUS99110", 731)
    make_pbp(os.path.join(PSP, "PSP", "GAME", "Quiet Orbit", "EBOOT.PBP"), "Quiet Orbit", "MG", extra_mb=2)
    make_pbp(os.path.join(PSP, "PSP", "GAME", "Moonlit Arcade", "EBOOT.PBP"), "Moonlit Arcade", "ME",
             "SLUS99107", 310)
    xlt = os.path.join(PSP, "PSP", "GAME", "XLT")
    os.makedirs(os.path.join(xlt, "cache"), exist_ok=True)
    open(os.path.join(xlt, "EBOOT.PBP"), "wb").write(b"\0PBP")
    open(os.path.join(xlt, "XLTSYNC.PRX"), "wb").write(b"\0")
    now = int(time.time())
    played = [("iso", "ms0:/ISO/Astral Drift.iso", "ULUS99101", now - 3600 * 20, 12 * 3600 + 40 * 60, 1,
               "Astral Drift"),
              ("iso", "ms0:/ISO/Kite Racer 2.iso", "ULJM99105", now - 86400 * 4, 3 * 3600 + 5 * 60, 0, "Kite Racer 2"),
              ("iso", "ms0:/ISO/Starfall Tactics.iso", "ULUS99110", now - 600, 27 * 3600, 1, "Starfall Tactics"),
              ("ps1", "ms0:/PSP/GAME/Moonlit Arcade/EBOOT.PBP", "SLUS99107", now - 86400 * 12, 95 * 60, 0,
               "Moonlit Arcade")]
    with open(os.path.join(xlt, "cache", "games.txt"), "w") as f:
        f.write("XLT games 1\n")
        for row in played:
            f.write("\t".join(str(x) for x in row) + "\n")
    sav = os.path.join(PSP, "PSP", "SAVEDATA")
    make_save(os.path.join(sav, "ULUS99101DATA00"), "Astral Drift", "Sector 7 · 64%")
    make_save(os.path.join(sav, "ULUS99101SYSTEM"), "Astral Drift", "Settings")
    make_save(os.path.join(sav, "ULUS99110DATA01"), "Starfall Tactics", "Chapter 9")
    for d in ("VIDEO", "MUSIC", "PICTURE", "BOOKS", "DOWNLOADS"):
        os.makedirs(os.path.join(PSP, d), exist_ok=True)

    # save backups on the PC
    from xlttool import games as g
    for gid, folder, title, detail, days in (("ULUS99101", "ULUS99101DATA00", "Astral Drift", "Sector 5 · 41%", 9),
                                             ("ULUS99101", "ULUS99101DATA00", "Astral Drift", "Sector 7 · 64%", 1),
                                             ("ULES99102", "ULES99102SAVE", "Citadel of Echoes", "The Bell Tower",
                                              30)):
        p = g.new_backup_dir(gid, folder, when=now - 86400 * days)
        with open(os.path.join(p, "PARAM.SFO"), "wb") as f:
            f.write(sfo([("SAVEDATA_DETAIL", detail), ("SAVEDATA_TITLE", title)]))
        with open(os.path.join(p, "DATA.BIN"), "wb") as f:
            f.write(os.urandom(32 * 1024))


# --------------------------------------------------------------- made-up media files --
def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_media() -> list[str]:
    """Videos, songs, a picture and a book for the Convert queue; made once into CACHE."""
    def clip(title, w, h, secs, freq):
        def make(o):  # a drawn scene, slowly zooming, with a tone
            still = os.path.join(CACHE, title + ".png")
            open(still, "wb").write(art_png(title, w * 2, h * 2, big=True))
            ff("-loop", "1", "-framerate", "30", "-t", str(secs), "-i", still, "-f", "lavfi", "-i",
               f"sine=f={freq}:d={secs}", "-vf",
               f"zoompan=z='1+0.0015*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps=30",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", o)
        return make
    want = {"Mountain Trail.mkv": clip("Mountain Trail", 1280, 720, 8, 330),
            "City Lights.mp4": clip("City Lights", 1920, 1080, 6, 440)}
    out = []
    for name, make in want.items():
        p = os.path.join(CACHE, name)
        if not os.path.exists(p):
            make(p)
        out.append(p)
    cover = os.path.join(CACHE, "cover.png")
    if not os.path.exists(cover):
        open(cover, "wb").write(art_png("Low Tide", 500, 500, big=True))
    for i, (title, freq) in enumerate((("Low Tide", 220), ("Undertow", 262), ("Pale Mint", 294))):
        p = os.path.join(CACHE, f"Mira Ostend - {title}.flac")
        if not os.path.exists(p):
            ff("-f", "lavfi", "-i", f"sine=f={freq}:d=20", "-i", cover, "-map", "0", "-map", "1", "-c:a", "flac",
               "-c:v", "png", "-disposition:v", "attached_pic", "-metadata", f"title={title}",
               "-metadata", "artist=Mira Ostend", "-metadata", "album=Low Tide", "-metadata", f"track={i + 1}",
               "-metadata", "date=2024", p)
        out.append(p)
    pic = os.path.join(CACHE, "Harbour at Dusk.webp")
    if not os.path.exists(pic):
        tmp = os.path.join(CACHE, "harbour.png")
        open(tmp, "wb").write(art_png("Harbour", 1600, 1000, big=True))
        ff("-i", tmp, pic)
    out.append(pic)
    book = os.path.join(CACHE, "The Lighthouse Keeper.epub")
    if not os.path.exists(book):
        import zipfile
        with zipfile.ZipFile(book, "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                       'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                       'full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                       'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The '
                       'Lighthouse Keeper</dc:title></metadata><manifest><item id="c1" href="c1.xhtml" '
                       'media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>')
            z.writestr("c1.xhtml", "<html><body><h1>One</h1>" + "<p>The lamp turned all night.</p>" * 200 +
                       "</body></html>")
    out.append(book)
    dest = os.path.join(HOME, "Videos")
    os.makedirs(dest, exist_ok=True)
    copies = []
    for p in out:
        q = os.path.join(dest, os.path.basename(p))
        shutil.copyfile(p, q)
        copies.append(q)
    return copies


# ----------------------------------------------------------------------- the pretend PSP --
def install_pretend_psp():
    from test_psplib import FakeLink
    from xlttool import xltlink

    class DemoLink(FakeLink):
        def __init__(self, backend=None):
            super().__init__(PSP)
            self.info = {"model": 2, "devices": 1, "buf_size": 256 * 1024, "put_chunk": 128 * 1024,
                         "protocol": 4}
            self.dev = None
            self.free, self.total = 21_400_000_000, 31_900_000_000

        def hello(self):
            return {}

        def power(self):
            return dict(battery=76, charging=True, plugged=True, minutes=-1)

        def put(self, local, remote, replace=False, progress=None):
            """Like sending: a little at a time; a big game only as its start (the rest sparse)."""
            q = self._local(xltlink.psp_shown(remote))
            os.makedirs(os.path.dirname(q), exist_ok=True)
            size = os.path.getsize(local)
            steps = 24 if size > 4 << 20 else 6
            with open(local, "rb") as src, open(q, "wb") as dst:
                dst.write(src.read(min(size, 1 << 20)))
                dst.truncate(size)
            for k in range(1, steps + 1):
                time.sleep(0.05)
                if progress:
                    progress(size * k // steps, size)
            return {"bytes": size, "seconds": 1.0, "psp_seconds": 1.0}

    xltlink.Link = DemoLink


# --------------------------------------------------------------------------- the app --
def start_app(dark: bool = True):
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon
    from PySide6.QtWidgets import QApplication
    from xlttool import theme
    QGuiApplication.setDesktopFileName("xlt-tool")
    app = QApplication(["xlt-tool"])
    app.setApplicationName("xlt-tool")
    app.setApplicationDisplayName("XLT Tool")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(os.path.join(REPO, "xlttool", "xlt-tool.svg")))
    QIcon.setThemeSearchPaths(["/usr/share/icons", ":/icons"])  # (the scratch home has no icons of its own)
    QIcon.setThemeName("Adwaita")
    theme.install_menu_polish(app)
    apply_theme(app, dark)
    s = QSettings("xlt-tool", "xlt-tool")
    s.setValue("migrated_from_pspconv", True)
    s.setValue("update_check", "false")
    s.setValue("covers_online", "false")
    s.setValue("games_dirs", [os.path.join(HOME, "Games")])
    s.setValue("view_name", "library")
    s.sync()
    return app


def apply_theme(app, dark: bool):
    from PySide6.QtGui import QColor, QFont
    from xlttool import theme
    accent = QColor("#3584e4")
    t = theme.Theme(dark, accent, theme.build_palette(theme._adwaita_colors(dark), accent, dark),
                    QFont("Noto Sans", 10), "demo")
    app.setPalette(t.palette)
    app.setFont(t.font)
    app.setStyleSheet(theme.stylesheet(t))


def grab(win):
    """The window as it looks, with any popup menu or dialog drawn over it where it is."""
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QApplication
    img = win.grab().toImage()
    p = QPainter(img)
    origin = win.mapToGlobal(win.rect().topLeft())
    for w in QApplication.topLevelWidgets():
        if w is win or not w.isVisible() or not w.isWindow():
            continue
        pos = w.mapToGlobal(w.rect().topLeft()) - origin
        p.drawPixmap(pos, w.grab())
    p.end()
    return img


class Recorder:
    """Frames of the window at 30 fps into ffmpeg (lossless), kept in step with the clock:
    a frame that came late is repeated, so the video plays at the speed things happened."""

    def __init__(self, win, path):
        from PySide6.QtCore import QTimer
        self.win, self.path = win, path
        self.proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                      "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
                                      "-preset", "ultrafast", "-qp", "0", path], stdin=subprocess.PIPE)
        self.t0 = time.monotonic()
        self.frames = 0
        self.captions = []
        self.timer = QTimer(interval=1000 // FPS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def now(self) -> float:
        return time.monotonic() - self.t0

    def tick(self):
        from PySide6.QtGui import QImage
        due = int(self.now() * FPS) + 1
        if due <= self.frames:
            return
        img = grab(self.win).convertToFormat(QImage.Format.Format_RGB888)
        if img.width() != W or img.height() != H:
            img = img.scaled(W, H)
        raw = bytes(img.constBits())[:W * H * 3] if img.bytesPerLine() == W * 3 else \
            b"".join(bytes(img.constScanLine(y))[:W * 3] for y in range(H))
        while self.frames < due:
            self.proc.stdin.write(raw)
            self.frames += 1

    def caption(self, text: str, seconds: float):
        t = self.now()
        self.captions.append((t + 0.15, t + seconds, text))

    def stop(self):
        self.timer.stop()
        self.proc.stdin.close()
        self.proc.wait()


def finish_video(raw: str, captions: list, out_dir: str):
    tmp = os.path.join(WORK, "captions")
    os.makedirs(tmp, exist_ok=True)
    filters = []
    for i, (a, b, text) in enumerate(captions):
        f = os.path.join(tmp, f"c{i}.txt")
        open(f, "w").write(text)
        filters.append(f"drawtext=fontfile={FONT_FILE}:textfile={f}:fontsize=28:fontcolor=white:box=1:"
                       f"boxcolor=0x1d1d20@0.82:boxborderw=16:x=(w-text_w)/2:y=h-96:enable='between(t,{a:.2f},{b:.2f})'")
    vf = ",".join(filters + ["format=yuv420p"])
    mp4 = os.path.join(out_dir, "xlt_tool_demo.mp4")
    ff("-i", raw, "-vf", vf, "-c:v", "libx264", "-crf", "20", "-preset", "slow", "-movflags", "+faststart", mp4)
    gif = os.path.join(out_dir, "xlt_tool_teaser.gif")
    ff("-ss", "0.2", "-t", "10", "-i", raw, "-vf",
       "fps=12,scale=760:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];"
       "[b][p]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0", gif)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp4],
                         capture_output=True, text=True).stdout.strip()
    print(f"{mp4}: {dur} s; {gif}: {os.path.getsize(gif) // 1024} KB")


# ------------------------------------------------------------------------- scenarios --
class Script:
    """Steps run one after another on the Qt event loop: (seconds to wait first, what to do)."""

    def __init__(self, app, steps):
        from PySide6.QtCore import QTimer
        self.app, self.steps, self.i = app, steps, 0
        QTimer.singleShot(0, self.next)

    def next(self):
        from PySide6.QtCore import QTimer
        if self.i >= len(self.steps):
            return
        wait, fn = self.steps[self.i]
        self.i += 1

        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - show it and stop: a broken step spoils the rest
                import traceback
                traceback.print_exc()
                print(f"step {self.i} failed: {e}", flush=True)
                os._exit(1)
            self.next()
        QTimer.singleShot(int(wait * 1000), run)


def find_row(gv, title: str) -> int:
    for i in range(gv.grid.count()):
        if gv.grid.item(i).text().split("\n")[0].startswith(title):
            return i
    return -1


def select_game(gv, title: str):
    i = find_row(gv, title)
    if i >= 0:
        gv.grid.setCurrentRow(i)
        gv.grid.scrollToItem(gv.grid.item(i))


def popup_game_menu(gv, title: str):
    """Right click on a game; the menu stays until close_popup()."""
    from PySide6.QtCore import QTimer
    i = find_row(gv, title)
    rect = gv.grid.visualItemRect(gv.grid.item(i))
    QTimer.singleShot(0, lambda: gv._grid_menu(rect.center()))


def close_popup():
    from PySide6.QtWidgets import QApplication
    w = QApplication.activePopupWidget()
    if w:
        w.close()


def where(gv, text: str):
    for k in range(gv.where_box.count()):
        if gv.where_box.itemText(k).lower().startswith(text.lower()):
            gv.where_box.setCurrentIndex(k)
            return


def to_psp_destination(win):
    for k in range(win.dest_box.count()):
        if win.dest_box.itemData(k) == "psp:":
            win.dest_box.setCurrentIndex(k)
            return True
    return False


def select_job(win, name: str):
    for k in range(win.queue.count()):
        job = win.jobs[k] if k < len(win.jobs) else None
        if job and os.path.basename(job.path).startswith(name):
            win.queue.setCurrentRow(k)
            return


def menu_button(win):
    from PySide6.QtWidgets import QToolButton
    for b in win.findChildren(QToolButton):
        if b.menu() and b.menu().actions() and any("About" in a.text() for a in b.menu().actions()):
            return b
    return None


def stills(out: str):
    prepare_env(intro=False)
    install_pretend_psp()
    app = start_app(dark=True)
    make_library()
    files = make_media()
    from xlttool.window import VIEW_CONVERT, VIEW_LIBRARY, MainWindow
    win = MainWindow([])
    win.resize(W, H)
    win.show()
    gv = win.games_view

    def shot(name):
        grab(win).save(os.path.join(out, f"shot_{name}.png"))
        print("shot", name, flush=True)

    dialogs = []

    def open_tags():
        from PySide6.QtCore import QTimer
        from xlttool.tagdialog import TagDialog
        jobs = [j for j in win.jobs if j.path.endswith(".flac")][:1]
        d = TagDialog(win, jobs)
        d.resize(640, 540)
        dialogs.append(d)
        QTimer.singleShot(0, d.exec)

    def dialog_shot(name):  # the dialog on its own: offscreen windows have no frame to set it apart
        dialogs[-1].grab().save(os.path.join(out, f"shot_{name}.png"))
        print("shot", name, flush=True)

    def close_dialog():
        from PySide6.QtWidgets import QApplication
        w = QApplication.activeModalWidget()
        if w:
            w.reject()

    def light():
        apply_theme(app, False)
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication
        QApplication.sendEvent(win, QEvent(QEvent.Type.PaletteChange))

    steps = [
        (3.5, lambda: select_game(gv, "Astral Drift")),
        (1.0, lambda: shot("1_library")),
        (0.2, lambda: where(gv, "only on the psp")),
        (0.8, lambda: select_game(gv, "Starfall")),
        (0.2, lambda: popup_game_menu(gv, "Starfall")),
        (0.8, lambda: shot("2_psp_only_menu")),
        (0.1, close_popup),
        (0.2, lambda: where(gv, "pc and psp")),
        (0.2, lambda: win.view_tabs.setCurrentIndex(VIEW_CONVERT)),
        (0.2, lambda: win.add_paths(files)),
        (2.5, lambda: to_psp_destination(win)),
        (0.3, lambda: select_job(win, "Mountain Trail")),
        (0.8, lambda: shot("3_convert_video")),
        (0.1, lambda: select_job(win, "Mira Ostend - Low Tide")),
        (0.8, lambda: shot("4_convert_music")),
        (0.1, open_tags),
        (1.0, lambda: dialog_shot("5_tag_editor")),
        (0.1, close_dialog),
        (0.2, lambda: select_job(win, "The Lighthouse")),
        (0.8, lambda: shot("6_convert_other")),
        (0.1, lambda: win.view_tabs.setCurrentIndex(VIEW_LIBRARY)),
        (0.2, light),
        (0.3, lambda: select_game(gv, "Kite Racer 2")),
        (1.0, lambda: shot("7_light_theme")),
        (0.2, lambda: os._exit(0)),
    ]
    Script(app, steps)
    app.exec()


def video(out: str):
    prepare_env(intro=True)
    install_pretend_psp()
    app = start_app(dark=True)
    make_library()
    files = make_media()
    from xlttool.window import VIEW_CONVERT, VIEW_LIBRARY, MainWindow
    win = MainWindow([])
    win.resize(W, H)
    win.show()
    gv = win.games_view
    raw = os.path.join(WORK, "raw.mkv")
    rec = Recorder(win, raw)

    def cap(text, secs):
        return lambda: rec.caption(text, secs)

    def walk(titles):
        return [(1.1, (lambda t=t: select_game(gv, t))) for t in titles]

    def menu():
        from PySide6.QtCore import QTimer
        b = menu_button(win)
        if b:
            QTimer.singleShot(0, b.showMenu)

    def done():
        rec.stop()
        finish_video(raw, rec.captions, out)
        os._exit(0)

    steps = [
        (0.2, cap("XLT Tool · the PC companion of XLT", 2.8)),
        (3.4, cap("Your PSP games on the PC and on the PSP, side by side", 4.2)),
        (0.4, lambda: select_game(gv, "Astral Drift")),
        *walk(["Citadel of Echoes", "Kite Racer 2", "Moonlit Arcade"]),
        (0.9, lambda: select_game(gv, "Astral Drift")),
        (0.2, cap("Details, play time and saves, read from the games", 3.4)),
        (3.6, lambda: where(gv, "only on the psp")),
        (0.3, lambda: select_game(gv, "Starfall")),
        (0.6, cap("Copy games from the PSP, or send them to it", 3.6)),
        (0.4, lambda: popup_game_menu(gv, "Starfall")),
        (2.2, close_popup),
        (0.3, lambda: where(gv, "only on this pc")),
        (0.5, lambda: select_game(gv, "Hollow Peaks")),
        (0.6, lambda: gv.send_btn.click()),
        (3.0, lambda: where(gv, "pc and psp")),
        (1.0, lambda: win.view_tabs.setCurrentIndex(VIEW_CONVERT)),
        (0.3, cap("Convert videos, music, pictures, books and games for the PSP", 3.6)),
        (0.2, lambda: win.add_paths(files)),
        (2.0, lambda: select_job(win, "Mountain Trail")),
        (1.6, lambda: select_job(win, "Mira Ostend - Low Tide")),
        (1.6, lambda: select_job(win, "Harbour")),
        (1.4, lambda: select_job(win, "The Lighthouse")),
        (1.2, lambda: to_psp_destination(win)),
        (0.2, cap("Straight to the PSP over USB, while XLT keeps running", 4.0)),
        (1.2, lambda: win.toggle_run()),
        (14.0, menu),
        (0.1, cap("Box art, updates and more in the menu", 2.4)),
        (2.4, close_popup),
        (0.6, done),
    ]
    Script(app, steps)
    app.exec()


def main():
    out = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO, "docs", "media"))
    os.makedirs(out, exist_ok=True)
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("stills", "video"):
        (stills if what == "stills" else video)(out)
        return
    for part in ("stills", "video"):  # each in a process of its own: a fresh app and scratch home
        subprocess.run([sys.executable, "-I", os.path.abspath(__file__), part, out], check=True)
    print("make_demo_media: done in", out)


if __name__ == "__main__":
    main()
