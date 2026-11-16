from __future__ import annotations

import os
import sys


def _prepare_windows():
    """Windows, before QApplication exists: XLT Tool's own taskbar entry and icon (not
    python.exe's), and no console window flashing up for each ffmpeg or ffprobe run."""
    import ctypes
    import subprocess
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("XLT.XLTTool")
    except (AttributeError, OSError):
        pass
    popen_init = subprocess.Popen.__init__

    def no_console(self, *args, creationflags=0, **kwargs):
        popen_init(self, *args, creationflags=creationflags | subprocess.CREATE_NO_WINDOW, **kwargs)
    subprocess.Popen.__init__ = no_console


def _prepare_env():
    """Must run before QApplication exists."""
    if sys.platform == "win32":
        _prepare_windows()
        return
    in_appimage = bool(os.environ.get("APPIMAGE") or os.environ.get("APPDIR"))
    # Settings meant for the system Qt can break the bundled one.
    for var in ("QT_STYLE_OVERRIDE", "QT_PLUGIN_PATH", "QT_QPA_PLATFORMTHEME_OVERRIDE"):
        if in_appimage:
            os.environ.pop(var, None)
    # Native file dialogs (GNOME/KDE/etc.) through xdg-desktop-portal, and
    # dark-mode hints from the portal. System theme plugins such as kde or
    # qt6ct are not loadable into the bundled Qt anyway.
    os.environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"
    desk = (os.environ.get("XDG_CURRENT_DESKTOP") or "").upper()
    # GNOME's mutter has no server-side decorations on Wayland: use Qt's
    # libadwaita-style title bar instead of the old "bradient" one.
    if not any(k in desk for k in ("KDE", "LXQT", "HYPRLAND", "SWAY")):
        os.environ.setdefault("QT_WAYLAND_DECORATION", "adwaita")


def _icon_theme():
    from PySide6.QtGui import QIcon
    from . import theme
    name = None
    if theme.is_kde():
        import configparser
        cfg = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cfg.read(os.path.join(theme.config_home(), "kdeglobals"), encoding="utf-8")
            name = cfg.get("Icons", "Theme", fallback="breeze")
        except Exception:
            name = "breeze"
    else:
        name = theme._gsettings("org.gnome.desktop.interface", "icon-theme")
    paths = list(QIcon.themeSearchPaths())
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for d in [os.path.expanduser("~/.local/share")] + data_dirs:
        p = os.path.join(d, "icons")
        if os.path.isdir(p) and p not in paths:
            paths.append(p)
    if os.path.isdir(os.path.expanduser("~/.icons")):
        paths.append(os.path.expanduser("~/.icons"))
    QIcon.setThemeSearchPaths(paths)
    QIcon.setFallbackThemeName("Adwaita")
    if name:
        QIcon.setThemeName(name)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if "--version" in argv:
        from . import __version__
        print(__version__)
        return 0
    _prepare_env()
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWidgets import QApplication

    from . import theme
    from .window import APP_NAME, MainWindow

    QGuiApplication.setDesktopFileName("xlt-tool")   # Wayland app_id
    app = QApplication(argv)
    app.setApplicationName("xlt-tool")
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "xlt-tool.svg")))
    if sys.platform != "win32":     # Windows has no icon themes: Qt's own fallbacks are used
        _icon_theme()
    theme.install_menu_polish(app)

    def apply_theme():
        t = theme.detect()
        app.setPalette(t.palette)
        if t.font:
            app.setFont(t.font)
        app.setStyleSheet(theme.stylesheet(t))
        if (os.environ.get("XLTTOOL_DEBUG") or os.environ.get("PSPCONV_DEBUG")):
            print(f"theme: dark={t.dark} accent={t.accent.name()} source={t.source}",
                  file=sys.stderr)

    apply_theme()
    debounce = QTimer(singleShot=True, interval=250)
    debounce.timeout.connect(apply_theme)
    watcher = theme.PortalWatcher()
    watcher.changed.connect(debounce.start)
    watcher.start()

    files = [a for a in argv[1:] if not a.startswith("-")]
    win = MainWindow(files)
    win.show()
    rc = app.exec()
    # Tear the window down while QApplication is alive; leaving it to
    # interpreter shutdown can crash in PySide's cleanup.
    import shiboken6
    if shiboken6.isValid(win):
        shiboken6.delete(win)
    return rc
