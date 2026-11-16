"""Access to the PSP over USB.

Linux: a udev rule for XLT's sync device. Without it only root may open the device.
The rule (the same as XLT's tools/60-xltsync.rules) gives the logged-in user access
(systemd-logind or elogind's "uaccess"), and is put in place with pkexec, the
desktop's own password prompt. If there is no pkexec, the commands are shown to copy.

Windows: nothing to install. XLT's sync device answers Windows' MS OS 2.0 requests
with "WINUSB", so Windows 8.1+ gives it the WinUSB driver by itself. When another
driver got there first (a libusb0 or libusbK one put in by Zadig, say), the user
is shown how to take it away in Device Manager.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

RULE_PATH = "/etc/udev/rules.d/60-xltsync.rules"
RULE = """# XLT's USB sync link: lets the logged-in user send files to a PSP running XLT.
# Installed by XLT Tool. Remove this file to take the access away again.
SUBSYSTEM=="usb", ATTRS{idVendor}=="054c", ATTRS{idProduct}=="01cb", TAG+="uaccess"
"""
# $1: the rule in a temporary file. The trigger applies it to a PSP already plugged in.
SCRIPT = (f'install -m 644 "$1" {RULE_PATH} && udevadm control --reload && '
          "udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=054c")


WINDOWS_HELP = (
    "Windows normally sets the PSP up by itself the first time USB sync is on and the "
    "cable is in: it gives it the WinUSB driver, nothing to install.\n\n"
    "If XLT Tool still can't reach it, another driver has taken the PSP:\n"
    "1. Open Device Manager and find the PSP (under \"Universal Serial Bus devices\", "
    "\"libusb\" or \"Other devices\").\n"
    "2. Right-click it → Uninstall device, tick \"Attempt to remove the driver\" if it is there.\n"
    "3. Unplug the cable and plug it in again.")


def _offer_windows(parent, app: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Information, app,
                      "XLT Tool can see the PSP but Windows doesn't let it in.", parent=parent)
    box.setInformativeText(WINDOWS_HELP)
    devmgr = box.addButton("Open Device Manager", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is devmgr:
        try:
            subprocess.Popen(["mmc.exe", "devmgmt.msc"])
        except OSError as e:
            QMessageBox.warning(parent, app, f"Couldn't open Device Manager: {e}")
    return False


def offer(parent) -> bool:
    """Explains, asks, installs. True if the rule went in."""
    app = parent.windowTitle() if parent else "XLT Tool"
    if sys.platform == "win32":
        return _offer_windows(parent, app)
    commands = (f"sudo tee {RULE_PATH} <<'EOF'\n{RULE}EOF\n"
                "sudo udevadm control --reload\n"
                "sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=054c")
    if not shutil.which("pkexec"):
        box = QMessageBox(QMessageBox.Icon.Information, app,
                          "To send files to the PSP over USB, Linux needs a rule that lets you use it. "
                          "Run these commands in a terminal (they ask for your password):",
                          parent=parent)
        box.setDetailedText(commands)
        copy = box.addButton("Copy commands", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy:
            QApplication.clipboard().setText(commands)
        return False
    if QMessageBox.question(
            parent, app,
            "To send files to the PSP over USB, Linux needs a rule that lets you use it "
            f"({RULE_PATH}). Your password is asked for once to add it.\n\nAdd it now?") \
            != QMessageBox.StandardButton.Yes:
        return False
    import os
    import tempfile
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as f:
            f.write(RULE)
        os.chmod(f.name, 0o644)
        r = subprocess.run(["pkexec", "sh", "-c", SCRIPT, "sh", f.name], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        QMessageBox.warning(parent, app, f"Couldn't add the rule: {e}")
        return False
    finally:
        try:
            os.remove(f.name)
        except (OSError, NameError):
            pass
    if r.returncode != 0:
        if r.returncode not in (126, 127):  # 126: the password prompt was dismissed
            QMessageBox.warning(parent, app, f"Couldn't add the rule:\n{(r.stderr or r.stdout).strip()}")
        return False
    QMessageBox.information(parent, app, "Done. If the PSP doesn't show up in a few seconds, "
                                         "unplug the cable and plug it in again.")
    return True
