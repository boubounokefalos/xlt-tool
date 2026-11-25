# XLT Cast

XLT Cast shows the game being played on the PSP in a window on the PC, with its sound, over the USB cable. The
PSP side is XLT's `xltcast` plugin (`plugin/xltcast` in [XLT](https://github.com/boubounokefalos/xlt)); this
page covers XLT Tool's side: `xlttool/castview.py` and the protocol client `xlttool/xltcast.py`.

## How the two sides meet

XLT installs `SEPLUGINS/xltcast.prx` and turns its line in the game plugin list on or off from
**Settings → XLT → XLT Cast**. In a game, **L+R+SELECT** starts the plugin's own USB device, and **HOME** or quitting the
game stops it. It uses the same IDs as XLT's USB sync (054C:01CB) but a different interface protocol (`C`, 0x43,
where sync is `S`, 0x53). So each client checks the protocol in the descriptor the system keeps *before*
opening the device: `xltlink.Link` refuses a casting PSP and `xltcast.Cast` refuses one in sync. On Windows that
matters, because WinUSB lets only one program have the device at a time. The plugin's MS OS 2.0 descriptors
make Windows give it WinUSB by itself, under its own interface GUID.

`xltcast.py` is a copy of XLT's `tools/xltcast.py`; keep the two the same, as with `xltlink.py`. Run as a
script, the copy in XLT has speed tests (`test`), saves frames (`frames`), records the sound (`record`) and has
a bare viewer (`view`).

## The protocol, in short

Every message starts with a 32-byte header (`XLTC` magic, op, status, sequence, length and four values; the
protocol is version 3). The PC sends `START` (copy mode, every K-th frame) and `STOP`; the PSP replies, then sends:

- **Frames:** 480×272 at 2 bytes a pixel (565, 5551 or 4444 as the game draws them; a 32-bit game's 8888 is
  turned into 565 on the PSP, since USB 2.0 can't carry 32-bit frames at 30 fps), with the frame's number and
  the time the last one took to send.
- **Sound:** `AUDIO` messages of up to 2048 stereo 16-bit frames at 44.1 kHz. The plugin hooks the game's
  `sceAudio` calls and mixes every channel the game plays on the PSP's clock.
- **NOP** (op 0x12, PS1 games only): ends every message there; `read_message` skips it. PS1 games send no
  sound yet (POPS doesn't use the hooked `sceAudio` calls).

## The PSP side's timing

The plugin copies the buffer the game has just put on screen (the display's frame buffer, checked at each
vblank), into a 255 KB buffer of its own, then sends it. The copy waits until the GE says it has finished
drawing, for at most 12 ms: some games show a buffer before the GE is done with it, and without the wait about
one frame in 14 came out with only part of the scene drawn (NFS Most Wanted: the road there, the sky, cars and
race display still black). If the game shows another buffer while the copy is made, the frame is dropped.
16-bit games copy in about 4.5 ms and stay at their own speed (30 of 30 fps casting); 32-bit games take 10 to
13 ms, which slows the PSP, hence **Every other frame**.

## The window

- **`Reader`** (a `QThread`) opens the PSP, sends `START`, and turns each frame into a `QImage` of its own
  (`frame_image`, never pointing into the USB buffer). It looks again every second while there is no PSP. A read
  timeout means only "no new frame" (a paused game, a loading screen), unless nothing has come for 3 s and the
  PSP is gone from USB too. A pulled cable can come back as timeouts, not as an error. It waits outside its
  `except` blocks: a traceback would keep the device open, which on Windows locks out the next try.
- **`Picture`** paints the frame as big as fits at the PSP's shape. With sharp pixels on it uses whole steps
  (every PSP pixel the same size), and falls back to free scaling when whole steps would leave it far from
  filling the window.
- **`CastWindow`**: status (fps, colour depth, sound), **Sharp pixels**, **All frames / Every other frame**
  (sent as a new `START` at once), **Screenshot** (Ctrl+S, the 480×272 frame to Pictures/XLT Cast), full screen
  (double-click, F11, Esc leaves), mute and volume. The settings are kept as `cast_sharp`, `cast_every`,
  `cast_volume` and `cast_muted`.

## Sound backends

The sound is kept close to the picture: when more than 0.12 s is waiting to be played, what comes in is dropped
until it catches up. There are three backends, tried in order, and the window works without sound if none is
there:

| Backend | Where | How |
|---|---|---|
| `Sound` | Qt Multimedia, when the PySide6 install has it (running from source) | `QAudioSink` in push mode |
| `PulseSound` | Linux, AppImage (no Qt Multimedia there) | `libpulse-simple` through ctypes, on its own thread (its writes block); works with PipeWire's Pulse side |
| `WinSound` | Windows build | `waveOut` from winmm through ctypes, 8 blocks of 8 KB, at most 4 queued (~80 ms) |

`XLTTOOL_SOUND=pulse` forces `PulseSound`, to test it from source.

## Tests

`tests/test_castview.py` runs the window offscreen against a pretend PSP (`FakeCast`) and a pretend sound output.
It covers 16-bit and 32-bit frames, whole-step scaling, fps, the screenshot, waiting again after a pulled cable
(as an error and as timeouts), STOP when the window closes, every other frame, and the Pulse backend's queue.
