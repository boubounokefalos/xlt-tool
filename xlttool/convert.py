"""Everything besides video: music, pictures, text, subtitles and ZIP archives.

The limits come from XLT's own code (~/xlt/src: audiofmt.c, music.c, flac.c,
imgdecode.c, reader.c, srt.c, zip.c):
  * audio rates: 8, 11.025, 12, 16, 22.05, 24, 32, 44.1 or 48 kHz
  * AAC must be plain AAC-LC; M4A is decoded as stereo
  * pictures: JPEG, PNG or BMP, at most 8192 on a side, interlaced PNG limited
  * the Reader shows the first 2 MB of a text file
  * subtitles: a UTF-8 .srt under 2 MB named after the video
  * ZIP: stored or deflated only, no ZIP64 (4 GB, 65535 files), no passwords
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tarfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from . import media

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus",
    ".wma", ".ape", ".alac", ".aiff", ".aif", ".wv", ".ac3", ".dts", ".mka",
    ".tta", ".mpc", ".amr", ".mp2",
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".heic",
    ".heif", ".avif", ".tga", ".jp2", ".ppm", ".pgm",
}
TEXT_EXTENSIONS = {
    ".txt", ".text", ".md", ".log", ".html", ".htm", ".xhtml", ".epub", ".fb2",
    ".docx", ".odt",
}
SUB_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
LYRICS_EXTENSION = ".lrc"
TAGGABLE = {".mp3", ".m4a", ".flac"}    # what can be edited without converting
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")

# On the Memory Stick each kind goes where XLT's library looks for it.
FOLDERS = {"video": "VIDEO", "audio": "MUSIC", "image": "PICTURE", "text": "BOOKS",
           "sub": "VIDEO", "archive": "DOWNLOADS", "lyrics": "MUSIC", "ps1": "PSP/GAME", "pspgame": "ISO"}
PS1_EXTENSIONS = {".cue", ".bin", ".img"}          # PS1 discs: to an EBOOT.PBP
PSPGAME_EXTENSIONS = {".iso", ".cso", ".zso"}      # PSP games: ISO to CSO, CSO/ZSO to ISO

PSP_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)

# label -> kbit/s
MP3_BITRATE = {"small": 128, "balanced": 192, "high": 320}
AAC_BITRATE = {"small": 96, "balanced": 160, "high": 256}
MUSIC_EXT = {"mp3": ".mp3", "m4a": ".m4a", "flac": ".flac"}
COVER_MAX = 500

# name -> longest sides allowed
PICTURE_SIZES = {"screen": (480, 272), "zoom": (1920, 1920), "full": (8192, 8192)}
PICTURE_EXT = {"jpeg": ".jpg", "png": ".png"}

READER_LIMIT = (2 << 20) - (16 << 10)   # bytes; a little under XLT's 2 MB
SRT_LIMIT = 2 << 20
ZIP_LIMIT = 0xFFFFFFFF
ZIP_MAX_FILES = 65534
ZIP_MAX_LIST = 4 << 20                  # XLT reads the whole list of names into memory
MEMBER_LIMIT = 128 << 20                # never unpack more than this from one document part
ALREADY_SMALL = {".mp4", ".m4v", ".mp3", ".m4a", ".aac", ".flac", ".jpg", ".jpeg", ".png",
                 ".zip", ".cso", ".zso", ".gz", ".xz", ".7z", ".rar", ".pmf", ".ogg", ".webp"}


def kind_of(path: str) -> str:
    """What to do with a file, by name. Anything unknown is tried as a video."""
    name = os.path.basename(path).lower()
    if name.endswith(TAR_SUFFIXES):
        return "archive"
    ext = Path(name).suffix
    if ext in PS1_EXTENSIONS:
        return "ps1"
    if ext in PSPGAME_EXTENSIONS:
        return "pspgame"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in SUB_EXTENSIONS:
        return "sub"
    if ext == LYRICS_EXTENSION:
        return "lyrics"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "video"


def from_folder(paths: list[str]) -> list[str]:
    """Which files of a folder to take. Folders of films hold posters and
    .nfo files, music folders cover.jpg, so only the main kind of media is used."""
    kinds = {p: kind_of(p) for p in paths}
    known = {p: k for p, k in kinds.items()
             if k != "video" or Path(p).suffix.lower() in media.VIDEO_EXTENSIONS}
    # (a game's folder holds a readme and covers: the disc comes before them)
    for wanted in (("video", "audio"), ("ps1", "pspgame"), ("image",), ("text",), ("sub",), ("archive",)):
        hits = [p for p in paths if known.get(p) in wanted]
        if hits:
            return hits
    return []


def _ffprobe(path: str) -> dict:
    exe = media.ffprobe_path()
    if not exe:
        raise RuntimeError("ffprobe was not found")
    res = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", media.ff_file(path)],
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        msg = res.stderr.strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "ffprobe failed")
    return json.loads(res.stdout or "{}")


# -------------------------------------------------------------------- music --

@dataclass
class MusicOptions:
    format: str = "mp3"           # mp3 | m4a | flac
    quality: str = "balanced"     # small | balanced | high
    cover: bool = True


TAG_FIELDS = ("title", "artist", "album", "track", "date", "genre")


@dataclass
class TagEdit:
    """What the user changed for one song; applied after any conversion."""
    tags: dict = field(default_factory=dict)  # field -> new value ("" clears it)
    art: str | None = None        # None keep, "" remove, otherwise a picture file
    lyrics: str | None = None     # None keep, "" remove, otherwise the new text
    embed_lyrics: bool = False    # also store the lyrics inside the music file
    in_place: bool = False        # update the original instead of making a copy

    def changed(self) -> bool:
        return bool(self.tags) or self.art is not None or self.lyrics is not None \
            or self.embed_lyrics or self.in_place


@dataclass
class AudioInfo:
    path: str
    duration: float = 0.0
    codec: str = ""
    profile: str = ""
    rate: int = 0
    channels: int = 0
    bits: int = 0
    stream: int = 0
    cover_stream: int = -1
    stream_tags: bool = False     # tags live on the stream (Ogg, Opus)
    title: str = ""
    artist: str = ""
    album: str = ""
    track: str = ""
    date: str = ""
    genre: str = ""
    lyrics_file: str = ""         # a .lrc with the song's name next to it
    lyrics_embedded: str = ""     # lyrics stored inside the file

    def value(self, name: str, edit: TagEdit | None = None) -> str:
        if edit and name in edit.tags:
            return edit.tags[name]
        return getattr(self, name)

    def summary(self, edit: TagEdit | None = None) -> str:
        parts = []
        title, artist = self.value("title", edit), self.value("artist", edit)
        if artist or title:
            parts.append(" - ".join(x for x in (artist, title) if x))
        parts.append(self.codec.upper())
        if self.rate:
            parts.append(f"{self.rate / 1000:g} kHz")
        if self.duration:
            parts.append(media.format_time(self.duration))
        if self.cover_stream >= 0:
            parts.append("cover")
        return " · ".join(parts)


def probe_audio(path: str) -> AudioInfo:
    data = _ffprobe(path)
    info = AudioInfo(path=path)
    info.duration = media._ratio(data.get("format", {}).get("duration"))
    audio = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio" and audio is None:
            audio = s
        elif (s.get("codec_type") == "video" and info.cover_stream < 0
              and s.get("disposition", {}).get("attached_pic")):
            info.cover_stream = int(s["index"])
    if audio is None:
        raise RuntimeError("no sound in this file")
    info.stream = int(audio["index"])
    info.codec = audio.get("codec_name", "")
    info.profile = audio.get("profile", "")
    info.rate = int(audio.get("sample_rate") or 0)
    info.channels = int(audio.get("channels") or 0)
    info.bits = int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0)
    if not info.duration:
        info.duration = media._ratio(audio.get("duration"))
    file_tags = {k.lower(): v for k, v in (data.get("format", {}).get("tags") or {}).items()}
    stream_tags = {k.lower(): v for k, v in (audio.get("tags") or {}).items()}
    tags = {**stream_tags, **file_tags}
    info.stream_tags = "title" not in file_tags and "artist" not in file_tags \
        and ("title" in stream_tags or "artist" in stream_tags)
    info.title, info.artist = tags.get("title", ""), tags.get("artist", "")
    info.album, info.genre = tags.get("album", ""), tags.get("genre", "")
    info.track = tags.get("track", "")
    info.date = tags.get("date", tags.get("year", ""))
    info.lyrics_embedded = next((v for k, v in tags.items() if k.startswith("lyrics")), "")
    lrc = os.path.splitext(path)[0] + LYRICS_EXTENSION
    info.lyrics_file = lrc if os.path.isfile(lrc) else ""
    return info


def target_rate(rate: int) -> int:
    """The nearest rate XLT and the PSP's audio hardware accept."""
    if rate in PSP_RATES:
        return rate
    if rate and rate % 11025 == 0:
        return 44100
    return 48000 if rate > 44100 else next((r for r in PSP_RATES if r >= rate), 44100)


def music_can_copy(info: AudioInfo, opts: MusicOptions) -> bool:
    """True when the sound is already right, so re-encoding would only lose quality."""
    if info.rate not in PSP_RATES:
        return False
    if opts.format == "mp3":
        return info.codec == "mp3" and info.channels <= 2
    if opts.format == "m4a":
        return info.codec == "aac" and info.profile == "LC" and info.channels == 2
    return info.codec == "flac" and info.channels <= 2 and 0 < info.bits <= 16


def music_command(info: AudioInfo, dst: str, opts: MusicOptions) -> list[str]:
    cmd = [media.ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", "-i", media.ff_file(info.path),
           "-map", f"0:{info.stream}"]
    cover = opts.cover and info.cover_stream >= 0
    if cover:
        cmd += ["-map", f"0:{info.cover_stream}"]
    cmd += ["-map_metadata", "0:s:a:0" if info.stream_tags else "0", "-map_chapters", "-1"]
    if music_can_copy(info, opts):
        cmd += ["-c:a", "copy"]
    else:
        if opts.format == "mp3":
            cmd += ["-c:a", "libmp3lame", "-b:a", f"{MP3_BITRATE[opts.quality]}k"]
        elif opts.format == "m4a":
            cmd += ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", f"{AAC_BITRATE[opts.quality]}k"]
        else:
            cmd += ["-c:a", "flac", "-sample_fmt", "s16", "-compression_level", "8"]
        cmd += ["-ar", str(target_rate(info.rate))]
        if opts.format == "m4a" or info.channels > 2:
            cmd += ["-ac", "2"]                 # XLT's AAC path is stereo only
    if cover:
        cmd += ["-c:v", "mjpeg", "-q:v", "3", "-vf",
                f"scale='min({COVER_MAX},iw)':-2,format=yuvj420p",
                "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-vn"]
    if opts.format == "mp3":
        cmd += ["-id3v2_version", "3", "-write_id3v1", "0", "-f", "mp3"]
    elif opts.format == "m4a":
        cmd += ["-movflags", "+faststart", "-f", "ipod"]
    else:
        cmd += ["-f", "flac"]
    return cmd + [media.ff_file(dst)]


def music_estimate_mb(info: AudioInfo, opts: MusicOptions) -> float:
    kbps = {"mp3": MP3_BITRATE, "m4a": AAC_BITRATE}.get(opts.format, {}).get(opts.quality, 800)
    return info.duration * kbps / 8 / 1024 if info.duration else math.nan


def read_text_file(path: str) -> str:
    with open(path, "rb") as f:
        return media.decode_subtitle(f.read())[0]


def lyrics_text(info: AudioInfo, edit: TagEdit) -> str:
    """The song's lyrics as things stand: edited, else its .lrc, else the ones inside it."""
    if edit.lyrics is not None:
        return edit.lyrics
    if info.lyrics_file:
        return read_text_file(info.lyrics_file)
    return info.lyrics_embedded


def write_lyrics(info: AudioInfo, edit: TagEdit, audio_path: str) -> None:
    """XLT shows a .lrc named after the song: write it next to audio_path, as UTF-8."""
    target = os.path.splitext(audio_path)[0] + LYRICS_EXTENSION
    if edit.lyrics == "":
        if edit.in_place:
            _remove(target)
        return
    text = edit.lyrics if edit.lyrics is not None else \
        (read_text_file(info.lyrics_file) if info.lyrics_file else "")
    if text and not (edit.in_place and edit.lyrics is None):
        _write_text(target, text)


def needs_tagging(info: AudioInfo, edit: TagEdit) -> bool:
    return edit.changed() or bool(info.lyrics_embedded)


def _prepared_art(path: str) -> bytes:
    """Any picture as the baseline JPEG that covers are stored as."""
    res = subprocess.run([media.ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", media.ff_file(path), "-frames:v", "1", "-vf",
                          f"scale='min({COVER_MAX},iw)':-2,format=yuvj420p", "-c:v", "mjpeg", "-q:v", "3",
                          "-f", "image2pipe", "pipe:1"], capture_output=True, timeout=60)
    if res.returncode != 0 or not res.stdout:
        lines = res.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(lines[0] if lines else "couldn't read that picture")
    return res.stdout


def preview_command(path: str, size: int, stream: int = -1) -> list[str]:
    """A picture or a song's cover as a PNG on stdout, for the editor."""
    cmd = [media.ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", media.ff_file(path)]
    if stream >= 0:
        cmd += ["-map", f"0:{stream}"]
    return cmd + ["-frames:v", "1", "-vf",
                  f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                  "-f", "image2pipe", "-c:v", "png", "pipe:1"]


def _embedded_after(info: AudioInfo, edit: TagEdit) -> str:
    """The lyrics the finished file should carry inside it; "" for none."""
    if edit.lyrics is not None:
        return edit.lyrics if edit.embed_lyrics else ""
    if edit.embed_lyrics:
        return lyrics_text(info, edit)
    return info.lyrics_embedded


def _tag_id3(path: str, info: AudioInfo, edit: TagEdit, art: bytes | None) -> None:
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TRCK, USLT
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    frames = {"title": ("TIT2", TIT2), "artist": ("TPE1", TPE1), "album": ("TALB", TALB),
              "track": ("TRCK", TRCK), "date": ("TDRC", TDRC), "genre": ("TCON", TCON)}
    for name, value in edit.tags.items():
        frame_id, cls = frames[name]
        tags.delall(frame_id)
        if value:
            tags.add(cls(encoding=1, text=[value]))     # UTF-16: valid in ID3v2.3
    if edit.art is not None:
        tags.delall("APIC")
    if art:
        tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="", data=art))
    tags.delall("USLT")
    tags.delall("TXXX:lyrics-eng")              # where ffmpeg would have left them
    lyrics = _embedded_after(info, edit)
    if lyrics:
        tags.add(USLT(encoding=1, lang="eng", desc="", text=lyrics))
    tags.update_to_v23()
    tags.save(path, v2_version=3, v1=0)


def _tag_m4a(path: str, info: AudioInfo, edit: TagEdit, art: bytes | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    keys = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "date": "\xa9day", "genre": "\xa9gen"}
    for name, value in edit.tags.items():
        if name == "track":
            audio.tags.pop("trkn", None)
            numbers = re.findall(r"\d+", value)
            if numbers:
                audio.tags["trkn"] = [(int(numbers[0]), int(numbers[1]) if len(numbers) > 1 else 0)]
            continue
        audio.tags.pop(keys[name], None)
        if value:
            audio.tags[keys[name]] = [value]
    if edit.art is not None:
        audio.tags.pop("covr", None)
    if art:
        audio.tags["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.tags.pop("\xa9lyr", None)
    lyrics = _embedded_after(info, edit)
    if lyrics:
        audio.tags["\xa9lyr"] = [lyrics]
    audio.save()


def _tag_flac(path: str, info: AudioInfo, edit: TagEdit, art: bytes | None) -> None:
    from mutagen.flac import FLAC, Picture
    audio = FLAC(path)
    keys = {"track": "tracknumber"}
    for name, value in edit.tags.items():
        key = keys.get(name, name)
        audio.pop(key, None)
        if value:
            audio[key] = [value]
    if edit.art is not None:
        audio.clear_pictures()
    if art:
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", art
        audio.add_picture(pic)
    for key in ("lyrics", "unsyncedlyrics"):
        audio.pop(key, None)
    lyrics = _embedded_after(info, edit)
    if lyrics:
        audio["lyrics"] = [lyrics]
    audio.save()


def apply_tags(path: str, ext: str, info: AudioInfo, edit: TagEdit) -> None:
    """Writes the edits into a finished .mp3/.m4a/.flac (ext says which it is)."""
    art = _prepared_art(edit.art) if edit.art else None
    {".mp3": _tag_id3, ".m4a": _tag_m4a, ".flac": _tag_flac}[ext.lower()](path, info, edit, art)


def save_in_place(info: AudioInfo, edit: TagEdit, progress, cancel: threading.Event) -> list[str]:
    """Updates the original file. Works on a copy and swaps it in only once the
    copy has been checked, so a failure can't damage the song."""
    from mutagen import File as MutagenFile
    path = info.path
    ext = Path(path).suffix.lower()
    if ext not in TAGGABLE:
        raise ValueError("only MP3, M4A and FLAC files can be updated directly")
    tmp = path + ".part"
    try:
        shutil.copy2(path, tmp)
        progress(0.5)
        if cancel.is_set():
            raise InterruptedError
        apply_tags(tmp, ext, info, edit)
        before, after = MutagenFile(path), MutagenFile(tmp)
        if after is None or abs(after.info.length - before.info.length) > 1.0:
            raise ValueError("the updated file didn't check out, so the original was left alone")
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        _remove(tmp)
        raise
    write_lyrics(info, edit, path)
    progress(1.0)
    return [path]


def convert_lyrics(src: str, out_dir: str | None, progress, cancel: threading.Event) -> list[str]:
    """A .lrc on its own: UTF-8 is what XLT reads most reliably."""
    text = read_text_file(src)
    if cancel.is_set():
        raise InterruptedError
    stem = media.safe_name(Path(src).stem)
    path = media.unique_path(out_dir or os.path.dirname(src), stem if out_dir else stem + " (PSP)", LYRICS_EXTENSION)
    _write_text(path, text)
    progress(1.0)
    return [path]


# ----------------------------------------------------------------- pictures --

@dataclass
class PictureOptions:
    format: str = "jpeg"          # jpeg | png
    size: str = "zoom"            # screen | zoom | full


@dataclass
class ImageInfo:
    path: str
    width: int = 0
    height: int = 0
    codec: str = ""
    alpha: bool = False
    duration: float = 0.0         # always 0: pictures have no timeline

    def summary(self) -> str:
        return " · ".join(x for x in (f"{self.width}×{self.height}", self.codec.upper()) if x)


def probe_image(path: str) -> ImageInfo:
    data = _ffprobe(path)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            fmt = s.get("pix_fmt", "")
            return ImageInfo(path, int(s.get("width") or 0), int(s.get("height") or 0),
                             s.get("codec_name", ""),
                             bool(re.search(r"rgba|bgra|argb|abgr|^ya|yuva|gbrap|pal8", fmt)))
    raise RuntimeError("this isn't a picture ffmpeg can read")


def picture_command(info: ImageInfo, dst: str, opts: PictureOptions) -> list[str]:
    w, h = PICTURE_SIZES[opts.size]
    scale = (f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease"
             ":flags=lanczos")
    cmd = [media.ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", "-i", media.ff_file(info.path)]
    if opts.format == "png":
        cmd += ["-vf", f"{scale},format={'rgba' if info.alpha else 'rgb24'}",
                "-frames:v", "1", "-c:v", "png", "-f", "image2", "-update", "1"]
    else:
        if info.alpha:                          # transparent areas become white, not black
            graph = (f"[0:v:0]{scale},format=rgba,split[a][b];"
                     "[a]drawbox=c=white:t=fill[bg];"
                     "[bg][b]overlay=format=rgb,format=yuvj420p[out]")
            cmd += ["-filter_complex", graph, "-map", "[out]"]
        else:
            cmd += ["-vf", f"{scale},format=yuvj420p"]
        cmd += ["-frames:v", "1", "-c:v", "mjpeg", "-q:v", "2", "-f", "image2", "-update", "1"]
    return cmd + [media.ff_file(dst)]


def picture_estimate_mb(info: ImageInfo, opts: PictureOptions) -> float:
    w, h = PICTURE_SIZES[opts.size]
    k = min(1.0, w / max(info.width, 1), h / max(info.height, 1))
    pixels = info.width * k * info.height * k
    return pixels * (0.35 if opts.format == "jpeg" else 2.0) / (1 << 20)


# --------------------------------------------------------------------- text --

@dataclass
class TextInfo:
    path: str
    size: int = 0
    duration: float = 0.0

    def summary(self) -> str:
        from .devices import human_size
        return f"{Path(self.path).suffix[1:].upper()} · {human_size(self.size)}"


def probe_text(path: str) -> TextInfo:
    return TextInfo(path, os.path.getsize(path))


BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
              "ul", "ol", "table", "tr", "pre", "title", "header", "footer", "aside"}
INVISIBLE = "­​‌‍﻿"


def tidy(text: str) -> str:
    """One blank line between paragraphs, no stray spaces or invisible characters."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = text.translate({ord(c): None for c in INVISIBLE})
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


class _HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "template"):
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in BLOCK_TAGS:
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "template"):
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS:
            self.out.append("\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    p = _HtmlText()
    p.feed(html)
    p.close()
    return "".join(p.out)


def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    if z.getinfo(name).file_size > MEMBER_LIMIT:
        raise ValueError("a part of this document is too large")
    return z.read(name)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def epub_to_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        container = ET.fromstring(_read_member(z, "META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile")
        opf = ET.fromstring(_read_member(z, opf_path))
        base = PurePosixPath(opf_path).parent
        manifest, spine, title = {}, [], ""
        for e in opf.iter():
            tag = _local(e.tag)
            if tag == "item":
                manifest[e.get("id")] = e.get("href", "")
            elif tag == "itemref":
                spine.append(e.get("idref"))
            elif tag == "title" and not title:
                title = (e.text or "").strip()
        parts = [title] if title else []
        for ref in spine:
            href = manifest.get(ref, "").split("#")[0].replace("%20", " ")
            member = str(base / href) if str(base) != "." else href
            if member in names:
                data = _read_member(z, member)
                parts.append(html_to_text(media.decode_subtitle(data)[0]))
    return "\n\n".join(parts)


def fb2_to_text(path: str) -> str:
    with open(path, "rb") as f:
        root = ET.fromstring(f.read())
    out: list[str] = []

    def walk(el):
        tag = _local(el.tag)
        if tag == "binary":
            return
        if tag in ("p", "subtitle", "v", "text-author"):
            out.append("".join(el.itertext()).strip() + "\n\n")
            return
        if tag == "empty-line":
            out.append("\n")
            return
        for child in el:
            walk(child)
        if tag == "title":
            out.append("\n")

    for body in root:
        if _local(body.tag) == "body":
            walk(body)
    return "".join(out)


def docx_to_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(_read_member(z, "word/document.xml"))
    out: list[str] = []
    for para in root.iter():
        if _local(para.tag) != "p":
            continue
        line: list[str] = []
        for e in para.iter():
            tag = _local(e.tag)
            if tag == "t":
                line.append(e.text or "")
            elif tag == "tab":
                line.append(" ")
            elif tag in ("br", "cr"):
                line.append("\n")
        out.append("".join(line) + "\n\n")
    return "".join(out)


def odt_to_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(_read_member(z, "content.xml"))

    def flat(el) -> str:
        bits = [el.text or ""]
        for c in el:
            tag = _local(c.tag)
            if tag == "s":
                bits.append(" ")
            elif tag == "tab":
                bits.append(" ")
            elif tag == "line-break":
                bits.append("\n")
            else:
                bits.append(flat(c))
            bits.append(c.tail or "")
        return "".join(bits)

    out = [flat(e) + "\n\n" for e in root.iter() if _local(e.tag) in ("p", "h")]
    return "".join(out)


def extract_text(path: str) -> tuple[str, str]:
    """The readable text of a document, and a note on where it came from."""
    ext = Path(path).suffix.lower()
    if ext == ".epub":
        return tidy(epub_to_text(path)), "EPUB"
    if ext == ".fb2":
        return tidy(fb2_to_text(path)), "FB2"
    if ext == ".docx":
        return tidy(docx_to_text(path)), "DOCX"
    if ext == ".odt":
        return tidy(odt_to_text(path)), "ODT"
    with open(path, "rb") as f:
        text, enc = media.decode_subtitle(f.read())
    if ext in (".html", ".htm", ".xhtml"):
        return tidy(html_to_text(text)), "HTML"
    return tidy(text), enc


def split_text(text: str, limit: int = READER_LIMIT) -> list[str]:
    """Pieces that each fit XLT's Reader, cut between paragraphs where possible."""
    data_len = len(text.encode("utf-8"))
    if data_len <= limit:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for para in re.split(r"(?<=\n\n)", text):
        n = len(para.encode("utf-8"))
        if n > limit:                           # one enormous paragraph: cut it at spaces
            if current:
                pieces.append("".join(current))
                current, size = [], 0
            pieces.extend(_cut_long(para, limit))
            continue
        if size + n > limit:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(para)
        size += n
    if current:
        pieces.append("".join(current))
    return pieces


def _cut_long(text: str, limit: int) -> list[str]:
    out = []
    while len(text.encode("utf-8")) > limit:
        # characters are at most 4 bytes, so this many always fit
        cut = limit // 4
        while len(text[:cut * 2].encode("utf-8")) <= limit and cut * 2 < len(text):
            cut *= 2
        space = text.rfind(" ", 0, cut)
        if space > 0:
            cut = space + 1
        out.append(text[:cut])
        text = text[cut:]
    if text:
        out.append(text)
    return out


def _write_text(path: str, text: str) -> None:
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def convert_text(src: str, out_dir: str | None, progress, cancel: threading.Event) -> list[str]:
    text, _ = extract_text(src)
    if not text.strip():
        raise ValueError("no text found in this file")
    pieces = split_text(text)
    folder = out_dir or os.path.dirname(src)
    stem = media.safe_name(Path(src).stem)
    if not out_dir:
        stem += " (PSP)"
    written = []
    for i, piece in enumerate(pieces, 1):
        if cancel.is_set():
            raise InterruptedError
        name = f"{stem} ({i} of {len(pieces)})" if len(pieces) > 1 else stem
        path = media.unique_path(folder, name, ".txt")
        _write_text(path, piece)
        written.append(path)
        progress(i / len(pieces))
    return written


# ---------------------------------------------------------------- subtitles --

def srt_from_text_file(utf8_src: str, dst: str) -> None:
    """dst: an .srt from a UTF-8 .srt/.ass/.vtt."""
    if Path(utf8_src).suffix.lower() == ".srt":
        with open(utf8_src, encoding="utf-8", newline="") as fi, \
                open(dst, "w", encoding="utf-8", newline="") as fo:
            fo.write(fi.read())
        return
    res = subprocess.run([media.ffmpeg_path() or "ffmpeg", "-hide_banner", "-nostdin", "-y",
                          "-loglevel", "error", "-i", media.ff_file(utf8_src), "-map", "0:s:0",
                          "-c:s", "srt", "-f", "srt", media.ff_file(dst)],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines()
        raise ValueError(lines[0] if lines else "couldn't convert these subtitles")


def convert_subtitle(src: str, out_dir: str | None, tmp_dir: str, progress,
                     cancel: threading.Event) -> list[str]:
    source = os.path.join(tmp_dir, "in" + Path(src).suffix.lower())
    media.subtitle_to_utf8(src, source)
    srt = os.path.join(tmp_dir, "out.srt")
    srt_from_text_file(source, srt)
    if os.path.getsize(srt) >= SRT_LIMIT:
        raise ValueError("over 2 MB: XLT won't load subtitles this long")
    if cancel.is_set():
        raise InterruptedError
    with open(srt, encoding="utf-8") as f:
        text = f.read()
    stem = media.safe_name(Path(src).stem)
    path = media.unique_path(out_dir or os.path.dirname(src),
                             stem if out_dir else stem + " (PSP)", ".srt")
    _write_text(path, text)
    progress(1.0)
    return [path]


# ----------------------------------------------------------------- archives --

@dataclass
class ArchiveInfo:
    path: str
    files: int = 0
    size: int = 0
    duration: float = 0.0

    def summary(self) -> str:
        from .devices import human_size
        return f"{self.files} files · {human_size(self.size)}"


def _zip_junk(rel: str) -> bool:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    return low.startswith("__macosx/") or name in (".ds_store", "thumbs.db", "desktop.ini")


def zip_name(rel: str) -> str:
    """A path inside the ZIP that XLT will accept and FAT can hold."""
    parts = [media.safe_name(p) for p in rel.replace("\\", "/").split("/")
             if p not in ("", ".", "..")]
    name = "/".join(parts)
    while len(name.encode("utf-8")) > 240 and "/" in name:
        name = name.split("/", 1)[1]
    return name


def _folder_files(root: str) -> list[tuple[str, str]]:
    out = []
    for base, dirs, names in os.walk(root):
        dirs.sort()
        for n in sorted(names):
            full = os.path.join(base, n)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.isfile(full) and not os.path.islink(full) and not _zip_junk(rel):
                out.append((full, rel))
    return out


def _check_fits(files: int, size: int, names_bytes: int = 0) -> None:
    if 46 * files + names_bytes > ZIP_MAX_LIST:
        raise ValueError("too many files, or names too long, for XLT's ZIP reader")
    if files == 0:
        raise ValueError("there are no files to pack")
    if files > ZIP_MAX_FILES:
        raise ValueError(f"{files} files is too many for XLT (the limit is {ZIP_MAX_FILES})")
    if size >= ZIP_LIMIT - (64 << 20):
        raise ValueError("over 4 GB: XLT can't open a ZIP that large")


def probe_archive(path: str) -> ArchiveInfo:
    if os.path.isdir(path):
        files = _folder_files(path)
        info = ArchiveInfo(path, len(files), sum(os.path.getsize(f) for f, _ in files))
        names = sum(len(rel.encode("utf-8")) for _, rel in files)
    else:
        info, names = ArchiveInfo(path), 0
        with tarfile.open(path, "r:*") as tar:
            for m in tar:
                if m.isfile() and not _zip_junk(m.name):
                    info.files += 1
                    info.size += m.size
                    names += len(m.name.encode("utf-8"))
    _check_fits(info.files, info.size, names)
    return info


def _write_member(z: zipfile.ZipFile, name: str, stream, size: int, used: set[str], step,
                  cancel: threading.Event) -> None:
    name = zip_name(name)
    if not name:
        return
    stem, ext = os.path.splitext(name)
    n, unique = 2, name
    while unique.lower() in used:               # FAT doesn't tell a.txt from A.TXT
        unique = f"{stem} ({n}){ext}"
        n += 1
    used.add(unique.lower())
    info = zipfile.ZipInfo(unique, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED if ext.lower() in ALREADY_SMALL else zipfile.ZIP_DEFLATED
    info.file_size = size
    with z.open(info, "w", force_zip64=False) as out:
        while True:
            if cancel.is_set():
                raise InterruptedError
            buf = stream.read(1 << 20)
            if not buf:
                break
            out.write(buf)
            step(len(buf))


def make_zip(src: str, info: ArchiveInfo, out_dir: str | None, progress,
             cancel: threading.Event) -> list[str]:
    base = media.safe_name(_archive_stem(src))
    path = media.unique_path(out_dir or os.path.dirname(os.path.abspath(src)),
                             base if out_dir else base + " (PSP)", ".zip")
    tmp = path + ".part"
    done = [0]

    def step(n: int):
        done[0] += n
        progress(min(1.0, done[0] / max(info.size, 1)))

    used: set[str] = set()
    try:
        with open(tmp, "wb") as f:
            with zipfile.ZipFile(f, "w", allowZip64=False, compresslevel=6) as z:
                if os.path.isdir(src):
                    for full, rel in _folder_files(src):
                        with open(full, "rb") as fi:
                            _write_member(z, rel, fi, os.path.getsize(full), used, step, cancel)
                else:
                    with tarfile.open(src, "r:*") as tar:
                        for m in tar:           # only read: nothing is unpacked to disk
                            if m.isfile() and not _zip_junk(m.name):
                                _write_member(z, m.name, tar.extractfile(m), m.size, used, step, cancel)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except zipfile.LargeZipFile:
        _remove(tmp)
        raise ValueError("over 4 GB: XLT can't open a ZIP that large")
    except BaseException:
        _remove(tmp)
        raise
    return [path]


def _archive_stem(src: str) -> str:
    name = os.path.basename(os.path.abspath(src))
    if os.path.isdir(src):
        return name
    low = name.lower()
    for suffix in TAR_SUFFIXES:
        if low.endswith(suffix):
            return name[:-len(suffix)]
    return Path(name).stem


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ games --

@dataclass
class GameJobInfo:
    """A PS1 disc to make an EBOOT of, or a PSP game image to compress or unpack."""
    path: str
    title: str = ""
    game_id: str = ""
    size: int = 0
    image: str = ""          # PS1: the data track's file
    audio_tracks: int = 0
    target: str = ""         # PSP game: "cso" or "iso"
    duration: float = 0.0
    discs: list = field(default_factory=list)  # PS1, several discs: [(path, image, size, game ID)]
    icon0: str = ""          # PS1: a PNG to use as the icon (the same game's, from the library)
    pic1: str = ""           # PS1: a PNG for the background
    covers: bool = False     # PS1: box art may be downloaded for the icon (the user turned it on)

    def summary(self) -> str:
        from .devices import human_size
        bits = [b for b in (self.title, self.game_id, f"{len(self.discs)} discs" if self.discs else "",
                            human_size(self.size)) if b]
        if self.target:
            bits.append(f"to {self.target.upper()}")
        if self.audio_tracks:
            from . import psx
            bits.append(f"with its CD music ({self.audio_tracks} tracks)" if psx.atracdenc()
                        else f"without its CD music ({self.audio_tracks} tracks: atracdenc isn't there)")
        return " · ".join(bits)


def ps1_sources(files: list[str]) -> list[str]:
    """Of a list of files, the PS1 discs to add: a .cue stands for the images it names
    (and those are left out); a .bin or .img without a cue stands for itself."""
    from . import psx
    named = set()
    for f in files:
        if f.lower().endswith(".cue"):
            try:
                named |= {os.path.normcase(os.path.abspath(t["file"])) for t in psx.parse_cue(f) if t["file"]}
            except OSError:
                pass
    return [f for f in files if not (kind_of(f) == "ps1" and not f.lower().endswith(".cue")
                                     and os.path.normcase(os.path.abspath(f)) in named)]


_DISC_RE = re.compile(r"\s*[\(\[]\s*dis[ck]\s*(\d+)\s*(?:of\s*\d+\s*)?[\)\]]", re.I)


def disc_number(path: str) -> int:
    """The N of "(Disc N)" in a file's name, 0 if none."""
    m = _DISC_RE.search(Path(path).stem)
    return int(m.group(1)) if m else 0


def group_discs(files: list[str]) -> list[list[str]]:
    """PS1 discs of one game (named "... (Disc 1)", "(Disc 2)" ...) together, in order; every
    other file on its own. Order of the first appearance is kept."""
    groups, by_key = [], {}
    for f in files:
        n = disc_number(f) if kind_of(f) == "ps1" else 0
        if not n:
            groups.append([f])
            continue
        from . import psx
        key = psx.clean_title(_DISC_RE.sub("", Path(f).stem)).lower()
        if key in by_key:
            by_key[key].append(f)
        else:
            by_key[key] = [f]
            groups.append(by_key[key])
    return [sorted(g, key=disc_number) if len(g) > 1 else g for g in groups]


def probe_ps1(path: str, discs: list | None = None) -> GameJobInfo:
    from . import psx
    parts = []
    for p in (discs or [path]):
        image = psx.data_track(p)
        size = psx.data_size(p)
        if size % psx.SECTOR:
            raise RuntimeError(f"not a PS1 disc image (raw 2352-byte sectors): {os.path.basename(p)}")
        gid = psx.disc_id(image)
        if not gid:
            raise RuntimeError(f"no PS1 game ID on this disc (SYSTEM.CNF): {os.path.basename(p)}")
        parts.append((p, image, size, gid))
    if len(parts) > 5:
        raise RuntimeError("a PS1 EBOOT holds up to 5 discs")
    title = psx.clean_title(_DISC_RE.sub("", Path(path).stem))
    p, image, size, gid = parts[0]
    info = GameJobInfo(path, title, gid, sum(x[2] for x in parts), image,
                       sum(psx.audio_tracks(x[0]) for x in parts))
    if len(parts) > 1:
        info.discs = parts
    return info


def probe_pspgame(path: str) -> GameJobInfo:
    from . import games
    g = games.read_disc(path, os.path.join(games.cache_dir(), "games"))
    if not g:
        raise RuntimeError("not a PSP game image")
    return GameJobInfo(path, g.title, g.game_id, g.size, target="iso" if g.kind in ("cso", "zso") else "cso")


ICON_W, ICON_H = 144, 80        # what the XMB and XLT show (PS1 Classics' own are 80x80; both work)
COVER_NAMES = ("cover", "folder", "front", "icon0", "icon")


def _png(img, w: int, h: int) -> bytes:
    """A QImage fitted into w x h (transparent edges), as PNG bytes."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    canvas = QImage(w, h, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    scaled = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    p = QPainter(canvas)
    p.drawImage((w - scaled.width()) // 2, (h - scaled.height()) // 2, scaled)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    canvas.save(buf, "PNG")
    return bytes(data)


def _title_icon(title: str) -> bytes:
    """An icon drawn with the game's title, for a disc with no picture anywhere."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
    img = QImage(ICON_W, ICON_H, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, 0, ICON_H)
    g.setColorAt(0, QColor(54, 58, 72))
    g.setColorAt(1, QColor(24, 26, 34))
    p.fillRect(img.rect(), g)
    p.setPen(QColor(150, 156, 176))
    f = QFont()
    f.setPixelSize(10)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRect(6, 4, ICON_W - 12, 14), Qt.AlignmentFlag.AlignLeft, "PS1")
    p.setPen(QColor(240, 240, 245))
    for size in (18, 16, 14, 12, 11):  # the biggest that fits
        f.setPixelSize(size)
        p.setFont(f)
        r = p.boundingRect(QRect(6, 18, ICON_W - 12, ICON_H - 22), Qt.TextFlag.TextWordWrap, title)
        if r.height() <= ICON_H - 22:
            break
    p.drawText(QRect(6, 18, ICON_W - 12, ICON_H - 22),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, title)
    p.end()
    return _png(img, ICON_W, ICON_H)


def ps1_pictures(info: GameJobInfo) -> tuple[bytes, bytes | None]:
    """ICON0 and PIC1 for a PS1 EBOOT: the same game's from the library (info.icon0/pic1),
    else a picture beside the disc (cover.jpg, the game's name.png...), else the title drawn."""
    from PySide6.QtGui import QImage

    def read(p: str) -> bytes | None:
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            return None
    icon0 = read(info.icon0) if info.icon0 else None
    pic1 = read(info.pic1) if info.pic1 else None
    if not icon0 and info.game_id:  # box art, if it was downloaded (or may be now)
        from . import covers, games
        g = games.Game(path=info.path, kind="ps1", title=info.title, game_id=info.game_id)
        art = covers.cached(g)
        if not art and info.covers:
            try:
                art = covers.fetch(g)
            except Exception:  # noqa: BLE001 - offline: the next way
                art = ""
        img = QImage(art) if art else QImage()
        if not img.isNull():
            icon0 = _png(img, ICON_W, ICON_H)
    if not icon0:
        folder, stem = os.path.dirname(os.path.abspath(info.path)), Path(info.path).stem.lower()
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        for n in names:
            base, ext = os.path.splitext(n.lower())
            if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp") and (base in COVER_NAMES or base == stem):
                img = QImage(os.path.join(folder, n))
                if not img.isNull():
                    icon0 = _png(img, ICON_W, ICON_H)
                    break
    return icon0 or _title_icon(info.title), pic1


def convert_game(info: GameJobInfo, kind: str, out_dir: str | None, progress, cancel) -> list[str]:
    """Runs a game job: PS1 disc -> <ID>/EBOOT.PBP, ISO -> CSO, CSO/ZSO -> ISO."""
    from . import cso, psx
    where = out_dir or os.path.dirname(os.path.abspath(info.path))
    stop = cancel.is_set
    if kind == "ps1":
        folder = os.path.join(where, info.game_id)
        out = os.path.join(folder, "EBOOT.PBP")
        if os.path.exists(out):
            raise RuntimeError(f"{info.game_id}/EBOOT.PBP is there already")
        os.makedirs(folder, exist_ok=True)
        icon0, pic1 = ps1_pictures(info)
        discs = []
        for p, image, size, gid in (info.discs or [(info.path, info.image, info.size, info.game_id)]):
            d = psx.Disc(image, gid, size)
            if p.lower().endswith(".cue"):
                layout = psx.disc_layout(p)
                if any(t["mode"] == "AUDIO" for t in layout) and psx.atracdenc():
                    d.toc = psx.cue_toc(layout)
                    d.music = psx.encode_music(p, cancel=stop)
            discs.append(d)
        try:
            psx.make_eboot(discs, out, info.title, icon0=icon0, pic1=pic1, progress=progress, cancel=stop)
        except BaseException:
            try:
                os.rmdir(folder)
            except OSError:
                pass
            raise
        return [out]
    ext = ".cso" if info.target == "cso" else ".iso"
    out = os.path.join(where, Path(info.path).stem + ext)
    if os.path.exists(out):
        raise RuntimeError(f"{os.path.basename(out)} is there already")
    if info.target == "cso":
        cso.iso_to_cso(info.path, out, progress=progress, cancel=stop)
    else:
        cso.cso_to_iso(info.path, out, progress=progress, cancel=stop)
    return [out]
