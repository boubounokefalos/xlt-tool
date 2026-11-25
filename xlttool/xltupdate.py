"""Updating XLT on the PSP over USB sync. No Qt here.

XLT updates itself over Wi-Fi from GitHub (its src/update.c); this is the same update
without the PSP's Wi-Fi. XLT Tool gets the new XLT -- GitHub's latest XLT release, or an
EBOOT.PBP (or a release zip) chosen on this computer -- checks it, and leaves it in XLT's
folder:

    <XLT>/UPDATE/RELEASE.TXT   its version on the first line, then the release notes
    <XLT>/UPDATE/XLT.PBP       the new EBOOT.PBP, sent last

XLT sees XLT.PBP arrive (sync only shows a file once it is complete and its CRC matches)
and offers it in Settings > About; installing keeps the running XLT as EBOOT-<version>.PBP
and updates ARK's start-up copy, as the Wi-Fi update does. XLT says its running version in
<XLT>/cache/version.txt ("1.3.1", and "next 1.4.0" once one is installed).

The repository can be set with XLTTOOL_XLT_REPO (owner/name) and the API's address with
XLTTOOL_UPDATE_API (as update.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import zipfile
from dataclasses import dataclass

from . import update

REPO = "boubounokefalos/xlt"
XLT_DIRS = ("ms0:/PSP/GAME/XLT", "ef0:/PSP/GAME/XLT")
MIN_EBOOT = 256 * 1024           # anything smaller is not XLT (as src/update.c)
NOTES_MAX = 3000                 # XLT keeps 3 KB of notes
_VERSION_RE = re.compile(rb"XLT (\d+(?:\.\d+){1,3}(?:-[\w.]+)?) started as")


@dataclass
class XltRelease:
    version: str
    tag: str
    notes: str
    page: str
    zip_name: str = ""
    url: str = ""
    size: int = -1
    sha256: str = ""


@dataclass
class PspXlt:
    folder: str          # "ms0:/PSP/GAME/XLT"
    running: str         # "1.3.1", "" for an XLT older than this feature
    next: str = ""       # installed, runs from XLT's next start


def eboot_rank(name: str) -> int:
    """As XLT's upd_eboot_rank: 2 for PSP/GAME/XLT/EBOOT.PBP, 1 for any other EBOOT.PBP."""
    n = name.replace("\\", "/").lower()
    if n == "psp/game/xlt/eboot.pbp" or n.endswith("/psp/game/xlt/eboot.pbp"):
        return 2
    return 1 if n == "eboot.pbp" or n.endswith("/eboot.pbp") else 0


def parse_release(data: dict) -> XltRelease | None:
    tag = str(data.get("tag_name") or "")
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    r = XltRelease(version=tag.lstrip("vV"), tag=tag, notes=str(data.get("body") or ""),
                   page=str(data.get("html_url") or ""))
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        if name.lower().endswith(".zip"):
            r.zip_name, r.url, r.size = name, str(a.get("browser_download_url") or ""), int(a.get("size") or -1)
            digest = str(a.get("digest") or "")
            if digest.lower().startswith("sha256:") and re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
                r.sha256 = digest[7:].lower()
            break
    return r if r.url else None


def latest() -> XltRelease | None:
    """GitHub's latest XLT release, or None (none, or the repository isn't public). Network errors raise."""
    repo = os.environ.get("XLTTOOL_XLT_REPO") or REPO
    api = (os.environ.get("XLTTOOL_UPDATE_API") or update.API).rstrip("/")
    try:
        with update._get(f"{api}/repos/{repo}/releases/latest") as r:
            return parse_release(json.load(r))
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 404:
            return None
        raise


def eboot_version(data: bytes) -> str:
    """The version an XLT EBOOT.PBP was built as ("1.3.1"), "" if it isn't XLT."""
    if data[:4] != b"\0PBP":
        return ""
    m = _VERSION_RE.search(data)
    return m.group(1).decode() if m else ""


def read_eboot(path: str) -> tuple[bytes, str]:
    """An EBOOT.PBP, or the best one in a release zip: (its bytes, its version).
    Raises ValueError when it isn't XLT."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = sorted((eboot_rank(i.filename), i.filename) for i in z.infolist() if not i.is_dir())
            if not names or names[-1][0] == 0:
                raise ValueError("there's no EBOOT.PBP in the zip")
            data = z.read(names[-1][1])
    else:
        with open(path, "rb") as f:
            data = f.read()
    if data[:4] != b"\0PBP" or len(data) < MIN_EBOOT:
        raise ValueError("it isn't an EBOOT.PBP")
    version = eboot_version(data)
    if not version:
        raise ValueError("it's an EBOOT.PBP, but not XLT's")
    return data, version


def download(rel: XltRelease, progress=None, cancel=None) -> bytes:
    """The release's zip, checked against GitHub's size and SHA-256: XLT's EBOOT.PBP from it."""
    h, buf = hashlib.sha256(), bytearray()
    with update._get(rel.url, 60) as r:
        total = rel.size if rel.size > 0 else int(r.headers.get("Content-Length") or 0)
        while True:
            if cancel and cancel():
                raise InterruptedError
            block = r.read(256 * 1024)
            if not block:
                break
            buf += block
            h.update(block)
            if progress and total:
                progress(len(buf) / total)
    if rel.size > 0 and len(buf) != rel.size:
        raise RuntimeError(f"the download is {len(buf)} bytes, GitHub says {rel.size}")
    if rel.sha256 and h.hexdigest() != rel.sha256:
        raise RuntimeError("the download's SHA-256 isn't the one GitHub gives: not used")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as t:
        t.write(buf)
    try:
        data, version = read_eboot(t.name)
    finally:
        os.remove(t.name)
    if update.version_cmp(version, rel.version) != 0:
        raise RuntimeError(f"the release {rel.tag} holds XLT {version}")
    return data


def parse_version_txt(text: str) -> tuple[str, str]:
    """cache/version.txt: (running, next)."""
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    running = lines[0] if lines else ""
    nxt = next((ln[5:].strip() for ln in lines[1:] if ln.startswith("next ")), "")
    return running, nxt


def find_xlt(link) -> PspXlt | None:
    """XLT's folder on the PSP and the version it runs (link: an open xltlink.Link)."""
    for folder in XLT_DIRS:
        if not link.stat(f"{folder}/EBOOT.PBP"):
            continue
        try:
            running, nxt = parse_version_txt(link.get(f"{folder}/cache/version.txt").decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - an XLT from before USB updates
            running, nxt = "", ""
        return PspXlt(folder, running, nxt)
    return None


def release_txt(version: str, notes: str) -> bytes:
    text = f"{version}\n{notes.strip()}\n"
    data = text.encode("utf-8")
    if len(data) > NOTES_MAX:
        data = data[:NOTES_MAX].rsplit(b"\n", 1)[0] + "\n…\n".encode()
    return data


def send(link, xlt: PspXlt, eboot: bytes, version: str, notes: str, progress=None, cancel=None) -> None:
    """Leaves the new XLT in <XLT>/UPDATE/ for XLT to install; XLT.PBP last (what XLT waits for).
    cancel() true stops it (InterruptedError; the PSP drops the half-sent file)."""
    def step(sent, size):
        if cancel and cancel():
            raise InterruptedError
        if progress:
            progress(sent / size)

    folder = f"{xlt.folder}/UPDATE"
    link.mkdir(folder)
    with tempfile.TemporaryDirectory() as d:
        txt, pbp = os.path.join(d, "RELEASE.TXT"), os.path.join(d, "XLT.PBP")
        with open(txt, "wb") as f:
            f.write(release_txt(version, notes))
        with open(pbp, "wb") as f:
            f.write(eboot)
        link.put(txt, f"{folder}/RELEASE.TXT", replace=True)
        link.put(pbp, f"{folder}/XLT.PBP", replace=True, progress=step)


if __name__ == "__main__":  # python -m xlttool.xltupdate [EBOOT.PBP|zip]: what it sees
    import sys
    if len(sys.argv) > 1:
        print(read_eboot(sys.argv[1])[1])
    else:
        print(latest() or "no release")
