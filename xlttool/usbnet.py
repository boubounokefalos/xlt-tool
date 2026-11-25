"""USBNet: the PSP's internet over the USB cable, by chriopter (github.com/chriopter/pspkit-usbnet,
MIT licence). No Qt here.

XLT Tool doesn't carry it: it fetches USBNet's release from GitHub -- the PSP's plugin as
PSPDX installs it (usbnet-psp.zip: usbnet.prx and the files that go with it) and the
gateway for this computer (pspkit-usbnetd, the PSP's router) -- checks each against the
SHA-256 GitHub gives for it, and keeps them in its data folder (usbnet/<tag>/). It downloads
only when GitHub has a newer release than the one kept; offline, the kept one is used.

The PSP's side: when USBNet is installed on the PSP already (by PSPDX in seplugins/usbnet/,
or by hand as seplugins/usbnet.prx), nothing is sent and nothing there is touched. When it
isn't, XLT Tool leaves the release's files in XLT's folder (STAGED_DIR: XLT's sync may not
write SEPLUGINS, on purpose) and XLT puts them where PSPDX would and switches its line on
(XLT > Settings > Network > Internet over USB, src/netplug.c).

The repository can be set with XLTTOOL_USBNET_REPO (owner/name), the API's address with
XLTTOOL_UPDATE_API (as update.py), to try it against a test release.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import urllib.error
from dataclasses import dataclass, field

from . import games, update

REPO = "chriopter/pspkit-usbnet"
PAGE = f"https://github.com/{REPO}"
PRX = "usbnet.prx"
ZIP = "usbnet-psp.zip"                      # the plugin's release as PSPDX installs it
INSTALLED = ("ms0:/SEPLUGINS/usbnet/usbnet.prx",  # PSPDX's place (and XLT's)
             "ms0:/SEPLUGINS/usbnet.prx")         # the README's
STAGED_DIR = "ms0:/PSP/GAME/XLT/USBNET"     # where XLT Tool leaves it for XLT
USB_ID = (0x054C, 0x01C9)                   # the PSP while USBNet has the cable


def gateway_asset(system: str | None = None, machine: str | None = None) -> str:
    """The gateway's file name in a release, for this computer; "" when there is none."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    if system.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "pspkit-usbnetd-linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "pspkit-usbnetd-linux-aarch64"
        return ""
    if system in ("win32", "windows"):
        return "pspkit-usbnetd-windows-x86_64.exe" if machine in ("amd64", "x86_64") else ""
    if system == "darwin":
        return "pspkit-usbnetd-macos"
    return ""


@dataclass
class Asset:
    name: str
    url: str
    size: int = -1
    sha256: str = ""


@dataclass
class Release:
    tag: str
    page: str
    assets: dict = field(default_factory=dict)  # name -> Asset

    @property
    def version(self) -> str:
        return self.tag.lstrip("vV")


def parse_release(data: dict) -> Release | None:
    tag = str(data.get("tag_name") or "")
    if not tag or data.get("draft") or data.get("prerelease") or not re.fullmatch(r"[\w.\-]{1,40}", tag):
        return None
    r = Release(tag=tag, page=str(data.get("html_url") or PAGE))
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        digest = str(a.get("digest") or "")
        sha = digest[7:].lower() if digest.lower().startswith("sha256:") and re.fullmatch(
            r"[0-9a-fA-F]{64}", digest[7:]) else ""
        if name:
            r.assets[name] = Asset(name, str(a.get("browser_download_url") or ""), int(a.get("size") or -1), sha)
    return r


def latest() -> Release | None:
    """USBNet's latest release, or None if there is none. Network errors raise."""
    repo = os.environ.get("XLTTOOL_USBNET_REPO") or REPO
    api = (os.environ.get("XLTTOOL_UPDATE_API") or update.API).rstrip("/")
    try:
        with update._get(f"{api}/repos/{repo}/releases/latest") as r:
            return parse_release(json.load(r))
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 404:
            return None
        raise


def home() -> str:
    return os.path.join(games.data_dir(), "usbnet")


@dataclass
class Kept:
    """The copy XLT Tool keeps: a release's gateway, and its plugin's files (psp/) unpacked."""
    tag: str
    folder: str
    prx: str
    gateway: str

    @property
    def psp_files(self) -> list[str]:
        d = os.path.dirname(self.prx)
        return sorted(os.path.join(d, n) for n in os.listdir(d))

    @property
    def version(self) -> str:
        return self.tag.lstrip("vV")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def kept() -> Kept | None:
    """The newest complete copy in the data folder, or None."""
    gw = gateway_asset()
    try:
        tags = sorted(os.listdir(home()), key=lambda t: [int(x) if x.isdigit() else 0
                                                        for x in re.split(r"[.\-]", t.lstrip("vV"))], reverse=True)
    except OSError:
        return None
    for tag in tags:
        d = os.path.join(home(), tag)
        prx, gpath = os.path.join(d, "psp", PRX), os.path.join(d, gw) if gw else ""
        if os.path.isfile(prx) and gpath and os.path.isfile(gpath):
            return Kept(tag, d, prx, gpath)
    return None


def _download(asset: Asset, target: str, cancel=None):
    if not asset.url:
        raise RuntimeError(f"{asset.name}: no download address")
    part = target + ".part"
    h, got = hashlib.sha256(), 0
    try:
        with update._get(asset.url, 60) as r, open(part, "wb") as f:
            while True:
                if cancel and cancel():
                    raise InterruptedError
                block = r.read(256 * 1024)
                if not block:
                    break
                f.write(block)
                h.update(block)
                got += len(block)
        if asset.size > 0 and got != asset.size:
            raise RuntimeError(f"{asset.name}: {got} bytes, the release says {asset.size}")
        if not asset.sha256:
            raise RuntimeError(f"{asset.name}: GitHub gives no SHA-256 for it: not used")
        if h.hexdigest() != asset.sha256:
            raise RuntimeError(f"{asset.name}: its SHA-256 isn't the one GitHub gives: not used")
        os.replace(part, target)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def fetch(log=None, cancel=None) -> Kept:
    """The latest USBNet, downloaded if it isn't kept yet; the kept copy when GitHub
    can't be reached. Raises RuntimeError when there is neither."""
    log = log or (lambda line: None)
    gw = gateway_asset()
    if not gw:
        raise RuntimeError(f"USBNet has no gateway for this computer ({sys.platform}, {platform.machine()})")
    have = kept()
    try:
        rel = latest()
    except Exception as e:  # noqa: BLE001 - offline, GitHub down, rate limit
        if have:
            log(f"GitHub not reached ({e}): using USBNet {have.version} kept from before")
            return have
        raise RuntimeError(f"USBNet couldn't be downloaded: {e}") from e
    if rel is None:
        if have:
            return have
        raise RuntimeError("USBNet has no release on GitHub")
    if have and update.version_cmp(rel.version, have.version) <= 0:
        return have  # nothing newer on GitHub
    missing = [n for n in (ZIP, gw) if n not in rel.assets]
    if missing:
        if have:
            log(f"USBNet {rel.version} lacks {', '.join(missing)}: using {have.version}")
            return have
        raise RuntimeError(f"USBNet {rel.version} lacks {', '.join(missing)}")
    d = os.path.join(home(), rel.tag)
    os.makedirs(d, exist_ok=True)
    log(f"downloading USBNet {rel.version} from {PAGE}")
    try:
        for name in (ZIP, gw):
            _download(rel.assets[name], os.path.join(d, name), cancel)
        unpack(os.path.join(d, ZIP), os.path.join(d, "psp"))
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)  # never half a copy
        raise
    gpath = os.path.join(d, gw)
    if not gw.endswith(".exe"):
        os.chmod(gpath, os.stat(gpath).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # older copies go: the new one is complete
    for old in os.listdir(home()):
        if old != rel.tag:
            shutil.rmtree(os.path.join(home(), old), ignore_errors=True)
    return Kept(rel.tag, d, os.path.join(d, "psp", PRX), gpath)


def unpack(zip_path: str, out: str):
    """The plugin's zip into out/: its files, flat, by plain names only (they go onto the PSP by
    these names); it must hold usbnet.prx."""
    import zipfile
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    with zipfile.ZipFile(zip_path) as z:
        names = [i for i in z.infolist() if not i.is_dir()]
        if len(names) > 8 or not any(i.filename == PRX for i in names):
            raise RuntimeError(f"{ZIP} isn't USBNet's plugin as expected: {[i.filename for i in names]}")
        for i in names:
            if not re.fullmatch(r"[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,39}", i.filename) or i.file_size > 4 << 20:
                raise RuntimeError(f"{ZIP}: unexpected file {i.filename!r}")
        for i in names:
            with z.open(i) as src, open(os.path.join(out, i.filename), "wb") as dst:
                shutil.copyfileobj(src, dst)


# ------------------------------------------------------------------ the gateway's log

def read_line(line: str) -> tuple[str, str] | None:
    """What a line of `pspkit-usbnetd -v --stats` says, for the button: (kind, text) with kind
    "waiting", "online", "no_access", "busy" or "stats"; None for the rest."""
    text = re.sub(r"^\s*\d+\.\d+\s+(?:[^|]+\|\s+)?", "", line.strip())
    if text.startswith("usb: device found"):
        return "online", "PSP connected"
    if text.startswith("usb: device lost"):
        return "waiting", "waiting for the PSP"
    if text.startswith("usb: waiting for the device:"):
        why = text.split(":", 2)[2].strip()
        if "no access" in why:
            return "no_access", "no access to the PSP on USB"
        if "another program" in why:
            return "busy", "another program has the PSP's network"
        return "waiting", "waiting for the PSP"
    m = re.match(r"stats: .*?tcp (\d+) open.*?(\d+) B up, (\d+) B down", text)
    if m:
        conns, up, down = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out = f"PSP online · {conns} connection{'s' if conns != 1 else ''}" if conns else "PSP online"
        mb = (up + down) / 1e6
        return "stats", out + (f" · {mb:.1f} MB" if mb >= 0.05 else "")
    return None


# ------------------------------------------------------------------ Linux: USB access

UDEV_LINE = 'SUBSYSTEM=="usb", ATTRS{idVendor}=="054c", ATTRS{idProduct}=="01c9", TAG+="uaccess"'


def udev_ok(path: str = "/etc/udev/rules.d") -> bool:
    """True when a rule there gives the user USBNet's device (054c:01c9)."""
    try:
        names = os.listdir(path)
    except OSError:
        return False
    for n in names:
        if not n.endswith(".rules"):
            continue
        try:
            with open(os.path.join(path, n), encoding="utf-8", errors="replace") as f:
                t = f.read().lower()
        except OSError:
            continue
        if "054c" in t and "01c9" in t:
            return True
    return False
