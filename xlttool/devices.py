"""Find mounted PSP Memory Sticks: from /proc/self/mounts on Linux, the removable
drive letters on Windows."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

REMOVABLE_FS = {"vfat", "msdos", "exfat", "fuseblk", "ntfs", "ntfs3", "fat", "fat32"}  # fat32: Windows' name
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


def _is_stick(mount: str) -> bool:
    try:
        names = {n.upper() for n in os.listdir(mount)}
    except OSError:
        return False
    # A PSP folder alone is enough; otherwise need two typical markers.
    return "PSP" in names or sum(m in names for m in MARKERS) >= 2


def _free(mount: str) -> int:
    try:
        return shutil.disk_usage(mount).free
    except OSError:
        return 0


def _linux_mounts() -> list[tuple[str, str]]:
    """(mount, label) of the FAT/exFAT/NTFS mounts."""
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out, seen = [], set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in REMOVABLE_FS:
            continue
        mount = _unescape(parts[1])
        # Wherever the desktop or the user mounted it (udisks, fstab, by hand): the
        # PSP folder decides. Only the boot partitions are skipped, FAT too.
        if mount in seen or mount == "/" or mount.startswith(("/boot", "/efi")):
            continue
        seen.add(mount)
        out.append((mount, os.path.basename(mount) or mount))
    return out


def _windows_drives() -> list[tuple[str, str]]:
    """(root, label) of the removable drives: 'E:\\', 'PSP (E:)'. A PSP in USB mode and
    card readers show as removable; fixed disks and network drives are left out."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)     # SEM_FAILCRITICALERRORS: no "insert a disk" box for an empty reader
    out = []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if k32.GetDriveTypeW(root) != 2:        # DRIVE_REMOVABLE
            continue
        name, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
        if not k32.GetVolumeInformationW(root, name, 261, None, None, None, fs, 261):
            continue                            # no medium in it
        if fs.value.lower() not in REMOVABLE_FS:
            continue
        out.append((root, f"{name.value or 'Removable disk'} ({root[:2]})"))
    return out


def find_sticks() -> list[Stick]:
    mounts = _windows_drives() if sys.platform == "win32" else _linux_mounts()
    return [Stick(m, label, _free(m)) for m, label in mounts if _is_stick(m)]


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
