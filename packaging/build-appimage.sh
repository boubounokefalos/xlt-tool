#!/usr/bin/env bash
# Build XLT_Tool-x86_64.AppImage
#
# Bundles: portable CPython (python-build-standalone), PySide6-Essentials,
# jeepney, mutagen, pyusb with libusb, a static ffmpeg/ffprobe (BtbN GPL shared build: libx264, libass, zimg),
# atracdenc (ATRAC3, for PS1 games' CD music; built here without libsndfile, see
# packaging/atracdenc) and the app. Needs: curl, tar, xz, readelf (binutils), cmake and
# g++. Network on first run;
# downloads are cached in build/cache.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
CACHE="$BUILD/cache"
APPDIR="$BUILD/AppDir"
DIST="$ROOT/dist"

# ffmpeg 8.1, not 9.x: 9.x needs NVIDIA driver 610+ for NVENC, and Pascal and
# older GPUs (e.g. GTX 10xx) are stuck on the 580 driver series.
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
FF_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-shared-8.1.tar.xz"
AIT_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
PYSIDE_VERSION="6.12.0"
JEEPNEY_VERSION="0.9.0"
MUTAGEN_VERSION="1.48.1"
PYUSB_VERSION="1.3.1"            # XLT's USB sync (xlttool/xltlink.py)
LIBUSB_PACKAGE_VERSION="1.0.30.0" # libusb for it, in the AppImage: no system package needed
ATRACDENC_COMMIT="0fe8e1a0d1a495e41a281f54af31d6713c0de01d"  # LGPL-2.1; PS1 CD music to ATRAC3
ATRACDENC_URL="https://github.com/dcherednik/atracdenc/archive/$ATRACDENC_COMMIT.tar.gz"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/xlttool/__init__.py")"
OUT="$DIST/XLT_Tool-$VERSION-x86_64.AppImage"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

fetch() {   # url file
    if [[ ! -s "$CACHE/$2" ]]; then
        say "Downloading $2"
        curl -fL --retry 3 --progress-bar -o "$CACHE/$2.tmp" "$1"
        mv "$CACHE/$2.tmp" "$CACHE/$2"
    fi
    printf '    sha256 %s  %s\n' "$(sha256sum "$CACHE/$2" | cut -d' ' -f1)" "$2"
}

trim_qt() {   # site-packages dir
    local SITE="$1" QT="$1/PySide6/Qt" f m d lib src
    say "Trimming Qt to what the app uses"
    # Python modules the app imports
    local keep_mods="QtCore QtGui QtWidgets QtDBus QtSvg"
    for f in "$SITE"/PySide6/Qt*.abi3.so "$SITE"/PySide6/Qt*.pyi; do
        m="$(basename "$f")"; m="${m%%.*}"
        [[ " $keep_mods " == *" $m "* ]] || rm -f "$f"
    done
    # Plugins: X11, Wayland, portal theme, SVG icons, input methods
    for d in "$QT"/plugins/*/; do
        case "$(basename "$d")" in
            platforms|platformthemes|platforminputcontexts|xcbglintegrations|\
            wayland-decoration-client|wayland-shell-integration|\
            wayland-graphics-integration-client|imageformats|iconengines|tls) ;;
            *) rm -rf "$d" ;;
        esac
    done
    find "$QT/plugins/platforms" -name '*.so' ! -name 'libqxcb.so' ! -name 'libqwayland*.so' ! -name 'libqoffscreen.so' -delete
    rm -f "$QT"/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so
    find "$QT/plugins/imageformats" -name '*.so' ! -name 'libqsvg.so' ! -name 'libqjpeg.so' \
        ! -name 'libqpng.so' ! -name 'libqico.so' ! -name 'libqgif.so' -delete
    rm -rf "$QT/qml" "$QT/translations" "$QT/metatypes" "$QT/libexec" "$QT/resources/qtwebengine"* \
        "$SITE"/PySide6/{include,typesystems,glue,scripts,examples,doc} \
        "$SITE"/PySide6/{assistant,designer,linguist,lrelease,lupdate,qmlformat,qmllint,qmlls,balsam,balsamui,qsb,svgtoqml}* \
        2>/dev/null || true

    # Keep only Qt libraries reachable (DT_NEEDED) from what is left.
    python3 - "$SITE" <<'PYEOF'
import os, re, subprocess, sys
site = sys.argv[1]
libdir = os.path.join(site, "PySide6", "Qt", "lib")
def needed(path):
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out)
roots = [os.path.join(b, f) for b, _, fs in os.walk(site) for f in fs
         if (f.endswith(".so") or ".so." in f) and not b.startswith(libdir)]
present = set(os.listdir(libdir))
keep, todo = set(), list(roots)
while todo:
    for lib in needed(todo.pop()):
        if lib in present and lib not in keep:
            keep.add(lib)
            todo.append(os.path.join(libdir, lib))
removed = 0
for f in present:
    p = os.path.join(libdir, f)
    if f not in keep and os.path.basename(os.path.realpath(p)) not in keep and not os.path.isdir(p):
        os.remove(p)
        removed += 1
print(f"    kept {len(keep)} Qt libraries, removed {removed}")
PYEOF

    # libxcb-cursor & co: needed by Qt's X11 plugin, not installed by default
    # on several distros (e.g. Ubuntu). Small and ABI-stable, so bundle them.
    for lib in libxcb-cursor.so.0 libxcb-image.so.0 libxcb-render-util.so.0 libxcb-util.so.1; do
        src="$(ldconfig -p 2>/dev/null | awk -v l="$lib" '$1==l && /x86-64/ && !f {print $NF; f=1}')"
        if [[ -n "$src" ]]; then cp -L "$src" "$QT/lib/$lib"; else echo "    (host has no $lib)"; fi
    done
}

if [[ "${1:-}" == "--trim-only" ]]; then trim_qt "$2"; exit 0; fi

mkdir -p "$CACHE" "$DIST"
fetch "$PY_URL" python.tar.gz
fetch "$FF_URL" ffmpeg81-shared.tar.xz
fetch "$AIT_URL" appimagetool
fetch "$ATRACDENC_URL" "atracdenc-${ATRACDENC_COMMIT:0:7}.tar.gz"
chmod +x "$CACHE/appimagetool"

say "Assembling AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/xlt-tool"
tar xzf "$CACHE/python.tar.gz" -C "$APPDIR/usr"          # -> usr/python
PY="$APPDIR/usr/python/bin/python3"
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
SITE="$APPDIR/usr/python/lib/python$PYVER/site-packages"

say "Installing PySide6-Essentials $PYSIDE_VERSION, jeepney, mutagen, pyusb and libusb"
"$PY" -m pip install --no-cache-dir --disable-pip-version-check --no-compile \
    --only-binary=:all: "PySide6-Essentials==$PYSIDE_VERSION" "jeepney==$JEEPNEY_VERSION" \
    "mutagen==$MUTAGEN_VERSION" "pyusb==$PYUSB_VERSION" "libusb-package==$LIBUSB_PACKAGE_VERSION"

say "Adding ffmpeg"
FFTMP="$BUILD/ffmpeg-extract"
rm -rf "$FFTMP" && mkdir -p "$FFTMP"
tar xJf "$CACHE/ffmpeg81-shared.tar.xz" -C "$FFTMP"
install -m755 "$FFTMP"/*/bin/ffmpeg "$FFTMP"/*/bin/ffprobe "$APPDIR/usr/bin/"
# Shared build: ffmpeg and ffprobe use one copy of the libav* libraries
# (static binaries would duplicate ~160 MB). Their RUNPATH is $ORIGIN/../lib.
mkdir -p "$APPDIR/usr/lib"
cp -a "$FFTMP"/*/lib/*.so* "$APPDIR/usr/lib/"
install -m644 "$FFTMP"/*/LICENSE.txt "$APPDIR/usr/share/xlt-tool/FFMPEG-LICENSE.txt" 2>/dev/null || true
rm -rf "$FFTMP"

say "Building atracdenc"
ATTMP="$BUILD/atracdenc"
rm -rf "$ATTMP" && mkdir -p "$ATTMP"
tar xzf "$CACHE/atracdenc-${ATRACDENC_COMMIT:0:7}.tar.gz" -C "$ATTMP" --strip-components=1
# WAV input of its own in place of libsndfile (and its codec libraries), linked fully
# static: one program that needs no system library at all, so the glibc of the PC it
# is built on doesn't matter (built dynamically on a new distro it needs that glibc)
cp "$ROOT/packaging/atracdenc/pcm_io_wav.cpp" "$ATTMP/src/pcm_io_sndfile.cpp"
sed -i 's/^\s*INCLUDE(FindLibSndFile)/    # (libsndfile not used)/' "$ATTMP/src/CMakeLists.txt"
cmake -S "$ATTMP/src" -B "$ATTMP/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_CXX_FLAGS="-include cstdint" -DCMAKE_EXE_LINKER_FLAGS="-static -s" >/dev/null
cmake --build "$ATTMP/build" --target atracdenc -j"$(nproc)" >/dev/null
install -m755 "$ATTMP/build/atracdenc" "$APPDIR/usr/bin/atracdenc"
if readelf -d "$APPDIR/usr/bin/atracdenc" 2>/dev/null | grep -q NEEDED; then
    echo "atracdenc isn't static (needs: $(readelf -d "$APPDIR/usr/bin/atracdenc" | grep NEEDED))" >&2; exit 1
fi
install -m644 "$ATTMP/LICENSE" "$APPDIR/usr/share/xlt-tool/ATRACDENC-LICENSE.txt"
install -m644 "$ROOT/LICENSE" "$APPDIR/usr/share/xlt-tool/LICENSE.txt"
install -m644 "$ROOT/docs/Credits.md" "$APPDIR/usr/share/xlt-tool/CREDITS.md"
rm -rf "$ATTMP"

trim_qt "$SITE"
QT="$SITE/PySide6/Qt"

say "Trimming Python"
PYLIB="$APPDIR/usr/python/lib/python$PYVER"
rm -rf "$PYLIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data} \
       "$PYLIB"/lib-dynload/_tkinter* "$APPDIR"/usr/python/lib/{libtcl,libtk,tcl,tk,itcl,thread}* \
       "$APPDIR"/usr/python/{include,share} "$SITE"/pip "$SITE"/pip-* 2>/dev/null || true
rm -f "$APPDIR"/usr/python/bin/{pip*,idle*,pydoc*,2to3*,python*-config}

say "Adding the app"
cp -r "$ROOT/xlttool" "$APPDIR/usr/share/xlt-tool/"
find "$APPDIR/usr/share/xlt-tool" -name '__pycache__' -prune -exec rm -rf {} +
cat > "$APPDIR/usr/share/xlt-tool/launch.py" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xlttool.app import main
sys.exit(main(sys.argv))
PYEOF
# The image is read-only: precompile so Python doesn't recompile every start.
"$PY" -m compileall -q -j0 "$APPDIR/usr/python/lib" "$APPDIR/usr/share/xlt-tool" >/dev/null || true

cat > "$APPDIR/AppRun" <<'SHEOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export APPDIR="${APPDIR:-$HERE}"
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP QT_PLUGIN_PATH QML2_IMPORT_PATH
exec "$HERE/usr/python/bin/python3" -I "$HERE/usr/share/xlt-tool/launch.py" "$@"
SHEOF
chmod +x "$APPDIR/AppRun"

python3 - "$ROOT" "$APPDIR/xlt-tool.desktop" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
src = open(sys.argv[1] + "/xlttool/window.py", encoding="utf-8").read()
entry = src.split('DESKTOP_ENTRY = """', 1)[1].split('"""', 1)[0]
open(sys.argv[2], "w").write(entry.replace("{exec}", "xlt-tool"))
PYEOF
install -Dm644 "$APPDIR/xlt-tool.desktop" "$APPDIR/usr/share/applications/xlt-tool.desktop"
cp "$ROOT/xlttool/xlt-tool.svg" "$APPDIR/xlt-tool.svg"
install -Dm644 "$ROOT/xlttool/xlt-tool.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/xlt-tool.svg"
ln -sf xlt-tool.svg "$APPDIR/.DirIcon"
command -v desktop-file-validate >/dev/null && desktop-file-validate "$APPDIR/xlt-tool.desktop" || true

say "Smoke test"
APPDIR="$APPDIR" "$APPDIR/AppRun" --version
encoders="$("$APPDIR/usr/bin/ffmpeg" -hide_banner -encoders 2>/dev/null)"
[[ "$encoders" == *libx264* ]] && echo "    ffmpeg has libx264" || { echo "ffmpeg is missing libx264" >&2; exit 1; }
QT_QPA_PLATFORM=offscreen "$PY" -I -c "import sys; sys.path.insert(0,'$APPDIR/usr/share/xlt-tool'); \
from PySide6.QtWidgets import QApplication; a=QApplication([]); import xlttool.window, jeepney, mutagen, usb.core, libusb_package; \
assert libusb_package.get_libusb1_backend() is not None, 'no libusb'; print('    Qt, pyusb and libusb OK')"

say "Packing $(du -sh "$APPDIR" | cut -f1) AppDir"
ARCH=x86_64 "$CACHE/appimagetool" --appimage-extract-and-run --comp zstd -n "$APPDIR" "$OUT"
chmod +x "$OUT"
say "Done: $OUT ($(du -h "$OUT" | cut -f1))"
