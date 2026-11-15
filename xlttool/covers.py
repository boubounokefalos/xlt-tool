"""Box art for the game library, from the libretro project (online, only when the user
has turned it on). No Qt here.

- libretro-database maps a game's ID to its name: metadat/serial for the PSP
  ("ULUS-10021" -> "Midnight Club 3 - DUB Edition (USA)"), metadat/redump for PS1.
- libretro-thumbnails has the box art by that name (Named_Boxarts/<name>.png);
  thumbnails.libretro.com serves it. Its list of names (GitHub's API) lets a game
  whose own region has no art take another region's.
Both lists are downloaded once and kept for 30 days; each cover once, by game ID.
Nothing else is sent: the requests carry the names of the lists and covers only.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import games

DB = "https://raw.githubusercontent.com/libretro/libretro-database/master/"
SYSTEMS = {
    "psp": ("Sony - PlayStation Portable", "Sony_-_PlayStation_Portable",
            DB + "metadat/serial/Sony%20-%20PlayStation%20Portable.dat"),
    "ps1": ("Sony - PlayStation", "Sony_-_PlayStation",
            DB + "metadat/redump/Sony%20-%20PlayStation.dat"),
}
THUMBS = "https://thumbnails.libretro.com/"
TREE = "https://api.github.com/repos/libretro-thumbnails/{repo}/git/trees/master"
KEEP = 30 * 86400
AGENT = "XLT-Tool"  # a plain client name, nothing about the user
NOT_FOUND = b""     # cached as an empty file: looked for, none there


def system_of(g: games.Game) -> str:
    if g.kind in ("iso", "cso", "zso", "psn"):
        return "psp"
    if g.kind == "ps1":
        return "ps1"
    return ""  # homebrew: no box art


def covers_dir() -> str:
    return os.path.join(games.cache_dir(), "covers")


def cached(g: games.Game) -> str:
    """The cover already downloaded for g, or ""."""
    sysname = system_of(g)
    if not sysname or not g.game_id:
        return ""
    p = os.path.join(covers_dir(), sysname, f"{g.game_id.upper()}.png")
    try:
        return p if os.path.getsize(p) > 0 else ""
    except OSError:
        return ""


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fresh(path: str) -> bool:
    try:
        return time.time() - os.path.getmtime(path) < KEEP
    except OSError:
        return False


def _save(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)


def parse_dat(text: str) -> dict:
    """A clrmamepro .dat -> {serial without dashes, upper case: name}."""
    out = {}
    for block in re.finditer(r"game \((.*?)\n\)", text, re.S):
        b = block.group(1)
        name = re.search(r'\b(?:name|comment) "([^"]+)"', b)
        if not name:
            continue
        for serial in re.findall(r'serial "([^"]+)"', b):
            key = re.sub(r"[^A-Z0-9]", "", serial.upper())
            out.setdefault(key, name.group(1))
    return out


def _lists(sysname: str) -> tuple[dict, list]:
    """(serial -> name, box art names) for a system, from the cache or downloaded."""
    title, repo, dat_url = SYSTEMS[sysname]
    d = os.path.join(covers_dir(), sysname)
    serials_p, names_p = os.path.join(d, "serials.json"), os.path.join(d, "names.json")
    if not _fresh(serials_p):
        _save(serials_p, json.dumps(parse_dat(_get(dat_url, 60).decode("utf-8", "replace"))).encode())
    if not _fresh(names_p):
        root = json.loads(_get(TREE.format(repo=repo)))
        sha = next(x["sha"] for x in root["tree"] if x["path"] == "Named_Boxarts")
        tree = json.loads(_get(TREE.format(repo=repo).rsplit("/", 1)[0] + "/" + sha, 60))
        names = [x["path"][:-4] for x in tree["tree"] if x["path"].lower().endswith(".png")]
        _save(names_p, json.dumps(names).encode())
    with open(serials_p, encoding="utf-8") as a, open(names_p, encoding="utf-8") as b:
        return json.load(a), json.load(b)


def _key(name: str) -> str:
    """'Silent Hill - Origins (Europe) (En,Fr)' -> 'silenthillorigins': the title alone."""
    base = re.split(r"\s+\(", name, maxsplit=1)[0]
    base = re.sub(r"^the\s+|,\s*the$", "", base.lower())
    return re.sub(r"[^a-z0-9]", "", base)


def _region(name: str) -> str:
    tags = " ".join(re.findall(r"\(([^)]*)\)", name)).lower()
    for r in ("usa", "europe", "japan", "asia", "korea", "australia"):
        if r in tags:
            return r
    return ""


def pick(wanted: str, names: list, region: str = "") -> str:
    """The box art name for a game name: the same name if it has art, else the same
    title in the game's region, else USA, Europe, any. "" if none."""
    if wanted in names:
        return wanted
    safe = re.sub(r'[&*/:`<>?\\|"]', "_", wanted)  # how libretro names the files
    if safe in names:
        return safe
    k = _key(wanted)
    same = [n for n in names if _key(n) == k]
    if not same:
        return ""
    region = (region or _region(wanted)).lower()
    for r in (region, "usa", "europe", ""):
        hits = [n for n in same if not r or _region(n) == r]
        hits = [n for n in hits if "(beta" not in n.lower() and "(demo" not in n.lower()] or hits
        if hits:
            return sorted(hits, key=len)[0]
    return same[0]


def fetch(g: games.Game) -> str:
    """Downloads g's box art into the cache (once; "not found" is remembered too) and
    returns its path, or "". Network errors raise (the caller tries again later)."""
    sysname = system_of(g)
    if not sysname:
        return ""
    gid = g.game_id.upper()
    if gid:
        p = os.path.join(covers_dir(), sysname, f"{gid}.png")
        if os.path.exists(p):
            return p if os.path.getsize(p) > 0 else ""
    else:
        return ""
    serials, names = _lists(sysname)
    wanted = serials.get(re.sub(r"[^A-Z0-9]", "", gid), "") or g.title
    name = pick(wanted, names, games.REGIONS.get(gid[2:3], "") if wanted == g.title else "")
    if not name:
        _save(p, NOT_FOUND)
        return ""
    title = SYSTEMS[sysname][0]
    url = THUMBS + urllib.parse.quote(f"{title}/Named_Boxarts/{name}.png")
    try:
        data = _get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _save(p, NOT_FOUND)
            return ""
        raise
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        _save(p, NOT_FOUND)
        return ""
    _save(p, data)
    return p
