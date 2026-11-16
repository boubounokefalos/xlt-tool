"""Probing source videos and building PSP-safe ffmpeg command lines.

PSP H.264 constraints used here (from hardware testing on a real PSP):
  * H.264 Main profile, level 3.0, yuv420p
  * at most 3 reference frames
  * NO weighted prediction (weightp=0, weightb=0) -- the firmware decoder
    rejects streams that use it (error 0x80628002)
  * no B-pyramid
  * 480x272 (every model) or 720x480 (PSP-2000/3000/Go, XMB player)
  * <= 30 fps
  * AAC-LC stereo, 48 kHz
  * written with ffmpeg's "psp" MP4 muxer
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".ogv", ".3gp", ".divx", ".rmvb",
    ".asf", ".f4v",
}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
EXTERNAL_SUB_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt")

RESOLUTIONS = {
    "480x272": (480, 272),
    "720x480": (720, 480),
}

# name -> (crf, maxrate kbit/s, audio kbit/s)
QUALITY = {
    "small": (25, 800, 96),
    "balanced": (22, 1500, 128),
    "high": (19, 2500, 160),
}

# x264 presets offered as "Speed". All keep the PSP-required flags.
SPEED = {"fast": "faster", "balanced": "medium", "slow": "slow"}

# NVENC (NVIDIA GPU) presets for the same speed names.
NVENC_PRESET = {"fast": "p3", "balanced": "p5", "slow": "p7"}

FAT_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _bundled(name: str) -> str | None:
    env = os.environ.get(f"XLTTOOL_{name.upper()}") or os.environ.get(f"PSPCONV_{name.upper()}")
    if env and os.access(env, os.X_OK):
        return env
    appdir = os.environ.get("APPDIR")
    if appdir:
        p = os.path.join(appdir, "usr", "bin", name)
        if os.access(p, os.X_OK):
            return p
    if sys.platform == "win32":
        # The Windows build keeps them beside XLT Tool.exe, or in its bin folder.
        here = os.path.dirname(sys.executable)
        for p in (os.path.join(here, name + ".exe"), os.path.join(here, "bin", name + ".exe")):
            if os.path.isfile(p):
                return p
    return shutil.which(name)


def ffmpeg_path() -> str | None:
    return _bundled("ffmpeg")


def ffprobe_path() -> str | None:
    return _bundled("ffprobe")


@dataclass
class Track:
    index: int          # absolute stream index in the file
    rel: int            # index among streams of the same type
    codec: str
    language: str = ""
    title: str = ""
    channels: int = 0
    default: bool = False
    forced: bool = False

    def label(self, kind: str) -> str:
        bits = []
        if self.language and self.language != "und":
            bits.append(self.language.upper())
        if self.title:
            bits.append(self.title)
        if kind == "audio" and self.channels:
            bits.append({1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(
                self.channels, f"{self.channels} ch"))
        bits.append(self.codec)
        if self.forced:
            bits.append("forced")
        return f"#{self.rel + 1} · " + " · ".join(bits)


@dataclass
class SubChoice:
    """What subtitles to burn in. kind: none | embedded | external."""
    kind: str = "none"
    track: Track | None = None
    path: str = ""

    @property
    def is_bitmap(self) -> bool:
        return self.kind == "embedded" and bool(self.track) and self.track.codec in BITMAP_SUB_CODECS

    def label(self) -> str:
        if self.kind == "embedded" and self.track:
            return "Embedded " + self.track.label("sub")
        if self.kind == "external":
            return "File · " + Path(self.path).name
        return "No subtitles"


@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    sar: tuple[int, int] = (1, 1)
    vcodec: str = ""
    interlaced: bool = False
    hdr: bool = False
    rotation: int = 0
    audio: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)
    external_subs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.width:
            parts.append(f"{self.width}×{self.height}")
        if self.fps:
            parts.append(f"{self.fps:.3g} fps")
        if self.duration:
            parts.append(format_time(self.duration))
        if self.hdr:
            parts.append("HDR")
        na, ns = len(self.audio), len(self.subs) + len(self.external_subs)
        if na != 1:
            parts.append(f"{na} audio tracks" if na else "no audio")
        if ns:
            parts.append(f"{ns} subtitle{'s' if ns != 1 else ''}")
        return " · ".join(parts)

    def default_audio(self) -> Track | None:
        if not self.audio:
            return None
        return next((a for a in self.audio if a.default), self.audio[0])

    def sub_choices(self) -> list[SubChoice]:
        out = [SubChoice()]
        out += [SubChoice("external", path=p) for p in self.external_subs]
        out += [SubChoice("embedded", track=t) for t in self.subs
                if t.codec in TEXT_SUB_CODECS or t.codec in BITMAP_SUB_CODECS]
        return out

    def auto_sub(self, text_only: bool = False) -> SubChoice:
        """The default when subtitles are wanted: sidecar file, else forced,
        else default-flagged, else first usable embedded track."""
        choices = self.sub_choices()[1:]
        if text_only:   # picture-based tracks can't become a .srt
            choices = [c for c in choices if not c.is_bitmap]
        if not choices:
            return SubChoice()
        for test in (lambda c: c.kind == "external",
                     lambda c: c.track and c.track.forced,
                     lambda c: c.track and c.track.default):
            hit = next((c for c in choices if test(c)), None)
            if hit:
                return hit
        return choices[0]


def ff_file(path: str) -> str:
    """Path for ffmpeg's command line: names like "Show: Ep 1.mkv" would
    otherwise be parsed as a URL protocol."""
    return "file:" + os.path.abspath(path)


def format_time(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _ratio(text: str | None) -> float:
    if not text or text in ("0/0", "N/A"):
        return 0.0
    if "/" in text:
        a, b = text.split("/", 1)
        try:
            return float(a) / float(b) if float(b) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def find_external_subs(path: str) -> list[str]:
    src = Path(path)
    stem = src.stem.lower()
    found = []
    try:
        for f in sorted(src.parent.iterdir()):
            name = f.name.lower()
            if (f.is_file() and name.endswith(EXTERNAL_SUB_EXTENSIONS)
                    and (name.startswith(stem + ".") or Path(name).stem == stem)):
                found.append(str(f))
    except OSError:
        pass
    return found


def probe(path: str) -> MediaInfo:
    exe = ffprobe_path()
    if not exe:
        raise RuntimeError("ffprobe was not found")
    res = subprocess.run(
        [exe, "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", ff_file(path)],
        capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        msg = res.stderr.strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "ffprobe failed")
    data = json.loads(res.stdout or "{}")
    info = MediaInfo(path=path)
    info.duration = _ratio(data.get("format", {}).get("duration"))
    counters = {"audio": 0, "subtitle": 0}
    for s in data.get("streams", []):
        ctype = s.get("codec_type")
        disp = s.get("disposition", {})
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        if ctype == "video" and not info.vcodec and not disp.get("attached_pic"):
            info.vcodec = s.get("codec_name", "")
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.fps = _ratio(s.get("avg_frame_rate")) or _ratio(s.get("r_frame_rate"))
            if info.fps > 240:          # bogus timebase-derived rates
                info.fps = _ratio(s.get("r_frame_rate"))
            sar = s.get("sample_aspect_ratio", "1:1")
            try:
                a, b = (int(x) for x in sar.split(":"))
                if a > 0 and b > 0:
                    info.sar = (a, b)
            except ValueError:
                pass
            fo = s.get("field_order", "progressive")
            info.interlaced = fo not in ("progressive", "unknown", "")
            info.hdr = s.get("color_transfer") in ("smpte2084", "arib-std-b67")
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    info.rotation = int(sd["rotation"]) % 360
            if not info.duration:
                info.duration = _ratio(s.get("duration"))
        elif ctype in counters:
            t = Track(
                index=int(s["index"]), rel=counters[ctype],
                codec=s.get("codec_name", "?"),
                language=tags.get("language", ""), title=tags.get("title", ""),
                channels=int(s.get("channels") or 0),
                default=bool(disp.get("default")), forced=bool(disp.get("forced")))
            counters[ctype] += 1
            (info.audio if ctype == "audio" else info.subs).append(t)
    if not info.vcodec:
        raise RuntimeError("no video stream in this file")
    info.external_subs = find_external_subs(path)
    return info


# Legacy 8-bit encodings subtitle files commonly use, most likely first.
SUB_ENCODINGS = ("cp1253", "cp1252", "cp1251", "cp1250", "cp1254", "cp1257",
                 "cp1255", "cp1256", "iso-8859-7", "koi8-r")


def _script(ch: str) -> str:
    import unicodedata
    try:
        return unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return "?"


def _plausibility(text: str) -> int:
    """Higher is better. Penalises what wrong code pages produce: words that
    mix scripts, capitals in the middle of words, and "Latin" words made mostly
    of accented letters (Greek/Cyrillic decoded as cp1252 look like that)."""
    # A symbol such as ¿ ³ ¹ between two letters means a letter was decoded
    # wrongly (Polish "żółć" read as cp1252); so do Icelandic-only letters.
    score = -5 * len(re.findall(r"(?<=[^\W\d_])[^\x00-\x7f\w\s](?=[^\W\d_])", text))
    score -= 2 * sum(text.count(c) for c in "ðýþÐÝÞ")
    for word in re.findall(r"[^\W\d_]+", text):
        if word.isascii():
            continue
        scripts = {"LATIN" if c.isascii() else _script(c) for c in word}
        n = len(word)
        if len(scripts) > 1:
            score -= 3 * n
            continue
        ok_case = word.islower() or word.isupper() or (word[0].isupper() and word[1:].islower())
        score += n if ok_case else -2 * n
        if "LATIN" in scripts and sum(not c.isascii() for c in word) * 2 > n and n > 2:
            score -= 3 * n
    return score


def decode_subtitle(data: bytes) -> tuple[str, str]:
    """Return (text, encoding) for a subtitle file of unknown encoding."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"),
                     (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace"), enc
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = None
    for rank, enc in enumerate(SUB_ENCODINGS):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        key = (_plausibility(text), -rank)
        if best is None or key > best[0]:
            best = (key, text, enc)
    if best:
        return best[1], best[2]
    return data.decode("cp1252", errors="replace"), "cp1252"


def subtitle_to_utf8(src: str, dst: str) -> str:
    """Write a UTF-8 copy of a subtitle file (libass only accepts UTF-8).
    Returns the detected source encoding."""
    with open(src, "rb") as f:
        text, enc = decode_subtitle(f.read())
    with open(dst, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return enc


@dataclass
class Options:
    resolution: str = "480x272"
    fit: str = "fit"              # fit | fill | stretch
    quality: str = "balanced"
    normalize: bool = True
    thumbnail: bool = True
    speed: str = "balanced"       # fast | balanced | slow
    encoder: str = "x264"         # x264 | nvenc


def _filter_path(path: str) -> str:
    """Escape a path for use inside a filtergraph option value."""
    p = path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "'" + p.replace(",", "\\,").replace("[", "\\[").replace("]", "\\]") + "'"


# The PSP shows every video full-screen at 16:9, so 720x480 is anamorphic:
# lay the picture out on a square-pixel 854x480 canvas, then squeeze it.
DISPLAY_SIZE = {"480x272": (480, 272), "720x480": (854, 480)}


def video_filters(info: MediaInfo, opts: Options, overlay: str = "") -> list[str]:
    """overlay: filter (e.g. subtitles) applied on the square-pixel canvas,
    after fitting and before any anamorphic squeeze."""
    W, H = RESOLUTIONS[opts.resolution]
    DW, DH = DISPLAY_SIZE[opts.resolution]
    f: list[str] = []
    if info.interlaced:
        f.append("bwdif=mode=send_frame")
    if info.hdr:
        f.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                 "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
    if info.sar != (1, 1):
        f.append(f"scale=trunc(iw*{info.sar[0]}/{info.sar[1]}/2)*2:ih,setsar=1")
    if opts.fit == "fill":
        f.append(f"scale={DW}:{DH}:force_original_aspect_ratio=increase:flags=lanczos")
        f.append(f"crop={DW}:{DH}")
    elif opts.fit == "stretch":
        f.append(f"scale={DW}:{DH}:flags=lanczos")
    else:
        f.append(f"scale={DW}:{DH}:force_original_aspect_ratio=decrease"
                 f":force_divisible_by=2:flags=lanczos")
        f.append(f"pad={DW}:{DH}:(ow-iw)/2:(oh-ih)/2:color=black")
    f.append("setsar=1")
    if overlay:
        f.append(overlay)
    if (DW, DH) != (W, H):
        f.append(f"scale={W}:{H}:flags=lanczos")
    if info.fps > 30.5 or info.fps == 0:
        f.append("fps=30000/1001")
    # display aspect = 16:9 for 720x480 (SAR 32:27), square pixels otherwise
    f.append("setsar=32/27" if (DW, DH) != (W, H) else "setsar=1")
    f.append("format=yuv420p")
    return f


def build_command(info: MediaInfo, dst: str, opts: Options,
                  audio: Track | None, sub: SubChoice,
                  sub_file: str | None = None, srt_out: str | None = None,
                  srt_track: Track | None = None) -> list[str]:
    """sub_file: a path with no special characters (a symlink made by the
    caller) to read text subtitles from, so filtergraph escaping is moot.
    srt_out / srt_track: also write that embedded text track to a .srt."""
    crf, maxrate, abr = QUALITY[opts.quality]
    cmd = [ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-y",
           "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-i", ff_file(info.path)]

    overlay = ""
    if sub.kind == "embedded" and sub.track and sub.track.codec not in BITMAP_SUB_CODECS:
        overlay = f"subtitles={_filter_path(sub_file or info.path)}:si={sub.track.rel}"
    elif sub.kind == "external" and sub.path:
        overlay = f"subtitles={_filter_path(sub_file or sub.path)}"
    vf = video_filters(info, opts, overlay)
    vmap: str
    if sub.kind == "embedded" and sub.track and sub.track.codec in BITMAP_SUB_CODECS:
        # Bitmap subs (PGS/VobSub): overlay at source size, then scale.
        pre = vf[:1] if info.interlaced else []
        rest = vf[len(pre):]
        chain = (",".join(pre) + "," if pre else "")
        graph = (f"[0:v:0]{chain}null[v0];"
                 f"[0:s:{sub.track.rel}]scale=w={info.width}:h={info.height}[s0];"
                 f"[v0][s0]overlay=eof_action=pass,{','.join(rest)}[vout]")
        cmd += ["-filter_complex", graph]
        vmap = "[vout]"
    else:
        cmd += ["-vf", ",".join(vf)]
        vmap = "0:v:0"

    cmd += ["-map", vmap]
    if audio:
        cmd += ["-map", f"0:{audio.index}"]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn"]

    if opts.encoder == "nvenc":
        # Experimental. Same PSP limits: Main@3.0, 3 refs, no weighted
        # prediction, no B-frame references.
        cmd += [
            "-c:v", "h264_nvenc", "-preset", NVENC_PRESET[opts.speed], "-tune", "hq",
            "-profile:v", "main", "-level:v", "3.0", "-pix_fmt", "yuv420p",
            "-rc", "vbr", "-cq", str(crf + 4), "-b:v", "0",
            "-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k",
            "-bf", "2", "-b_ref_mode", "disabled",   # Pascal: 1 ref only (PSP max 3)
            "-weighted_pred", "0", "-coder", "cabac", "-spatial_aq", "1",
            "-g", "300", "-forced-idr", "1",
        ]
    else:
        cmd += [
            "-c:v", "libx264", "-preset", SPEED[opts.speed],
            "-profile:v", "main", "-level:v", "3.0", "-pix_fmt", "yuv420p",
            "-crf", str(crf), "-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k",
            "-x264-params",
            "ref=3:bframes=2:b-pyramid=none:weightp=0:weightb=0:8x8dct=0:"
            "keyint=300:min-keyint=25:scenecut=40",
        ]
    cmd += [
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    ]
    if audio:
        af = ["aresample=48000"]
        if opts.normalize:
            af.insert(0, "loudnorm=I=-16:TP=-1.5:LRA=11")
        cmd += ["-c:a", "aac", "-b:a", f"{abr}k", "-ar", "48000", "-ac", "2",
                "-af", ",".join(af)]
    else:
        cmd += ["-an"]

    title = Path(dst).name.split(".mp4")[0]
    cmd += ["-metadata", f"title={title}", "-movflags", "+faststart",
            "-f", "psp", ff_file(dst)]
    if srt_out and srt_track:
        cmd += ["-map", f"0:{srt_track.index}", "-c:s", "srt", "-f", "srt", ff_file(srt_out)]
    return cmd


_nvenc_ok: bool | None = None


def nvenc_available() -> bool:
    """True if ffmpeg can open an NVIDIA H.264 encoder on this machine."""
    global _nvenc_ok
    if _nvenc_ok is None:
        exe = ffmpeg_path()
        try:
            r = subprocess.run(
                [exe or "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=black:s=256x144:d=0.1", "-c:v", "h264_nvenc",
                 "-profile:v", "main", "-f", "null", "-"],
                capture_output=True, timeout=15)
            _nvenc_ok = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _nvenc_ok = False
    return _nvenc_ok


def thumbnail_command(info: MediaInfo, dst: str) -> list[str]:
    """160x120 JPEG the PSP XMB shows next to the video (NAME.THM)."""
    at = max(0.0, min(info.duration * 0.15, 120.0)) if info.duration else 0
    vf = []
    if info.sar != (1, 1):
        vf.append(f"scale=trunc(iw*{info.sar[0]}/{info.sar[1]}/2)*2:ih")
    vf += ["scale=160:120:force_original_aspect_ratio=decrease",
           "pad=160:120:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1",
           "format=yuvj420p"]
    return [ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-loglevel", "error", "-ss", f"{at:.2f}", "-i", ff_file(info.path),
            "-frames:v", "1", "-vf", ",".join(vf), "-q:v", "3",
            "-f", "mjpeg", ff_file(dst)]


def poster_command(path: str, at: float) -> list[str]:
    """Small preview frame for the queue list, written as PNG to stdout."""
    seek = ["-ss", f"{at:.2f}"] if at else []   # seeking loses an MP3's embedded cover
    return [ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin",
            "-loglevel", "error", *seek, "-i", ff_file(path),
            "-frames:v", "1", "-vf", "scale=-2:72", "-f", "image2pipe",
            "-c:v", "png", "pipe:1"]


def safe_name(stem: str) -> str:
    """A filename the PSP can hold: FAT's rules, and only characters Shift-JIS has,
    the PSP's encoding for names (the rest become their plain letter: é -> e,
    ά -> α, – -> -). A name with other characters is unreachable on the PSP. Up to
    150 bytes in Shift-JIS, so the PSP's paths (240 bytes) have room for folders."""
    from .xltlink import psp_name
    name = psp_name(FAT_FORBIDDEN.sub("_", stem).strip(" ."))
    while len(name.encode("cp932")) > 150:
        name = name[:-1].rstrip(" .")
    return name if name and name != "_" else "video"


def unique_path(folder: str, base: str, ext: str) -> str:
    """folder/base.ext, numbered if that name (or its unfinished .part) is taken."""
    cand = Path(folder) / f"{base}{ext}"
    n = 2
    while cand.exists() or cand.with_name(cand.name + ".part").exists():
        cand = Path(folder) / f"{base} {n}{ext}"
        n += 1
    return str(cand)


def output_path(src: str, out_dir: str | None, ext: str = ".mp4") -> str:
    s = Path(src)
    base = safe_name(s.stem)
    if not out_dir:
        base += " (PSP)"
    return unique_path(str(Path(out_dir) if out_dir else s.parent), base, ext)


def estimate_size_mb(info: MediaInfo, opts: Options) -> float:
    """Rough upper estimate (maxrate is a ceiling, CRF usually lands lower)."""
    _, maxrate, abr = QUALITY[opts.quality]
    typical = maxrate * 0.55 + abr
    return info.duration * typical / 8 / 1024 if info.duration else math.nan
