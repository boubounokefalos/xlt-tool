"""Checking for a newer XLT Tool and updating the AppImage in place. No Qt here.

Like XLT's own update check (its src/update.c): GitHub's latest release of the
public repository, its tag as the version, the AppImage among its files, and the
SHA-256 GitHub gives for each file ("digest"), checked after the download.

Updating: the new AppImage is downloaded next to the running one (APPIMAGE) as
NAME.part, checked (size, SHA-256, it is an ELF program and says its version
with --version), made executable and renamed over the old one. The old one keeps
running until the app restarts. Run from source or from somewhere not writable,
only the check is offered, with a link to the release.

The repository can be set with XLTTOOL_UPDATE_REPO (owner/name) and the API's
address with XLTTOOL_UPDATE_API, to try it against a test release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

REPO = "boubounokefalos/xlt-tool"
API = "https://api.github.com"
AGENT = "XLT-Tool"
ASSET_RE = re.compile(r"XLT_Tool-.*-x86_64\.AppImage$", re.I)


@dataclass
class Release:
    version: str        # "1.1.0" (the tag without its v)
    tag: str
    notes: str
    page: str           # the release's web page
    asset: str = ""     # the AppImage's file name
    url: str = ""       # its download address
    size: int = -1
    sha256: str = ""    # lower-case hex, "" if GitHub gave none


def version_cmp(a: str, b: str) -> int:
    """<0, 0, >0 by number, as XLT's upd_version_cmp: "1.10.0" > "1.9.2", a leading v
    ignored, missing parts 0, and "1.1.0-beta" < "1.1.0"."""
    def split(v: str):
        v = v.strip().lstrip("vV")
        main, _, pre = v.partition("-")
        nums = [int(x) if x.isdigit() else 0 for x in main.split(".")]
        return nums, pre
    (na, pa), (nb, pb) = split(a), split(b)
    n = max(len(na), len(nb))
    na += [0] * (n - len(na))
    nb += [0] * (n - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    if pa == pb:
        return 0
    return 1 if not pa else -1 if not pb else (-1 if pa < pb else 1)


def parse_release(data: dict) -> Release | None:
    tag = str(data.get("tag_name") or "")
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    r = Release(version=tag.lstrip("vV"), tag=tag, notes=str(data.get("body") or ""),
                page=str(data.get("html_url") or ""))
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        if ASSET_RE.search(name):
            r.asset, r.url, r.size = name, str(a.get("browser_download_url") or ""), int(a.get("size") or -1)
            digest = str(a.get("digest") or "")
            if digest.lower().startswith("sha256:") and re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
                r.sha256 = digest[7:].lower()
            break
    return r


def _get(url: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest() -> Release | None:
    """The latest release, or None if there is none yet. Network errors raise."""
    repo = os.environ.get("XLTTOOL_UPDATE_REPO") or REPO
    api = (os.environ.get("XLTTOOL_UPDATE_API") or API).rstrip("/")
    try:
        with _get(f"{api}/repos/{repo}/releases/latest") as r:
            return parse_release(json.load(r))
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 404:  # no release yet (or the repository isn't public yet)
            return None
        raise


def newer(rel: Release | None, current: str = __version__) -> bool:
    return bool(rel and version_cmp(rel.version, current) > 0)


def appimage() -> str:
    """The running AppImage's path if it can be replaced, else ""."""
    p = os.environ.get("APPIMAGE", "")
    if not p or not os.path.isfile(p):
        return ""
    d = os.path.dirname(os.path.abspath(p))
    return p if os.access(d, os.W_OK) and os.access(p, os.W_OK) else ""


def download(rel: Release, target: str, progress=None, cancel=None) -> str:
    """Downloads rel's AppImage beside target as target + ".part" and checks it.
    Returns the .part's path (not yet in place: install() does that)."""
    if not rel.url:
        raise RuntimeError("the release has no AppImage")
    part = target + ".part"
    h, got = hashlib.sha256(), 0
    try:
        with _get(rel.url, 60) as r, open(part, "wb") as f:
            total = rel.size if rel.size > 0 else int(r.headers.get("Content-Length") or 0)
            while True:
                if cancel and cancel():
                    raise InterruptedError
                block = r.read(256 * 1024)
                if not block:
                    break
                f.write(block)
                h.update(block)
                got += len(block)
                if progress and total:
                    progress(got / total)
        if rel.size > 0 and got != rel.size:
            raise RuntimeError(f"the download is {got} bytes, the release says {rel.size}")
        if rel.sha256 and h.hexdigest() != rel.sha256:
            raise RuntimeError("the download's SHA-256 isn't the one GitHub gives: not used")
        with open(part, "rb") as f:
            if f.read(4) != b"\x7fELF":
                raise RuntimeError("the download isn't a Linux program")
        os.chmod(part, os.stat(part).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        out = subprocess.run([part, "--version"], capture_output=True, text=True, timeout=60,
                             env={k: v for k, v in os.environ.items() if k not in ("APPIMAGE", "APPDIR", "ARGV0")})
        said = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        if version_cmp(said, rel.version) != 0:
            raise RuntimeError(f"the new AppImage says it is {said or 'nothing'}, not {rel.version}")
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return part


def install(part: str, target: str) -> None:
    """Puts the checked download in place of the running AppImage."""
    os.replace(part, target)


def restart(target: str) -> None:
    """Starts the (new) AppImage on its own; the caller then quits."""
    env = {k: v for k, v in os.environ.items() if k not in ("APPIMAGE", "APPDIR", "ARGV0", "OWD")}
    subprocess.Popen([target], env=env, start_new_session=True, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def plain_notes(md: str, limit: int = 1500) -> str:
    """Release notes in Markdown as plain text, for a dialog."""
    out = []
    for line in md.replace("\r", "").split("\n"):
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = line.replace("**", "").replace("`", "")
        line = re.sub(r"^\s*[*-]\s+", "- ", line)
        if line.strip() or (out and out[-1].strip()):
            out.append(line.rstrip())
    text = "\n".join(out).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


if __name__ == "__main__":  # python -m xlttool.update: what the check sees
    rel = latest()
    print(rel or "no release", "newer" if newer(rel) else "", file=sys.stdout)
