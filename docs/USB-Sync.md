# Sending to the PSP over USB

With **Settings → XLT → USB sync** on in XLT and the cable in, the PSP shows up as a destination ("PSP-3000 over
USB (XLT)"): no USB mode, XLT keeps running, and its library updates by itself. The protocol and the PSP side are
described in XLT's [USB sync, in depth](https://github.com/boubounokefalos/xlt/blob/main/docs/USB-Sync.md); this
page is the PC side.

## How it differs from USB mode

In USB mode (the PSP's mass storage mode) the computer sees the Memory Stick as a drive and writes its sectors
with its own FAT driver. The PSP's FAT driver must then keep off the stick entirely, or the two would overwrite
each other's changes: that is why the PSP shows only "USB connected" and XLT can't be used meanwhile.

USB sync never hands the stick over. XLT shows up as a device of its own (054c:01cb) that takes file commands
("here is VIDEO/film.mp4", "delete MUSIC/old.mp3"), and the PSP's own FAT driver carries them out. So:

- XLT keeps running, and its library updates by itself after the last file.
- A file is written as `NAME.xltpart` and only renamed into place once it is complete and its CRC-32 matches:
  a pulled cable leaves nothing half-written behind.
- Only XLT's folders can be changed (`ISO`, `VIDEO`, `MUSIC`, `PICTURE`, `BOOKS`, `DOWNLOADS`, `PSP/GAME`), and
  save folders in `PSP/SAVEDATA`; system folders and plugins are out of reach.
- Names are always written so the PSP can hold them (Shift-JIS).
- It needs XLT Tool (or XLT's `tools/xltlink.py`); a file manager can't use it.
- It is a little slower, as the PSP's CPU does the writing: 64 MB at 8.3 MB/s, against 9.6 MB/s in USB mode. The
  Memory Stick, not USB, is the limit.

## In XLT Tool

`xlttool/pspsync.py` runs one worker thread that owns the link (`xlttool/xltlink.py`, a copy of XLT's
`tools/xltlink.py`, over pyusb and libusb):

- it looks for the PSP every 2 s while there is none;
- while idle it says hello every 3 s (XLT shows "PC connected" while it hears from the PC) and asks for the
  battery every 15 s;
- it sends the files of finished jobs one job at a time, while the next job converts: each file under a free
  name ("Name 2" if "Name" is there), with its companions (`.THM` thumbnail, `.srt`, `.lrc`) under the same name;
- it runs the Library's tasks (scan, copy, send, remove, saves) one at a time, each one stoppable. A task stopped
  mid-transfer drops the link and starts over after the PSP's 3 s timeout.

The protocol versions XLT Tool uses: 2 (free space from the FAT, folder delete, writable save folders),
3 (READ, a piece of a file), 4 (POWER, the battery). An older XLT works for sending files; the Library's PSP side
needs protocol 3.

## Access

- **Linux:** only root may open a new USB device, so the first time choose **PSP over USB: allow access…** in
  the destination list and give your password once (pkexec). It installs `/etc/udev/rules.d/60-xltsync.rules`
  (the same as XLT's `tools/60-xltsync.rules`), which gives the logged-in user access through logind's
  `uaccess` tag. Without pkexec, the commands are shown to copy.
- **Windows** (not released yet): the PSP answers Microsoft's OS descriptor requests, so Windows binds its own
  WinUSB driver without an install. This hasn't been tried on real Windows yet.
