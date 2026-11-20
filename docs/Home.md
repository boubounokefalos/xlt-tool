# XLT Tool developer docs

These pages are for anyone who wants to know how XLT Tool works in detail, or change it. For what it is and how
to use it, see the [README](../README.md). They work as a GitHub wiki too: copy the `.md` files in unchanged.

| Page | What it covers |
|---|---|
| [Conversions](Conversions.md) | Every output format and why: video (the PSP's H.264 limits), music, pictures, books, subtitles, ZIP, PS1 EBOOTs (PSISOIMG, CD music, several discs), ISO ↔ CSO, file names |
| [The Library](Library.md) | Reading games offline, the index, saves on the PC, the PSP side (matching, send, copy, remove, saves), play time, box art |
| [Sending to the PSP over USB](USB-Sync.md) | USB sync against USB mode, the worker thread, protocol versions, access on Linux and Windows |
| [XLT Cast](Cast.md) | The game on the PC: the window, the reader thread, frames and sound, the sound backends, a pulled cable |
| [How XLT Tool is built](Architecture.md) | Modules, threads, where things are kept, the theme, updates, the start-up animation |
| [Building and testing](Building-and-Tests.md) | Running from source, the AppImage, the tests, the README's media |
| [License and credits](Credits.md) | The licence, code from other projects, what the AppImage bundles, formats and data used |
