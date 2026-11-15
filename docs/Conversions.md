# Conversions

Everything the **Convert** tab makes, and why each output looks the way it does. The limits come from what the
PSP's firmware and XLT's own players accept (XLT's `src/audiofmt.c`, `music.c`, `flac.c`, `imgdecode.c`,
`reader.c`, `srt.c`, `zip.c`, `disc.c`), checked on a real PSP.

Each file in the queue is probed first (ffprobe for media), gets a kind (video, music, picture, book, subtitles,
ZIP, PS1 game, PSP game) and the settings tab of that kind. Jobs run one at a time; a finished file is written as
`NAME.part` and renamed only once it is complete. With the PSP connected over USB sync, the next job converts
while the last one is sent.

## Video

| | |
|---|---|
| Video | H.264 Main@3.0, yuv420p, ≤3 reference frames, **no weighted prediction**, no B-pyramid |
| Size | 480×272 (every PSP) or 720×480 (PSP-2000/3000/Go, XMB player only) |
| Frame rate | the source's rate up to 30 fps, otherwise 29.97 |
| Audio | AAC-LC stereo 48 kHz, optional loudness normalisation (ffmpeg's `loudnorm`, −16 LUFS) |
| Container | ffmpeg's `psp` muxer (MSNV brand), faststart |
| Extras | `NAME.THM` XMB thumbnail; subtitles burned in (sidecar `.srt`/`.ass`/`.vtt`, or embedded text, PGS or VobSub) or written as a separate `.srt` XLT can turn on and off |

Weighted prediction and more than 3 reference frames make the PSP's decoder reject the stream (error
`0x80628002`), so x264 is pinned to `weightp=0:weightb=0:ref=3`. NVIDIA's NVENC encoder is offered as well when ffmpeg and the
driver have it, held to the same limits.

Sources are handled as they come: anamorphic video is SAR-corrected, interlaced video is deinterlaced (bwdif),
HDR is tone-mapped to SDR, and the picture is fitted into the frame with bars where needed. `xlttool/media.py`
builds the ffmpeg command lines and parses ffmpeg's progress.

## Music

| Input | Output | Notes |
|---|---|---|
| Any audio, or a video's soundtrack | MP3, M4A (AAC-LC) or FLAC (16-bit) | Rates limited to the ones the PSP plays (8 to 48 kHz); tags, cover and `.lrc` lyrics kept; files that already fit are copied, not re-encoded |

**Edit tags, cover and lyrics** (one song or several at once) sets title, artist, album, track, year, genre, the
cover and `.lrc` lyrics, on the converted file or, if ticked, on the original MP3, M4A or FLAC (mutagen).
A `.lrc` dropped in alone becomes UTF-8.

## Pictures, books, subtitles and ZIP

| Input | Output | Stick folder | Notes |
|---|---|---|---|
| WebP, HEIC, AVIF, TIFF, GIF, … | JPEG or PNG | `PICTURE` | Turned the right way up; at most 8192 px a side; transparency becomes white in JPEG |
| EPUB, FB2, DOCX, ODT, HTML, text in any encoding | UTF-8 `.txt` | `BOOKS` | Split into parts under the Reader's 2 MB limit |
| SRT, ASS, VTT in any encoding | UTF-8 `.srt` | `VIDEO` | Named after the video, under 2 MB |
| A folder (**Make ZIP**), `.tar.*` | ZIP | `DOWNLOADS` | Stored or deflated only, no ZIP64 (4 GB, 65 535 files), FAT-safe names, symlinks skipped |

## PS1 games to EBOOT

A `.cue` (or a lone `.bin`/`.img`) becomes `<game ID>/EBOOT.PBP` in `PSP/GAME`, which the PSP's PS1 emulator
(POPS) runs through ARK. `xlttool/psx.py` writes it:

- **PARAM.SFO** with the title and the disc ID read from the disc's `SYSTEM.CNF`.
- **Icons:** ICON0 and PIC1 from the same game in the library, else a picture beside the disc (`cover.jpg`, the
  game's name `.png`, …) or its box art (if turned on), else the title drawn.
- **DATA.PSP and STARTDAT:** a small Sony program and start screen, the same in every PS1 EBOOT. XLT Tool does
  not include them: the first time, it copies them from a PS1 EBOOT you already have (from the library, or one
  you pick) into `~/.local/share/xlt-tool/ps1`.
- **PSISOIMG0000** (one disc): the disc ID, the TOC as POPS reads it, the CD audio table, the title, then an index
  of 16-sector blocks (offset, size and a SHA-1 of the plain sectors) and the blocks themselves, each raw deflate,
  compressed on several threads.
- **CD music:** the image keeps the data track; every audio track is encoded to ATRAC3 by
  [atracdenc](https://github.com/dcherednik/atracdenc) (run as a program; bundled in the AppImage, built without
  libsndfile and fully static) and stored after the disc, listed in the audio table. This is the layout pop-fe
  writes.
- **Several discs** (up to 5, files named "(Disc 1)", "(Disc 2)", …, or put together with **Combine discs**):
  one PSTITLEIMG with a PSISOIMG per disc. Its header is copied once from an EBOOT that has one (pop-fe writes it
  even for one disc). On the PSP, HOME → Reset game picks the disc.

Tried on a PSP-E1004 with ARK-5: Crash Bandicoot and Buster Bros. Collection (60 CD music tracks) from their
cues, and Gran Turismo 2's two discs in one EBOOT.

## PSP games: ISO and CSO

`.iso` becomes `.cso` (v1, deflate, 2048-byte blocks, compressed on several threads; `xlttool/cso.py`), and
`.cso`/`.zso` become `.iso` again. XLT and ARK's ISO drivers read CSO v1 exactly as written here.

## Names

The PSP keeps file names in Shift-JIS, so a character Shift-JIS lacks makes a file unreachable on the PSP.
Names are made ones the PSP can hold: such characters lose their accent or become the nearest plain one
(`Café` → `Cafe`, `Καλημέρα` → `Καλημερα`, `–` → `-`), at most 150 bytes. A name that is taken gets
" 2", " 3", ….

## Where files go

On a Memory Stick (found at any mount point, by its `PSP` folder) or over USB sync, each kind goes where XLT
looks for it: `VIDEO`, `MUSIC`, `PICTURE`, `BOOKS`, `DOWNLOADS`, `ISO`, `PSP/GAME`. Other destinations: next to
each source file, or a folder you choose.
