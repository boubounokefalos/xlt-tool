# The Library

The **Library** tab lists the PSP games on your PC and, with XLT connected over USB sync, the ones on the PSP,
side by side. Its code: `xlttool/games.py` (the readers and the index, no Qt), `xlttool/psplib.py` (the PSP
side, no Qt), `xlttool/gamesview.py` (the view), `xlttool/covers.py` (box art).

## Games on the PC

Add folders with **Folders**. They are looked through at every start and when you come back to the tab; only new
or changed files are read again (the index and icons are kept in `~/.cache/xlt-tool`).

- **PSP games:** ISO, CSO v1 and v2 (deflate or LZ4 blocks) and ZSO (LZ4). The readers follow XLT's own
  (`src/disc.c`, `src/probe.c`); LZ4 is decoded in Python, as only a few blocks are read per game.
- **Homebrew, PSN and PS1 games:** `EBOOT.PBP`. A folder with an `EBOOT.PBP` is one game (its other files
  included in its size); a loose `.pbp` is a game too. Disc images beside an EBOOT are still listed.
- **Not listed:** game updates (PARAM.SFO category `PG`) and UMD stubs.

What is shown comes from the games themselves, offline: title, game ID, region (from the ID's prefix), version,
the firmware it needs, ICON0 and PIC1. Search by title or ID, show one kind, sort by name, size, newest, region,
most played or last played.

## Saves on the PC

Each game shows the backups of its saves kept on the PC, in `~/.local/share/xlt-tool/saves/<game ID>/`, one
dated folder per backup. A save belongs to a game when its folder's name starts with the game's ID
(`ULUS10041DATA00`); PS1 saves likewise.

## With the PSP connected

With XLT's USB sync on, the PSP's games are read where XLT finds them: `ISO/` and two levels of folders under it
(not `ISO/VIDEO`), and `PSP/GAME/*/EBOOT.PBP` (a category folder one level deeper). A game's PARAM.SFO and icons
are read in pieces with sync's READ (protocol 3), so a 1.7 GB image costs a few small reads, and only once: a
second look costs only the folder listings.

The two sides are matched by game ID (an ISO on the PC and a CSO of it on the PSP are one game); homebrew, whose
IDs are often a template's, by title. Badges: **PC + PSP** (green), **PSP only** (orange), **PC only** (grey),
and a filter for each.

| Action | What happens |
|---|---|
| **Send to the PSP** | Checked against free space first. An image goes to `ISO/`, a homebrew or PS1 folder to `PSP/GAME/`, a loose `.pbp` becomes the `EBOOT.PBP` of its own folder. Save backups the PSP doesn't have go too (never over a save that is there). A folder cut off halfway is deleted again. |
| **Copy to this PC** | A game that is only on the PSP, into the library's first folder, saves included. Can be stopped; a stopped or failed copy leaves nothing half-copied. |
| **Back up saves to this PC** | The game's save folders, as a new dated backup. |
| **Put saves back on the PSP** | The newest backup of each save. A save already there is backed up first and replaced without ever leaving half of one: the backup goes in as `FOLDER.xltnew`, the PSP's is renamed `FOLDER.xltold`, the new one takes its name, the old one goes. A restore cut off earlier is tidied up the next time (the old save comes back). |
| **Remove from the PSP** | Backs the game's saves up first, always; deleting them from the PSP too is a separate tick. A game that isn't on this PC gets a warning first. Folders go in one command (sync's DELETE_TREE). |

Right-click a game for the same actions; the bottom line shows the PSP's free space (counted from the FAT in
about a second, protocol 2) and its battery (protocol 4: "64%, charging"; time left only where the PSP gives a real
estimate, which a PSP-E1000 doesn't).

## Play time and favorites

XLT counts how long each game runs (its xltshot plugin, PS1 games included) and lists the games with their play
history in `PSP/GAME/XLT/cache/games.txt` (format `XLT games 1`, tab-separated: kind, path, ID, last played,
seconds played, favorite, title). XLT Tool reads it over sync: a game's details read "3 min · last played today",
the **Most played** and **Last played** sorts use it, and XLT's favorites get a ★ (**Favorites first** puts them
on top).

## Box art

Off unless you turn it on (⋮ → **Box art for games**). A game's ID is looked up in the
[libretro-database](https://github.com/libretro/libretro-database) (`metadat/serial` for the PSP,
`metadat/redump` for PS1) to get its name, and the cover of that name is downloaded from
[libretro-thumbnails](https://github.com/libretro-thumbnails) through thumbnails.libretro.com (another region's
when the game's own has none). Only the names of the lists and covers are sent. The lists are kept for 30 days,
each cover once, in `~/.cache/xlt-tool/covers`. Covers show in a game's details, and a PS1 EBOOT made here can
take its icon from one.
