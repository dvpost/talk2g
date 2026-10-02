#!/usr/bin/env bash
set -eu
project_dir=$(dirname "$(readlink -f "$0")")
cd "$project_dir"
mkdir -p .tools
if command -v uv > /dev/null; then
    uv_bin=$(command -v uv)
else
    curl -fLsS https://astral.sh/uv/0.11.25/install.sh -o .tools/install-uv.sh
    UV_UNMANAGED_INSTALL="$project_dir/.tools/bin" sh .tools/install-uv.sh
    uv_bin="$project_dir/.tools/bin/uv"
fi
"$uv_bin" sync --frozen --extra desktop --python 3.12
plugin=$(.venv/bin/python -c 'from pathlib import Path; import importlib.util; print(Path(importlib.util.find_spec("PySide6").origin).parent / "Qt/plugins/platforms/libqxcb.so")')
libraries=$(ldd "$plugin")
case "$libraries" in
    *'libxcb-cursor.so.0 => not found'*)
        if command -v apt-get > /dev/null; then
            mkdir -p .tools/packages .tools/system-libs
            cd .tools/packages
            apt-get download libxcb-cursor0
            for package in libxcb-cursor0_*.deb; do
                dpkg-deb -x "$package" ../system-libs
            done
            cd "$project_dir"
        else
            echo 'Install the libxcb-cursor package using your Linux package manager.' >&2
            exit 1
        fi
        ;;
esac
./run-linux.sh download
./run-linux.sh doctor
echo 'Ready. Start with ./run-linux.sh'
