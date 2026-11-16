# How XLT Tool is built

XLT Tool is a Python 3 program with a Qt 6 window (PySide6). Conversions are done by ffmpeg and ffprobe (and
atracdenc for PS1 CD music), run as separate programs; everything else (game readers, CSO, PS1 EBOOTs, ZIP, USB
sync) is Python. On Linux it ships as one AppImage with its own Python, Qt, ffmpeg and atracdenc, so it needs
nothing from the system beyond glibc 2.34 and a desktop. On Windows it is a PyInstaller folder (`XLT Tool.exe`,
with ffmpeg and atracdenc in `bin\`), as an installer or a portable zip.

## Modules

| Module | What it does | Qt |
|---|---|---|
| `app.py` | Start-up: environment, theme, the window | yes |
| `window.py` | The main window: the Convert queue and its settings tabs, destinations, the ⋮ menu, jobs and their ffmpeg processes | yes |
| `gamesview.py` | The Library view: the PC and PSP side by side, details, saves, actions | yes |
| `brand.py` | XLT's logo, the app icon and the start-up animation (XLT's boot screen) | yes |
| `tagdialog.py` | The song editor (tags, cover, lyrics) | yes |
| `theme.py` | Following the desktop's light/dark mode, accent colour and font | yes |
| `media.py` | Probing videos, building the PSP-safe ffmpeg command lines | no |
| `convert.py` | Music, pictures, books, subtitles, ZIP; PS1 icons | no |
| `psx.py` | PS1 discs to EBOOT.PBP (PSISOIMG / PSTITLEIMG, CD music) | no |
| `cso.py` | ISO ↔ CSO | no |
| `games.py` | The PC library: readers for ISO, CSO, ZSO, PBP, PARAM.SFO; the index; save backups | no |
| `psplib.py` | The PSP library over sync: scan, matching, copy, send, remove, saves | no |
| `pspsync.py` | The worker thread that owns the USB link; tasks and uploads | signals only |
| `xltlink.py` | XLT's USB sync protocol, PC side (a copy of XLT's `tools/xltlink.py`) | no |
| `covers.py` | Box art from libretro (opt-in) | no |
| `update.py` | Checking GitHub for a newer release; replacing the AppImage | no |
| `devices.py` | Finding mounted Memory Sticks | no |
| `usbaccess.py` | The udev rule for the sync device (Linux); the WinUSB driver help (Windows) | no |

The modules without Qt are the ones the tests drive directly.

## Threads

- **The window** (Qt's main thread) runs one conversion at a time as a `QProcess` (ffmpeg), reading its progress
  from `-progress pipe:1`. Python-side jobs (ZIP, CSO, PS1 EBOOT, books, copies) run on a thread of their own and
  report back to the window; probing new files (ffprobe) runs on Qt's thread pool.
- **The PSP worker** (`pspsync.py`): one thread owns the USB link, so uploads, Library tasks, hellos and battery
  polls never cross. Its queue takes uploads and tasks; each task is a function of the link that can report
  progress and be stopped.
- **The Library scan** runs on a thread of its own and hands the games back with a signal; box art downloads too.

## Where things are kept

| What | Linux | Windows |
|---|---|---|
| Settings | `~/.config/xlt-tool/xlt-tool.conf` (QSettings) | the registry, `HKEY_CURRENT_USER\Software\xlt-tool` (QSettings) |
| Library index, icons, box art | `~/.cache/xlt-tool/` | `%LOCALAPPDATA%\xlt-tool\` |
| Save backups | `~/.local/share/xlt-tool/saves/<game ID>/<date>/` | `%APPDATA%\xlt-tool\saves\…` |
| PS1 parts copied from your EBOOT | `~/.local/share/xlt-tool/ps1/` | `%APPDATA%\xlt-tool\ps1\` |
| Work files while sending to the PSP | `~/.cache/xlt-tool/work/` | `%LOCALAPPDATA%\xlt-tool\work\` |

Uninstalling on Windows leaves these alone.

## Theme

`theme.py` reads, in order: the xdg-desktop-portal Settings (colour scheme and accent colour; GNOME 47+, KDE
Plasma 6 and others), KDE's `kdeglobals` (the full colour scheme), GNOME's gsettings, and GTK's `settings.ini`.
The palette is applied to Qt's Fusion style, so the bundled Qt looks the same on every distro, and it follows
changes while running. Icons come from the desktop's icon theme; file dialogs go through the portal. On
Windows the registry gives apps' light/dark mode and the accent colour (Settings → Personalisation → Colours),
Qt tells when they change, and the font is the system's (Segoe UI).

## Updates

`update.py` asks GitHub for the latest release of `boubounokefalos/xlt-tool` (at start, at most once a day, if
allowed, or from the ⋮ menu). Its tag is the version; the AppImage among its files is downloaded next to the
running one as `NAME.part`, checked (size, the SHA-256 GitHub lists, that it is an ELF program and that
`--version` prints the new version), made executable and renamed over the old one, which keeps running until
the app restarts. Run from source, or from a folder that can't be written, it only says a new version is out and
opens its page. **Skip this version** stops the reminders for one version.

## Start-up animation

`brand.py` draws XLT's "Home row" logo with the same geometry as XLT's `tools/make_logo.py`, and plays XLT's boot
screen over the window at start: the four cards pop in (an ease-out with a slight overshoot, 16 frames apart at
60 fps), a bar in their colours fills while the Library takes its first look (at most 4 s), then everything fades
into the window. A click or a key skips it; ⋮ → **Start-up animation** turns it off.
