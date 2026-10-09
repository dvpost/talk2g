#!/usr/bin/env bash
set -eu
project_dir=$(dirname "$(readlink -f "$0")")
cd "$project_dir"
uv sync --frozen --extra desktop --extra dev
export LD_LIBRARY_PATH="$project_dir/.tools/system-libs/usr/lib/$(uname -m)-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
if [ "${TALK2G_DESKTOP_TESTS:-}" != 1 ]; then
    export QT_QPA_PLATFORM=offscreen
fi
uv run ruff check src tests benchmarks packaging
uv run ruff format --check src tests benchmarks packaging
uv run pytest -q "$@"
