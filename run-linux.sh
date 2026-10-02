#!/usr/bin/env bash
set -eu
project_dir=$(dirname "$(readlink -f "$0")")
cd "$project_dir"
if [ ! -x .venv/bin/python ]; then
    ./setup-linux.sh
fi
local_libs="$project_dir/.tools/system-libs/usr/lib/$(uname -m)-linux-gnu"
if [ -d "$local_libs" ]; then
    export LD_LIBRARY_PATH="$local_libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec .venv/bin/python -m giga_dictation "$@"
