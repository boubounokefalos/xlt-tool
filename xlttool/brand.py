"""XLT's look on the PC: the logo, the app icon and the start-up animation.

The logo is XLT's own "Home row" (~/xlt/tools/make_logo.py): four cards in a row, as on XLT's
home screen, the second one focused (bigger, lifted, a ring and a bar under it), in the
launcher's four colours. The geometry here is the same as there, so the two always match.

The start-up animation is XLT's boot screen (~/xlt/src/bootdraw.c): the cards pop in one after
another, left to right; if the Library is still being looked through, a bar in the cards'
colours fills under them; then everything fades into the window. A click or a key skips it.

    python3 -m xlttool.brand icon > xlttool/xlt-tool.svg      (the app icon, from the logo)
"""
import sys

TEXT = "#e6edf3"
COLOURS = ("#7fd3ad", "#ec8e8e", "#8fb3ee", "#e0a3e0")   # mint, coral, blue, pink
FOCUS = 1
NIGHT_BG = "#0d1117"


def card(i: int, cx: float, cy: float, small: float, ring: str = TEXT) -> str:
    """Card i centred at (cx, cy); `small` is a plain card's side. The focused one is bigger, lifted, ringed."""
    c = COLOURS[i]
    if i != FOCUS:
        s, r = small, small * 0.27
        return f"<rect x='{cx - s / 2:.2f}' y='{cy - s / 2:.2f}' width='{s:.2f}' height='{s:.2f}' rx='{r:.2f}' fill='{c}'/>"
    s = small * 1.3
    cy -= small * 0.18
    rs, rw = s + small * 0.34, small * 0.09
    bar_w, bar_y = s * 0.85, cy + rs / 2 + small * 0.28
    return (f"<rect x='{cx - s / 2:.2f}' y='{cy - s / 2:.2f}' width='{s:.2f}' height='{s:.2f}' rx='{s * 0.26:.2f}' fill='{c}'/>"
            f"<rect x='{cx - rs / 2:.2f}' y='{cy - rs / 2:.2f}' width='{rs:.2f}' height='{rs:.2f}' "
            f"rx='{rs * 0.28:.2f}' fill='none' stroke='{ring}' stroke-width='{rw:.2f}'/>"
            f"<rect x='{cx - bar_w / 2:.2f}' y='{bar_y:.2f}' width='{bar_w:.2f}' height='{small * 0.12:.2f}' "
            f"rx='{small * 0.06:.2f}' fill='{ring}' opacity='0.85'/>")


def centres(small: float, gap: float) -> list[float]:
    """The cards' centres from the row's middle, the same gap between every two (edge to edge)."""
    xs, x = [], 0.0
    for i in range(4):
        w = small * 1.64 if i == FOCUS else small
        xs.append(x + w / 2)
        x += w + gap
    mid = (x - gap) / 2
    return [v - mid for v in xs]


def row(cx: float, cy: float, small: float, gap: float, ring: str = TEXT) -> str:
    return "".join(card(i, cx + dx, cy, small, ring) for i, dx in enumerate(centres(small, gap)))


def svg(w: float, h: float, body: str) -> str:
    return f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}'>{body}</svg>"


def logo(ring: str = TEXT) -> str:
    """The mark on its own, 144x80, transparent."""
    return svg(144, 80, row(72, 43, 22, 9, ring))


def icon() -> str:
    """The app icon: the mark on a tile like XLT's XMB icon (the Night theme), square."""
    n, m, r = 128, 6, 26
    body = (f"<defs><linearGradient id='face' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0' stop-color='#1f2630'/><stop offset='1' stop-color='#11161d'/></linearGradient></defs>"
            f"<rect x='{m}' y='{m}' width='{n - 2 * m}' height='{n - 2 * m}' rx='{r}' fill='url(#face)' "
            f"stroke='#30363d' stroke-width='2'/>"
            f"<rect x='{m + 2}' y='{m + 2}' width='{n - 2 * m - 4}' height='{n - 2 * m - 4}' rx='{r - 2}' fill='none' "
            f"stroke='#ffffff' stroke-opacity='0.07' stroke-width='1.2'/>")
    return svg(n, n, body + row(64, 66, 17, 6))


# ------------------------------------------------------------ the animation --
try:
    from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QPainter, QPalette
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QWidget
except ImportError:  # `python3 -m xlttool.brand icon` without Qt
    QWidget = object

FPS = 60
CELL = 64          # each card drawn in a cell of its own (as XLT's texture), centred
CELL_SMALL = 28    # a plain card's side in its cell
SHOWN = 104        # a cell on screen, px (XLT: 80 on a 480x272 screen)
HOLD = 24          # frames of the finished logo before it fades (XLT's HOLD_FRAMES)
LEAVE = 34         # frames of fading (XLT's LEAVE_FRAMES)
WAIT_MAX = 4 * FPS  # never wait longer than this for the Library after the logo is up


def _ease_out_back(p: float) -> float:
    c1 = 1.05
    c3 = c1 + 1
    q = p - 1
    return 1 + c3 * q * q * q + c1 * q * q


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


class BootOverlay(QWidget):
    """Covers the window at start and plays XLT's boot screen over it, then fades away.

    busy(): True while something worth waiting for goes on (the Library's first look);
    progress(i, n) and done() feed the bar."""

    def __init__(self, window: QWidget, busy=lambda: False):
        super().__init__(window)
        self._window = window
        self._busy = busy
        self.frame = 0
        self.leave = 0.0        # 0..1
        self.leaving = False
        self.bar_show = 0.0     # 0..1
        self.prog = -1.0        # shown progress 0..1, -1: not known (a moving segment)
        self.want = -1.0
        self.line = ""
        self.hold = 0
        self.waited = 0
        ring = window.palette().color(QPalette.ColorRole.WindowText).name()
        self.cards = []
        for i in range(4):
            r = QSvgRenderer(QByteArray(svg(CELL, CELL, card(i, CELL / 2, CELL / 2, CELL_SMALL, ring)).encode()))
            self.cards.append(r)
        scale = SHOWN / CELL
        self.xs = [v * scale for v in centres(CELL_SMALL, 12)]
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        window.installEventFilter(self)
        self.setGeometry(window.rect())
        self.raise_()
        self.timer = QTimer(self, interval=1000 // FPS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.show()
        self.setFocus()

    # the window's size and keys
    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        if obj is self._window and e.type() == QEvent.Type.Resize:
            self.setGeometry(self._window.rect())
            self.raise_()
        return False

    def progress(self, i: int, n: int):
        if n > 0:
            self.want = min(1.0, i / n)
            self.line = f"Looking for games · {i} / {n}"

    def done(self):
        if self.want >= 0 or self.bar_show > 0:
            self.want = 1.0

    def skip(self):
        self._leave()

    def mousePressEvent(self, e):
        self.skip()

    def keyPressEvent(self, e):
        self.skip()

    def wheelEvent(self, e):
        e.accept()

    def _leave(self):
        if not self.leaving:
            self.leaving = True
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.clearFocus()

    def _tick(self):
        self.frame += 1
        logo_up = self.frame >= 8 + 3 * 16 + 40
        busy = self._busy()
        if self.leaving:
            self.leave = min(1.0, self.leave + 1 / LEAVE)
            if self.leave >= 1.0:
                self.timer.stop()
                self._window.removeEventFilter(self)
                self.hide()
                self.deleteLater()
                return
        elif logo_up:
            if busy and self.waited < WAIT_MAX:
                self.waited += 1
                self.bar_show = min(1.0, self.bar_show + 1 / 16)
            elif self.bar_show > 0 and self.prog < 0.999:
                self.want = 1.0       # filled, then a short pause (as XLT)
                self.waited += 1
                if self.waited > WAIT_MAX + 30:
                    self._leave()
            else:
                self.hold += 1
                if self.hold >= HOLD:
                    self._leave()
        if self.want >= 0:
            self.prog = self.want if self.prog < 0 else self.prog + (self.want - self.prog) * 0.18
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        keep = 1.0 - self.leave
        bg = QColor(self.palette().color(QPalette.ColorRole.Window))
        bg.setAlphaF(keep)
        p.fillRect(self.rect(), bg)
        cx0, cy0 = self.width() / 2, self.height() / 2 - 24
        grow = 1 + 0.06 * self.leave
        for i, r in enumerate(self.cards):
            t = _clamp((self.frame - 8 - i * 16) / 40.0)
            if t <= 0:
                continue
            ease = _ease_out_back(t)
            s = SHOWN * (0.55 + 0.45 * ease) * grow
            cx = cx0 + self.xs[i] * grow
            cy = cy0 + (1 - ease) * 13
            p.setOpacity(_clamp(t * 1.8) * keep)
            r.render(p, QRectF(cx - s / 2, cy - s / 2, s, s))
        show = self.bar_show * keep
        if show > 0:
            p.setOpacity(show)
            w, h = 190, 4
            x, y = cx0 - w / 2, cy0 + 78
            pal = self.palette()
            track = QColor(pal.color(QPalette.ColorRole.WindowText))
            track.setAlphaF(0.12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(track)
            p.drawRoundedRect(QRectF(x, y, w, h), 2, 2)
            if self.prog < 0:   # how much is not known yet: a segment that keeps moving
                seg = 46
                pos = (self.frame * 2.5) % (w + seg) - seg
                a, b = max(x, x + pos), min(x + w, x + pos + seg)
                if b > a:
                    p.setBrush(QColor(COLOURS[(self.frame // 90) % 4]))
                    p.drawRoundedRect(QRectF(a, y, b - a, h), 2, 2)
            else:               # filled in the cards' colours, a quarter each
                fill = w * min(1.0, self.prog)
                p.save()
                p.setClipRect(QRectF(x, y, fill, h))
                for k in range(4):
                    p.setBrush(QColor(COLOURS[k]))
                    p.drawRect(QRectF(x + k * w / 4, y, w / 4 + 1, h))
                p.restore()
            if self.line:
                muted = QColor(pal.color(QPalette.ColorRole.WindowText))
                muted.setAlphaF(0.6)
                p.setPen(muted)
                p.drawText(QRectF(0, y + 12, self.width(), 24),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self.line)
        p.end()


if __name__ == "__main__":
    if sys.argv[1:] == ["icon"]:
        print(icon())
    elif sys.argv[1:] == ["logo"]:
        print(logo())
    else:
        sys.exit(__doc__)
