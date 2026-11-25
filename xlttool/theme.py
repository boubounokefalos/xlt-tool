"""Follow the desktop's light/dark mode, accent color and font.

Sources, in order:
  1. xdg-desktop-portal Settings (org.freedesktop.appearance color-scheme and
     accent-color) - GNOME 47+, KDE Plasma 6, COSMIC, recent XFCE/others.
  2. KDE: ~/.config/kdeglobals gives the full color scheme (Window, View,
     Button, Selection...), so the app matches custom Plasma schemes.
  3. GNOME: gsettings color-scheme / accent-color / font-name.
  4. GTK settings.ini (prefer-dark / theme name containing "dark").
On Windows: the registry (apps' light/dark mode, the accent colour) instead.

The palette is applied to Qt's Fusion style, so it looks the same with the
Qt bundled in the AppImage on any distro (system style plugins such as Breeze
can't be loaded into a bundled Qt).

Portal SettingChanged signals are watched on a background thread, so
switching dark mode or the accent color restyles the app immediately.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Settings"

GNOME_ACCENTS = {
    "blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a",
    "yellow": "#c88800", "orange": "#ed5b00", "red": "#e62d42",
    "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396",
}
DEFAULT_ACCENT = "#3584e4"


def desktop() -> str:
    return (os.environ.get("XDG_CURRENT_DESKTOP")
            or os.environ.get("XDG_SESSION_DESKTOP") or "").upper()


def is_kde() -> bool:
    return "KDE" in desktop() or os.environ.get("KDE_FULL_SESSION") == "true"


def config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


# ---------------------------------------------------------------- portal ----

def _unwrap(v):
    """jeepney returns variants as (signature, value); Read() nests twice."""
    while isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str):
        v = v[1]
    return v


def _portal_read_jeepney(keys):
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=PORTAL_IFACE)
    out = {}
    with open_dbus_connection(bus="SESSION") as conn:
        for ns, key in keys:
            for method in ("ReadOne", "Read"):
                try:
                    msg = new_method_call(addr, method, "ss", (ns, key))
                    reply = conn.send_and_get_reply(msg, timeout=1.5)
                except Exception:
                    break
                if reply.header.message_type.name == "error":
                    continue        # old portals have no ReadOne
                out[(ns, key)] = _unwrap(reply.body[0])
                break
    return out


def _portal_read_gdbus(keys):
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return {}
    out = {}
    for ns, key in keys:
        try:
            r = subprocess.run(
                [gdbus, "call", "--session", "--dest", PORTAL_BUS,
                 "--object-path", PORTAL_PATH, "--method",
                 PORTAL_IFACE + ".Read", ns, key],
                capture_output=True, text=True, timeout=1.5)
        except (OSError, subprocess.TimeoutExpired):
            return out
        if r.returncode != 0:
            continue
        nums = re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", r.stdout.split(">")[0])
        if key == "color-scheme" and nums:
            out[(ns, key)] = int(float(nums[-1]))
        elif key == "accent-color" and len(nums) >= 3:
            out[(ns, key)] = tuple(float(n) for n in nums[-3:])
    return out


def portal_appearance() -> tuple[int | None, QColor | None]:
    keys = [("org.freedesktop.appearance", "color-scheme"),
            ("org.freedesktop.appearance", "accent-color")]
    try:
        vals = _portal_read_jeepney(keys)
    except Exception:
        vals = _portal_read_gdbus(keys)
    scheme = vals.get(keys[0])
    scheme = int(scheme) if isinstance(scheme, (int, float)) else None
    accent = None
    rgb = vals.get(keys[1])
    if isinstance(rgb, (tuple, list)) and len(rgb) == 3:
        if all(0.0 <= float(c) <= 1.0 for c in rgb):   # out of range = unset
            accent = QColor.fromRgbF(*(float(c) for c in rgb))
    return scheme, accent


class PortalWatcher(QObject):
    """Emits changed() when the desktop's appearance settings change."""
    changed = Signal()

    def start(self):
        if sys.platform == "win32":
            # No portal: Qt tells when apps' light/dark mode is switched.
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.styleHints().colorSchemeChanged.connect(self.changed.emit)
            return
        threading.Thread(target=self._run, daemon=True,
                         name="portal-watch").start()

    def _run(self):
        try:
            from jeepney.bus_messages import MatchRule, message_bus
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            return
        try:
            conn = open_dbus_connection(bus="SESSION")
            rule = MatchRule(type="signal", interface=PORTAL_IFACE,
                             member="SettingChanged", path=PORTAL_PATH)
            conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=2)
            with conn.filter(rule) as queue:
                while True:
                    msg = conn.recv_until_filtered(queue)
                    ns = msg.body[0] if msg.body else ""
                    if (ns.startswith("org.freedesktop.appearance")
                            or ns.startswith("org.kde.kdeglobals")
                            or ns == "org.gnome.desktop.interface"):
                        self.changed.emit()
        except Exception:
            return


# ------------------------------------------------------------------ KDE ----

def _kde_color(cfg, section, key):
    try:
        r, g, b = (int(x) for x in cfg.get(section, key).split(",")[:3])
        return QColor(r, g, b)
    except Exception:
        return None


def kde_palette_colors():
    path = os.path.join(config_home(), "kdeglobals")
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.optionxform = str
    try:
        cfg.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    if not cfg.has_section("Colors:Window"):
        return None
    c = lambda s, k: _kde_color(cfg, s, k)
    colors = {
        "window": c("Colors:Window", "BackgroundNormal"),
        "window_text": c("Colors:Window", "ForegroundNormal"),
        "inactive_text": c("Colors:Window", "ForegroundInactive"),
        "base": c("Colors:View", "BackgroundNormal"),
        "alt_base": c("Colors:View", "BackgroundAlternate"),
        "text": c("Colors:View", "ForegroundNormal"),
        "button": c("Colors:Button", "BackgroundNormal"),
        "button_text": c("Colors:Button", "ForegroundNormal"),
        "highlight": c("Colors:Selection", "BackgroundNormal"),
        "highlight_text": c("Colors:Selection", "ForegroundNormal"),
        "tooltip": c("Colors:Tooltip", "BackgroundNormal"),
        "tooltip_text": c("Colors:Tooltip", "ForegroundNormal"),
        "link": c("Colors:View", "ForegroundLink"),
        "negative": c("Colors:View", "ForegroundNegative"),
        "positive": c("Colors:View", "ForegroundPositive"),
    }
    accent = None
    if cfg.has_option("General", "AccentColor"):
        accent = _kde_color(cfg, "General", "AccentColor")
    font = None
    if cfg.has_option("General", "font"):
        font = _qt_font_from_kde(cfg.get("General", "font"))
    return {k: v for k, v in colors.items() if v is not None}, accent, font


def _qt_font_from_kde(spec: str):
    parts = spec.split(",")
    if len(parts) < 2:
        return None
    f = QFont(parts[0])
    try:
        f.setPointSizeF(float(parts[1]))
    except ValueError:
        pass
    return f


# ---------------------------------------------------------------- GNOME ----

def _gsettings(schema, key):
    exe = shutil.which("gsettings")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "get", schema, key], capture_output=True,
                           text=True, timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip().strip("'") if r.returncode == 0 else None


def _qt_font_from_pango(spec: str):
    m = re.match(r"^(.*?)[ ,]+(\d+(?:\.\d+)?)$", spec.strip())
    if not m:
        return None
    family = re.sub(r"\b(Bold|Italic|Oblique|Light|Medium|Regular)\b", "",
                    m.group(1)).strip()
    f = QFont(family)
    f.setPointSizeF(float(m.group(2)))
    return f


def gtk_prefers_dark() -> bool | None:
    for ver in ("gtk-4.0", "gtk-3.0"):
        path = os.path.join(config_home(), ver, "settings.ini")
        cfg = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cfg.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if cfg.has_section("Settings"):
            s = cfg["Settings"]
            if s.get("gtk-application-prefer-dark-theme", "").lower() in ("1", "true"):
                return True
            name = s.get("gtk-theme-name", "")
            if name:
                return "dark" in name.lower()
    return None


# ------------------------------------------------------------- palettes ----

@dataclass
class Theme:
    dark: bool
    accent: QColor
    palette: QPalette
    font: QFont | None
    source: str


def _adwaita_colors(dark: bool):
    if dark:
        return {
            "window": QColor("#222226"), "window_text": QColor("#ffffff"),
            "base": QColor("#1d1d20"), "alt_base": QColor("#28282c"),
            "text": QColor("#ffffff"), "button": QColor("#3a3a3e"),
            "button_text": QColor("#ffffff"), "tooltip": QColor("#3a3a3e"),
            "tooltip_text": QColor("#ffffff"), "inactive_text": QColor("#9a9a9e"),
            "negative": QColor("#ff7b63"), "positive": QColor("#8ff0a4"),
        }
    return {
        "window": QColor("#fafafb"), "window_text": QColor("#2e2e33"),
        "base": QColor("#ffffff"), "alt_base": QColor("#f4f4f6"),
        "text": QColor("#2e2e33"), "button": QColor("#e8e8ea"),
        "button_text": QColor("#2e2e33"), "tooltip": QColor("#2e2e33"),
        "tooltip_text": QColor("#ffffff"), "inactive_text": QColor("#77777c"),
        "negative": QColor("#c01c28"), "positive": QColor("#26a269"),
    }


def _readable_on(bg: QColor) -> QColor:
    lum = 0.2126 * bg.redF() + 0.7152 * bg.greenF() + 0.0722 * bg.blueF()
    return QColor("#000000") if lum > 0.6 else QColor("#ffffff")


def build_palette(c: dict, accent: QColor, dark: bool) -> QPalette:
    p = QPalette()
    hl = c.get("highlight", accent)
    hl_text = c.get("highlight_text", _readable_on(hl))
    roles = {
        QPalette.Window: c["window"], QPalette.WindowText: c["window_text"],
        QPalette.Base: c["base"], QPalette.AlternateBase: c["alt_base"],
        QPalette.Text: c["text"], QPalette.Button: c["button"],
        QPalette.ButtonText: c["button_text"],
        QPalette.ToolTipBase: c["tooltip"], QPalette.ToolTipText: c["tooltip_text"],
        QPalette.PlaceholderText: c["inactive_text"],
        QPalette.Highlight: hl, QPalette.HighlightedText: hl_text,
        QPalette.Link: c.get("link", accent.lighter(130) if dark else accent),
        QPalette.BrightText: QColor("#ffffff"),
        QPalette.Accent: accent,
    }
    for role, col in roles.items():
        p.setColor(QPalette.All, role, col)
    win = c["window"]
    p.setColor(QPalette.All, QPalette.Light, win.lighter(150))
    p.setColor(QPalette.All, QPalette.Midlight, win.lighter(120 if dark else 110))
    p.setColor(QPalette.All, QPalette.Mid, win.darker(130 if dark else 115))
    p.setColor(QPalette.All, QPalette.Dark, win.darker(160 if dark else 135))
    p.setColor(QPalette.All, QPalette.Shadow, QColor(0, 0, 0, 160))
    dis = c["inactive_text"]
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, dis)
    p.setColor(QPalette.Disabled, QPalette.Highlight, win.darker(120))
    return p


# -------------------------------------------------------------- Windows ----

def windows_appearance() -> tuple[bool | None, QColor | None]:
    """Settings → Personalisation → Colours: apps' light/dark mode and the accent colour."""
    import winreg
    dark = accent = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            dark = winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as k:
            v = winreg.QueryValueEx(k, "AccentColor")[0]      # 0xAABBGGRR
            accent = QColor(v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)
    except OSError:
        pass
    return dark, accent


def _detect_windows() -> Theme:
    dark, accent = windows_appearance()
    source = "registry" if (dark is not None or accent is not None) else "defaults"
    dark = bool(dark)
    accent = accent or QColor(DEFAULT_ACCENT)
    if dark and accent.lightnessF() < 0.35:
        accent = accent.lighter(150)    # Windows' dark accents vanish on a dark window
    colors = _adwaita_colors(dark)
    # Qt already uses the system's UI font (Segoe UI) on Windows.
    return Theme(dark, accent, build_palette(colors, accent, dark), None, source)


def detect() -> Theme:
    if sys.platform == "win32":
        return _detect_windows()
    scheme, accent = portal_appearance()
    source = "portal" if (scheme is not None or accent is not None) else ""
    font = None
    colors = None

    if is_kde():
        kde = kde_palette_colors()
        if kde:
            colors, kde_accent, font = kde
            accent = accent or kde_accent or colors.get("highlight")
            source = (source + "+kdeglobals").strip("+")

    if scheme in (1, 2):
        dark = scheme == 1
    elif colors:
        dark = colors["window"].lightnessF() < 0.5
    else:
        gs = _gsettings("org.gnome.desktop.interface", "color-scheme")
        if gs and gs != "default":
            dark = gs == "prefer-dark"
        else:
            dark = bool(gtk_prefers_dark())

    if colors and (colors["window"].lightnessF() < 0.5) != dark:
        colors = None   # kdeglobals out of sync with the portal: trust portal

    if accent is None:
        name = _gsettings("org.gnome.desktop.interface", "accent-color")
        if name in GNOME_ACCENTS:
            accent = QColor(GNOME_ACCENTS[name])
            source = (source + "+gsettings").strip("+")
    if accent is None:
        accent = QColor(DEFAULT_ACCENT)

    if font is None and not is_kde():
        spec = _gsettings("org.gnome.desktop.interface", "font-name")
        if spec:
            font = _qt_font_from_pango(spec)

    if colors is None:
        colors = _adwaita_colors(dark)
    return Theme(dark, accent, build_palette(colors, accent, dark), font,
                 source or "defaults")


def _indicator_files(t: Theme, border: QColor) -> tuple[str, str, str]:
    """Checkbox images in the current accent color (Fusion's own boxes vanish
    on some light palettes). Written to a per-user temp dir."""
    import tempfile
    # Windows' temp folder is per user already, and it has no getuid().
    d = os.path.join(tempfile.gettempdir(),
                     f"xlt-tool-{os.getuid()}" if hasattr(os, "getuid") else "xlt-tool")
    os.makedirs(d, exist_ok=True)
    acc, fg = t.accent.name(), _readable_on(t.accent).name()
    base = t.palette.color(QPalette.Base).name()
    off = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
           f'<rect x="1" y="1" width="16" height="16" rx="4" fill="{base}" '
           f'stroke="{border.name()}" stroke-width="1.5"/></svg>')
    on = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
          f'<rect x="0.5" y="0.5" width="17" height="17" rx="4.5" fill="{acc}"/>'
          f'<path d="M4.5 9.2 L7.6 12.2 L13.5 5.8" fill="none" stroke="{fg}" '
          f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    dis = off.replace(base, border.name())
    paths = []
    for name, svg in (("off", off), ("on", on), ("dis", dis)):
        tag = f"{name}-{acc[1:]}-{base[1:]}-{border.name()[1:]}.svg"
        path = os.path.join(d, tag)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
        # forward slashes: in the stylesheet's url("...") a Windows backslash is an escape, and the whole
        # stylesheet failed to parse ("Could not parse application stylesheet"), so none of it applied
        paths.append(path.replace(os.sep, "/"))
    return tuple(paths)


class MenuPolish(QObject):
    """Menus with real rounded corners (as GNOME's): frameless and see-through behind the
    rounded background the stylesheet draws. Set on every QMenu before it is first shown."""

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QMenu
        if event.type() == QEvent.Type.Polish and isinstance(obj, QMenu):
            obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            obj.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            obj.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        return False


def install_menu_polish(app) -> None:
    f = MenuPolish(app)
    app.installEventFilter(f)
    app._menu_polish = f  # kept alive with the app


def stylesheet(t: Theme) -> str:
    """Light polish on top of Fusion; everything derives from the palette."""
    p = t.palette
    acc = t.accent
    acc_fg = _readable_on(acc).name()
    win = p.color(QPalette.Window)
    border = win.lighter(150) if t.dark else win.darker(118)
    card = p.color(QPalette.Base)
    hover = acc.lighter(112).name()
    press = acc.darker(112).name()
    muted = p.color(QPalette.PlaceholderText).name()
    neg = QColor("#ff7b63" if t.dark else "#c01c28").name()
    try:
        box_off, box_on, box_dis = _indicator_files(
            t, win.lighter(190) if t.dark else win.darker(140))
    except OSError:
        box_off = box_on = box_dis = ""
    pos = QColor("#8ff0a4" if t.dark else "#1b8553").name()
    return f"""
    QPushButton, QToolButton, QComboBox {{
        border: 1px solid {border.name()}; border-radius: 6px;
        padding: 5px 12px; min-height: 20px;
        background: {p.color(QPalette.Button).name()};
    }}
    QPushButton:hover, QToolButton:hover, QComboBox:hover {{
        background: {p.color(QPalette.Button).lighter(108 if t.dark else 96).name()};
    }}
    QPushButton:disabled {{ color: {muted}; }}
    QPushButton#primary {{
        background: {acc.name()}; color: {acc_fg}; border-color: {acc.name()};
        font-weight: 600; padding: 6px 20px;
    }}
    QPushButton#primary:hover {{ background: {hover}; }}
    QPushButton#primary:pressed {{ background: {press}; }}
    QPushButton#primary:disabled {{
        background: {border.name()}; border-color: {border.name()}; color: {muted};
    }}
    QPushButton#destructive {{ color: {neg}; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 18px; height: 18px; image: url("{box_off}"); }}
    QCheckBox::indicator:checked {{ image: url("{box_on}"); }}
    QCheckBox::indicator:disabled {{ image: url("{box_dis}"); }}
    QCheckBox::indicator:checked:disabled {{ image: url("{box_on}"); }}
    QTabBar::tab {{
        padding: 6px 4px; border: none; border-bottom: 2px solid {border.name()};
        background: transparent; color: {muted};
    }}
    QTabBar::tab:selected {{ color: palette(window-text); border-bottom: 2px solid {acc.name()}; font-weight: 600; }}
    QTabBar::tab:hover {{ color: palette(window-text); }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    QMenu {{
        background: {(win.lighter(118) if t.dark else win.lighter(103)).name()};
        border: 1px solid {border.name()}; border-radius: 12px; padding: 6px;
    }}
    QMenu::item {{
        padding: 7px 34px 7px 12px; border-radius: 7px; margin: 1px 0;
        background: transparent; color: palette(window-text);
    }}
    QMenu::item:selected {{
        background: {QColor(255, 255, 255, 26).name(QColor.HexArgb) if t.dark else QColor(0, 0, 0, 20).name(QColor.HexArgb)};
    }}
    QMenu::item:disabled {{ color: {muted}; }}
    QMenu::separator {{
        height: 1px; margin: 5px 6px;
        background: {QColor(255, 255, 255, 40).name(QColor.HexArgb) if t.dark else QColor(0, 0, 0, 36).name(QColor.HexArgb)};
    }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
    QToolButton:checked {{
        background: {QColor(acc.red(), acc.green(), acc.blue(), 70).name(QColor.HexArgb)};
        border: 1px solid {acc.name()};
    }}
    QToolButton#flat {{ border: none; background: transparent; padding: 4px; }}
    QToolButton#flat:hover {{ background: {border.name()}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QListWidget#queue, QListWidget#games {{
        background: {card.name()}; border: 1px solid {border.name()};
        border-radius: 10px; padding: 4px; outline: none;
    }}
    QListWidget#queue::item, QListWidget#games::item {{ border-radius: 8px; margin: 2px; }}
    QListWidget#queue::item:selected, QListWidget#games::item:selected {{
        background: {QColor(acc.red(), acc.green(), acc.blue(), 48).name(QColor.HexArgb)};
        color: palette(text);  /* on the pale tint, not the white meant for a full accent */
    }}
    QTreeWidget#files, QListWidget#places {{
        background: {card.name()}; border: 1px solid {border.name()};
        border-radius: 10px; padding: 4px; outline: none;
    }}
    QTreeWidget#files::item, QListWidget#places::item {{ border-radius: 6px; padding: 3px 2px; }}
    QTreeWidget#files::item:selected, QListWidget#places::item:selected {{
        background: {QColor(acc.red(), acc.green(), acc.blue(), 48).name(QColor.HexArgb)};
        color: palette(text);
    }}
    QTreeWidget#files QHeaderView::section {{
        background: transparent; border: none; border-bottom: 1px solid {border.name()};
        padding: 4px 6px; color: {muted}; font-weight: 600;
    }}
    QFrame#card {{
        background: {card.name()}; border: 1px solid {border.name()};
        border-radius: 10px;
    }}
    QLabel#heading {{ font-weight: 700; }}
    QLabel#muted {{ color: {muted}; }}
    QLabel#error {{ color: {neg}; }}
    QLabel#ok {{ color: {pos}; }}
    QLabel#accent {{ color: {acc.lighter(125).name() if t.dark else acc.darker(110).name()}; }}
    QProgressBar {{
        border: none; border-radius: 3px; background: {border.name()};
        max-height: 6px; min-height: 6px;
    }}
    QProgressBar::chunk {{ border-radius: 3px; background: {acc.name()}; }}
    """
