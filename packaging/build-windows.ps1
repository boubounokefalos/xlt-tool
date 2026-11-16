# Build XLT Tool for Windows: dist\XLT_Tool-<version>-windows-x64-setup.exe (the installer,
# packaging\xlt-tool.iss) and dist\XLT_Tool-<version>-windows-x64.zip (the same, portable).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 [-NoInstaller] [-TestInstaller]
#
# The installer needs Inno Setup 6 (winget install JRSoftware.InnoSetup); -NoInstaller makes
# the zip only. -TestInstaller installs it silently into a temporary folder, starts the
# installed app and uninstalls it again (the GitHub workflow does this).
#
# A folder build made with PyInstaller: "XLT Tool.exe" with Python, PySide6-Essentials,
# mutagen, pyusb and libusb (libusb-package) beside it, and in bin\ ffmpeg/ffprobe (BtbN
# GPL shared build, the AppImage's 8.1) and atracdenc (its own Windows build, for PS1
# games' CD music). The app finds them there (media._bundled, psx.atracdenc).
#
# Needs Python 3.11+ ($env:XLTTOOL_PYTHON, else py -3, else python) and network on the
# first run; downloads are cached in build\cache and checked by SHA-256.
#
# Not signed: SmartScreen warns once when the downloaded installer or exe is started, and
# Windows 11's Smart App Control, when on, blocks it.
param([switch]$NoInstaller, [switch]$TestInstaller)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # Invoke-WebRequest is many times slower with it

$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Root 'build\windows'
$Cache = Join-Path $Root 'build\cache'
$Dist = Join-Path $Root 'dist'

# The same versions as build-appimage.sh where Windows has them.
# ffmpeg 8.1, not 9.x: 9.x needs NVIDIA driver 610+ for NVENC (see build-appimage.sh).
$FfRepo = 'BtbN/FFmpeg-Builds'
$FfAsset = 'ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip'     # rolling: checked against GitHub's digest
$AtUrl = 'https://github.com/dcherednik/atracdenc/releases/download/0.2.3/atracdenc-win-x86_0.2.3.zip'
$AtSha = '16801a1d8b09d9585e03f8e212805a5d8d262b72ea06fc57461cf560f378d3e3'  # LGPL-2.1
# Its zip has no licence text: the one from the same tag goes in licenses\.
$AtLicUrl = 'https://raw.githubusercontent.com/dcherednik/atracdenc/0.2.3/LICENSE'
$AtLicSha = 'b634ab5640e258563c536e658cad87080553df6f34f62269a21d554844e58bfe'
$Packages = @(
    'PySide6-Essentials==6.12.0',
    'mutagen==1.48.1',
    'pyusb==1.3.1',               # XLT's USB sync (xlttool/xltlink.py)
    'libusb-package==1.0.30.1',   # libusb-1.0.dll for it: nothing to install on the PC
    'pyinstaller==6.22.3'
)

$Version = (Select-String -Path (Join-Path $Root 'xlttool\__init__.py') -Pattern '^__version__ = "(.*)"').Matches[0].Groups[1].Value
$AppName = 'XLT Tool'
$Out = Join-Path $Dist "XLT_Tool-$Version-windows-x64.zip"

function Say($text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Run {    # a native command; stops the build when it fails. No param block: -m and
    # the like must reach the command, not be taken as Run's own parameters. @(...): one
    # argument would otherwise be a bare string, and splatting a string passes its letters.
    $rest = @($args | Select-Object -Skip 1)
    & $args[0] @rest
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $($args -join ' ')" }
}

function WriteText($path, $text) {   # UTF-8 without the BOM Out-File puts in on PowerShell 5.1
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))
}

function Fetch($url, $file, $sha) {
    $path = Join-Path $Cache $file
    if (-not (Test-Path $path)) {
        Say "Downloading $file"
        Invoke-WebRequest -Uri $url -OutFile "$path.tmp" -UseBasicParsing
        Move-Item -Force "$path.tmp" $path
    }
    $got = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower()
    if ($sha -and $got -ne $sha) {
        Remove-Item $path
        throw "$file : SHA-256 $got, expected $sha (removed; run again to download it anew)"
    }
    Write-Host "    sha256 $got  $file"
    return $path
}

function ReleaseAsset($repo, $tag, $name) {   # url and sha256 from GitHub's own digest
    $rel = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$repo/releases/tags/$tag"
    $a = $rel.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
    if (-not $a) { throw "$repo $tag has no $name" }
    $sha = if ($a.digest -like 'sha256:*') { $a.digest.Substring(7) } else { '' }
    return @{ url = $a.browser_download_url; sha = $sha }
}

New-Item -ItemType Directory -Force $Build, $Cache, $Dist | Out-Null

# ------------------------------------------------------------------ downloads --
$ff = ReleaseAsset $FfRepo 'latest' $FfAsset
# The rolling build is replaced every day: an older cached copy no longer matches.
$ffZip = Join-Path $Cache $FfAsset
if ((Test-Path $ffZip) -and $ff.sha -and (Get-FileHash -Algorithm SHA256 $ffZip).Hash.ToLower() -ne $ff.sha) {
    Remove-Item $ffZip
}
$ffZip = Fetch $ff.url $FfAsset $ff.sha
$atZip = Fetch $AtUrl 'atracdenc-win-x86_0.2.3.zip' $AtSha
$atLic = Fetch $AtLicUrl 'atracdenc-0.2.3-LICENSE.txt' $AtLicSha

# ---------------------------------------------------------------- the Python --
$venv = Join-Path $Build 'venv'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    Say 'Making the build venv'
    if ($env:XLTTOOL_PYTHON) { Run $env:XLTTOOL_PYTHON -m venv $venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { Run py -3 -m venv $venv }
    else { Run python -m venv $venv }
}
Say "Installing $($Packages -join ', ')"
Run $py -m pip install --disable-pip-version-check --quiet --only-binary=:all: @Packages

# ------------------------------------------------------------------ the icon --
Say 'Making the icon'
$ico = Join-Path $Build 'xlt-tool.ico'
$env:QT_QPA_PLATFORM = 'offscreen'
Run $py -I -c @"
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication([])
img = QImage(256, 256, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
QSvgRenderer(sys.argv[1]).render(p)
p.end()
sys.exit(0 if img.save(sys.argv[2], 'ICO') else 1)
"@ (Join-Path $Root 'xlttool\xlt-tool.svg') $ico
Remove-Item Env:\QT_QPA_PLATFORM

# --------------------------------------------------------------- the version --
# Shown in the file's Properties → Details, and by SmartScreen.
$parts = @(($Version -replace '[^0-9.].*$', '').Split('.') | Where-Object { $_ -ne '' })
while ($parts.Count -lt 4) { $parts += '0' }
$tuple = ($parts[0..3] -join ', ')
$verFile = Join-Path $Build 'version.txt'
WriteText $verFile @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($tuple), prodvers=($tuple), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
          StringStruct('FileDescription', '$AppName'),
          StringStruct('ProductName', '$AppName'),
          StringStruct('FileVersion', '$Version'),
          StringStruct('ProductVersion', '$Version'),
          StringStruct('OriginalFilename', '$AppName.exe'),
          StringStruct('LegalCopyright', 'GPL-3.0')])]),
        VarFileInfo([VarStruct('Translation', [0x0409, 1200])])]
)
"@

# -------------------------------------------------------------------- the app --
Say 'Building with PyInstaller'
$launch = Join-Path $Build 'launch.py'
@"
import sys
from xlttool.app import main
sys.exit(main(sys.argv))
"@ | Out-File -Encoding ascii $launch
$work = Join-Path $Build 'work'
$appDist = Join-Path $Build 'dist'
$env:PYTHONPATH = $Root
Run $py -m PyInstaller --noconfirm --clean --log-level WARN --windowed --onedir `
    --name $AppName --icon $ico --version-file $verFile `
    --distpath $appDist --workpath $work --specpath $Build `
    --add-data "$(Join-Path $Root 'xlttool\xlt-tool.svg');xlttool" `
    --collect-binaries libusb_package --collect-data libusb_package `
    --exclude-module tkinter --exclude-module unittest --exclude-module pydoc `
    $launch
Remove-Item Env:\PYTHONPATH
$app = Join-Path $appDist $AppName
$bin = Join-Path $app 'bin'
$lic = Join-Path $app 'licenses'
New-Item -ItemType Directory -Force $bin, $lic | Out-Null

Say 'Adding ffmpeg'
$tmp = Join-Path $Build 'ffmpeg-extract'
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $ffZip -DestinationPath $tmp
$ffDir = Get-ChildItem $tmp -Directory | Select-Object -First 1
# Shared build: ffmpeg.exe and ffprobe.exe use one copy of the av*.dll beside them.
Get-ChildItem (Join-Path $ffDir.FullName 'bin') | Where-Object { $_.Name -ne 'ffplay.exe' } |
    Copy-Item -Destination $bin
Copy-Item (Join-Path $ffDir.FullName 'LICENSE.txt') (Join-Path $lic 'FFMPEG-LICENSE.txt') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $tmp

Say 'Adding atracdenc'
$tmp = Join-Path $Build 'atracdenc-extract'
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $atZip -DestinationPath $tmp
$atExe = Get-ChildItem $tmp -Recurse -Filter 'atracdenc.exe' | Select-Object -First 1
if (-not $atExe) { throw 'no atracdenc.exe in its zip' }
Get-ChildItem $atExe.DirectoryName -File | Where-Object { $_.Extension -in '.exe', '.dll' } | Copy-Item -Destination $bin
Copy-Item $atLic (Join-Path $lic 'ATRACDENC-LICENSE.txt')
Remove-Item -Recurse -Force $tmp

Copy-Item (Join-Path $Root 'LICENSE') (Join-Path $lic 'LICENSE.txt')
Copy-Item (Join-Path $Root 'docs\Credits.md') (Join-Path $lic 'CREDITS.md')

# ---------------------------------------------------------------- smoke test --
Say 'Smoke test'
$encoders = & (Join-Path $bin 'ffmpeg.exe') -hide_banner -encoders 2>$null | Out-String
if ($encoders -notmatch 'libx264') { throw 'ffmpeg is missing libx264' }
Write-Host '    ffmpeg has libx264'
& (Join-Path $bin 'ffprobe.exe') -hide_banner -version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'ffprobe does not run' }
& (Join-Path $bin 'atracdenc.exe') --help *> $null
if (-not $?) { Write-Host '    (atracdenc --help returned non-zero; checked below by encoding)' }
# The packed app's own Python finds the tools, Qt and libusb, without a window.
$env:QT_QPA_PLATFORM = 'offscreen'
$check = Join-Path $Build 'smoke.py'
@"
import os, sys, struct, subprocess, tempfile
from xlttool import media, psx
ff, fp, at = media.ffmpeg_path(), media.ffprobe_path(), psx.atracdenc()
assert ff and fp and at, (ff, fp, at)
import usb.core, libusb_package
assert libusb_package.get_libusb1_backend() is not None, 'no libusb'
with tempfile.TemporaryDirectory() as t:   # a second of silence through atracdenc, as psx does it
    pcm = bytes(44100 * 4)
    wav, aea = os.path.join(t, 'a.wav'), os.path.join(t, 'a.aea')
    with open(wav, 'wb') as f:
        f.write(psx._wav(pcm))
    subprocess.run([at, '--encode=atrac3', '-i', wav, '-o', aea], check=True, capture_output=True)
    assert os.path.getsize(aea) > 0x60, 'atracdenc wrote nothing'
print('ok', ff, at)
"@ | Out-File -Encoding ascii $check
# Run the check with the build's Python against the files in the packed folder.
$env:PYTHONPATH = $Root
$env:XLTTOOL_FFMPEG = Join-Path $bin 'ffmpeg.exe'
$env:XLTTOOL_FFPROBE = Join-Path $bin 'ffprobe.exe'
$env:XLTTOOL_ATRACDENC = Join-Path $bin 'atracdenc.exe'
Run $py $check
foreach ($v in 'QT_QPA_PLATFORM', 'PYTHONPATH', 'XLTTOOL_FFMPEG', 'XLTTOOL_FFPROBE', 'XLTTOOL_ATRACDENC') {
    Remove-Item "Env:\$v" -ErrorAction SilentlyContinue
}
# The packed exe itself starts (no console: it is a windowed program) and stays up.
$env:QT_QPA_PLATFORM = 'offscreen'
$env:XLTTOOL_NO_INTRO = '1'
$p = Start-Process -FilePath (Join-Path $app "$AppName.exe") -PassThru
Start-Sleep -Seconds 8
$alive = -not $p.HasExited
if ($alive) { Stop-Process -Id $p.Id -Force }
Remove-Item Env:\QT_QPA_PLATFORM, Env:\XLTTOOL_NO_INTRO
if (-not $alive) { throw "$AppName.exe quit at start (exit code $($p.ExitCode))" }
Write-Host "    $AppName.exe starts"

# ---------------------------------------------------------------------- pack --
$size = [math]::Round(((Get-ChildItem $app -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))
Say "Packing $size MB"
if (Test-Path $Out) { Remove-Item $Out }
Compress-Archive -Path $app -DestinationPath $Out -CompressionLevel Optimal
Say "Zip: $Out ($([math]::Round((Get-Item $Out).Length / 1MB)) MB)"

# ----------------------------------------------------------------- installer --
if ($NoInstaller) { return }
$iscc = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | ForEach-Object Source),
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    throw 'Inno Setup 6 is needed for the installer: winget install JRSoftware.InnoSetup (or -NoInstaller for the zip only)'
}
Say 'Making the installer'
$setup = Join-Path $Dist "XLT_Tool-$Version-windows-x64-setup.exe"
Run $iscc /Q "/DAppVersion=$Version" "/DFileVersion=$($parts[0..3] -join '.')" "/DSourceDir=$app" `
    "/DIconFile=$ico" "/DOutputDir=$Dist" (Join-Path $PSScriptRoot 'xlt-tool.iss')
Say "Installer: $setup ($([math]::Round((Get-Item $setup).Length / 1MB)) MB)"

if ($TestInstaller) {
    Say 'Installer test'
    $dir = Join-Path $Build 'install-test'
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    $log = Join-Path $Build 'install-test.log'
    $p = Start-Process $setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER',
        "/DIR=`"$dir`"", "/LOG=`"$log`"" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "the installer failed ($($p.ExitCode)), see $log" }
    foreach ($f in 'XLT Tool.exe', 'bin\ffmpeg.exe', 'bin\atracdenc.exe', 'unins000.exe') {
        if (-not (Test-Path (Join-Path $dir $f))) { throw "not installed: $f" }
    }
    $env:QT_QPA_PLATFORM = 'offscreen'
    $env:XLTTOOL_NO_INTRO = '1'
    $p = Start-Process (Join-Path $dir 'XLT Tool.exe') -PassThru
    Start-Sleep -Seconds 8
    $alive = -not $p.HasExited
    if ($alive) { Stop-Process -Id $p.Id -Force; Start-Sleep -Seconds 1 }
    Remove-Item Env:\QT_QPA_PLATFORM, Env:\XLTTOOL_NO_INTRO
    $p = Start-Process (Join-Path $dir 'unins000.exe') -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
    # The uninstaller copies itself to a temporary folder and finishes from there.
    for ($i = 0; $i -lt 30 -and (Test-Path (Join-Path $dir 'XLT Tool.exe')); $i++) { Start-Sleep -Seconds 1 }
    if (-not $alive) { throw 'the installed XLT Tool.exe quit at start' }
    if (Test-Path (Join-Path $dir 'XLT Tool.exe')) { throw 'the uninstaller left XLT Tool.exe behind' }
    Write-Host '    installed, started, uninstalled'
}
