# License and credits

## License

XLT Tool is free software under the **GNU General Public License v3.0** (see [LICENSE](../LICENSE)). It was
written with Claude (Anthropic's AI model) in Claude Code, by the author of [XLT](https://github.com/boubounokefalos/xlt),
whose code it shares where noted below.

## Code from other projects

| From | Used for | License |
|---|---|---|
| [XLT](https://github.com/boubounokefalos/xlt) (`tools/xltlink.py`, `tools/make_logo.py`, `src/bootdraw.c`) | `xlttool/xltlink.py` is a copy of XLT's USB sync client; `xlttool/brand.py` redraws XLT's logo and boot screen; the format readers follow XLT's `src/disc.c` and `src/probe.c` | GPL-3.0 |

Everything else in `xlttool/` was written for XLT Tool, including the LZ4 block decoder in `games.py` (from the
LZ4 block format description), the CSO writer, the PS1 EBOOT writer and the ZIP writer.

## Bundled in the AppImage

Each keeps its own licence. Inside the AppImage, XLT Tool's licence, this page and the licences of ffmpeg and atracdenc are in `usr/share/xlt-tool/`; the Python packages carry theirs in their `.dist-info` folders.

| Part | What for | License |
|---|---|---|
| [Python](https://www.python.org/) 3.13, built by [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Runs XLT Tool | PSF License (python-build-standalone's scripts: MPL-2.0; the libraries it includes keep theirs) |
| [Qt 6](https://www.qt.io/) through [PySide6](https://doc.qt.io/qtforpython/) and Shiboken6 | The window | LGPL-3.0 |
| [FFmpeg](https://ffmpeg.org/) 8.1, [BtbN's](https://github.com/BtbN/FFmpeg-Builds) GPL build, with x264, libass, zimg and others | Video, music and picture conversion, probing | GPL-3.0 (this build) |
| [atracdenc](https://github.com/dcherednik/atracdenc) by Daniil Cherednik | ATRAC3 for PS1 CD music, run as a program | LGPL-2.1 |
| [mutagen](https://github.com/quodlibet/mutagen) | Reading and writing song tags, covers and lyrics | GPL-2.0 or later |
| [pyusb](https://github.com/pyusb/pyusb) | USB sync | BSD-3-Clause |
| [libusb](https://libusb.info/) via [libusb-package](https://github.com/pyocd/libusb-package) | USB sync | LGPL-2.1 (libusb), Apache-2.0 (libusb-package) |
| [jeepney](https://gitlab.com/takluyver/jeepney) | D-Bus, for the desktop portal | MIT |
| The [AppImage](https://appimage.org/) runtime, from appimagetool | Starts the AppImage | MIT |

## Formats and references (no code taken)

- **[pop-fe](https://github.com/sahlberg/pop-fe)** by Ronnie Sahlberg: the layout of PS1 EBOOTs with CD music (the TOC as POPS reads it, the audio
  table, ATRAC3 tracks after the disc) and of multi-disc EBOOTs, followed from what it writes.
- **popstation** and the [PSDevWiki](https://www.psdevwiki.com/psp/) page on `EBOOT.PBP`: the PSISOIMG0000 and
  PSTITLEIMG0000 formats.
- The CISO (CSO) format by BOOSTER, CSO v2 and ZSO as read by ARK's ISO drivers and XLT.
- The [LZ4 block format](https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md).
- The Freedesktop and xdg-desktop-portal specifications (Settings, FileChooser), Microsoft's OS descriptors
  (for WinUSB).

## Data used online (only when turned on)

- [libretro-database](https://github.com/libretro/libretro-database): game IDs to names (`metadat/serial`
  for the PSP, `metadat/redump` for PS1, the latter from [Redump](http://redump.org/)).
- [libretro-thumbnails](https://github.com/libretro-thumbnails) and thumbnails.libretro.com: the box art.

Neither is bundled: the lists and covers are downloaded on your PC when you turn box art on.

## Not included

XLT Tool contains no Sony software. A PS1 EBOOT needs Sony's DATA.PSP, STARTDAT and (for several discs) the
PSTITLEIMG header: XLT Tool copies them from a PS1 EBOOT you already have, on your PC, and never ships them.

## Trademarks

PlayStation, PSP, PlayStation Portable, UMD, XMB and POPS are trademarks or registered trademarks of Sony
Interactive Entertainment Inc. Memory Stick and ATRAC3 are trademarks of Sony Group Corporation. Windows is a
trademark of Microsoft. Claude and Claude Code are trademarks of Anthropic. Other product and company names are
trademarks of their owners, used here to say what XLT Tool works with.

XLT Tool is an independent, unofficial project, not affiliated with, endorsed or approved by Sony or the ARK
developers. It comes with no warranty (see the GPL-3.0).
