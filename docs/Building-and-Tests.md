# Building and testing

## Run from source

```bash
./run-dev.sh [files…]
```

Needs Python 3.11 or later with PySide6 and jeepney (`XLTTOOL_PYTHON` picks the Python; jeepney, mutagen and
pyusb can also sit in `dev/`), and ffmpeg on `PATH`. `XLTTOOL_DEBUG=1` prints which theme source was used;
`XLTTOOL_NO_INTRO=1` skips the start-up animation.

## The AppImage

```bash
./packaging/build-appimage.sh
```

Downloads, cached in `build/cache` and checked by SHA-256:

| What | Version | Why |
|---|---|---|
| [python-build-standalone](https://github.com/astral-sh/python-build-standalone) CPython | 3.13 | A Python that runs on any distro |
| PySide6-Essentials (Qt 6) | 6.12 | The window |
| jeepney | 0.9 | D-Bus, for the desktop portal (theme, file dialogs) |
| mutagen | 1.48 | Music tags |
| pyusb, libusb-package | 1.3.1, 1.0.30 | USB sync, with its own libusb |
| [BtbN ffmpeg](https://github.com/BtbN/FFmpeg-Builds) (GPL, shared) | 8.1 | Conversions; 8.1 keeps NVENC working on 580-series drivers |
| [atracdenc](https://github.com/dcherednik/atracdenc) (source) | commit 0fe8e1a | PS1 CD music, built here with cmake and g++: without libsndfile (`packaging/atracdenc/pcm_io_wav.cpp` reads WAV instead) and fully static |
| [appimagetool](https://github.com/AppImage/appimagetool) | 1.9.1 | Packs the AppImage |

Output: `dist/XLT_Tool-<version>-x86_64.AppImage`. It runs on distros with glibc 2.34 or later (Ubuntu 22.04+,
Debian 12+, Fedora 35+, RHEL 9+, Arch, openSUSE Tumbleweed…), on X11 or Wayland. The licences of the bundled
parts are in `usr/share/xlt-tool/` inside it.

## Windows

From source on Windows: `.\run-dev.ps1 [files…]` makes `.venv` on its first run (PySide6, mutagen, pyusb,
libusb-package) and starts the app; ffmpeg and ffprobe go on `PATH` or beside the program. The tests run the
same way: `.venv\Scripts\python -I -m unittest discover -s tests`. USB sync needs nothing installed: XLT's sync
device asks Windows for the WinUSB driver itself.

The build, on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
```

It makes the installer, `dist/XLT_Tool-<version>-windows-x64-setup.exe`
([packaging/xlt-tool.iss](../packaging/xlt-tool.iss), Inno Setup 6: `winget install JRSoftware.InnoSetup`;
`-NoInstaller` skips it), from a PyInstaller folder build, `XLT Tool.exe` with Python, PySide6-Essentials, mutagen, pyusb and
libusb-package (the same versions as the AppImage), adds BtbN's Windows ffmpeg 8.1 (GPL, shared) and
[atracdenc 0.2.3's own Windows build](https://github.com/dcherednik/atracdenc/releases/tag/0.2.3) to its `bin`
folder and the licences to `licenses`, tries it (libx264 in ffmpeg, atracdenc encoding, libusb, the exe
starting) and zips it as well: `dist/XLT_Tool-<version>-windows-x64.zip`, the portable one. `-TestInstaller`
installs the installer silently into `build\windows\install-test`, starts the app from there and uninstalls it.
Smart App Control, when it is on, blocks the unsigned programs, so the smoke test fails on such a PC: build there
with GitHub Actions instead. Downloads are cached in `build/cache`;
atracdenc's is pinned by SHA-256, ffmpeg's rolling build is checked against the digest GitHub gives for it.
`XLTTOOL_PYTHON` picks the Python it builds with.

Releases are built by GitHub Actions instead ([.github/workflows/windows.yml](../.github/workflows/windows.yml),
on each `v*` tag, each push to the `windows` branch, or by hand): the same script with `-TestInstaller` on GitHub's
Windows runner, the installer and the zip kept as the run's artifacts and, on a tag, put on that tag's release. A
file already on a release is never replaced (people's downloads and winget check its SHA-256): a fix is a new
version. The build is not signed.

## Tests

```bash
python3 -I -m unittest discover -s tests
```

| Test | What it checks |
|---|---|
| `test_games.py` | The game readers against XLT's own test images (ISO, CSO v1, CSO v1 aligned, CSO v2, ZSO, homebrew and update PBPs, from `~/xlt/build/fixtures` or `$XLT_FIXTURES`): the same bytes from every format, titles, icons; LZ4; PARAM.SFO; the folder scan and its index; save backups |
| `test_psplib.py` | The PSP side against a pretend PSP (a folder that answers like XLT's sync): the scan, matching, copying games and saves to the PC, sending, removing, putting saves back (the `.xltnew`/`.xltold` swap, a cut-off restore tidied up) |
| `test_cso_psx.py` | ISO → CSO → ISO byte for byte; PS1 EBOOTs: the PSISOIMG layout, the index and SHA-1s, the TOC from cue sheets, the audio table, multi-disc PSTITLEIMG |
| `test_covers.py` | Box art against pretend libretro lists: names by ID, another region's art, the cache |
| `test_update.py` | The update check against a pretend GitHub: version order, the download checks, the swap |
| `test_castview.py` | The XLT Cast window against a pretend PSP that sends frames: 16-bit and 32-bit colours, the picture in whole steps, fps, a screenshot at 480×272, waiting again when the cable goes, STOP sent when the window closes (Qt offscreen) |

## The README's media

`tools/make_demo_media.py` renders the README's screenshots, the walk-through video and its GIF teaser from the
real window, offscreen, over a made-up library: the games, their icons, songs and pictures are generated, the
PSP is a pretend one (a folder answering like XLT's sync), and box art stays off, so nothing in them belongs to
anyone else. It needs the same Python as `run-dev.sh` and ffmpeg.
