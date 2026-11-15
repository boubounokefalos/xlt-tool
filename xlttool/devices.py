"""Find mounted PSP Memory Sticks."""

from __future__ import annotations

import os
from dataclasses import dataclass

REMOVABLE_FS = {"vfat", "msdos", "exfat", "fuseblk", "ntfs", "ntfs3", "fat"}
MARKERS = ("PSP", "MEMSTICK.IND", "MSTK_PRO.IND", "VIDEO", "MP_ROOT")


@dataclass(frozen=True)
class Stick:
    mount: str
    label: str
    free_bytes: int

    def folder(self, wanted: str) -> str:
        """A top-level folder of the stick, in whatever case it already exists."""
        try:
            for name in os.listdir(self.mount):
                if name.upper() == wanted and os.path.isdir(os.path.join(self.mount, name)):
                    return os.path.join(self.mount, name)
        except OSError:
            pass
        return os.path.join(self.mount, wanted)

    @property
    def video_dir(self) -> str:
        return self.folder("VIDEO")     # the XMB looks in /VIDEO (firmware 3.x+)


def _unescape(path: str) -> str:
    return (path.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def find_sticks() -> list[Stick]:
    out = []
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    seen = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in REMOVABLE_FS:
            continue
        mount = _unescape(parts[1])
        # Wherever the desktop or the user mounted it (udisks, fstab, by hand): the
        # PSP folder decides, below. Only the boot partitions are skipped, FAT too.
        if mount in seen or mount == "/" or mount.startswith(("/boot", "/efi")):
            continue
        seen.add(mount)
        try:
            names = {n.upper() for n in os.listdir(mount)}
        except OSError:
            continue
        hits = sum(m in names for m in MARKERS)
        # A PSP folder alone is enough; otherwise need two typical markers.
        if "PSP" not in names and hits < 2:
            continue
        try:
            st = os.statvfs(mount)
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = 0
        out.append(Stick(mount, os.path.basename(mount) or mount, free))
    return out


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
