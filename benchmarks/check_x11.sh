#!/usr/bin/env bash
# Run native keyboard/clipboard tests without touching the user's X11 focus.
set -eu
project_dir=$(dirname "$(dirname "$(readlink -f "$0")")")
cd "$project_dir"
if [ -d .tools/xvfb/usr/bin ]; then
    export PATH="$project_dir/.tools/xvfb/usr/bin:$PATH"
    export LD_LIBRARY_PATH="$project_dir/.tools/xvfb/usr/lib/$(uname -m)-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
if [ "${1:-}" != --inside ]; then
    exec xvfb-run -a -s '-screen 0 1280x1024x24' dbus-run-session "$0" --inside "$@"
fi
shift
export GIO_USE_VFS=local
export NO_AT_BRIDGE=1
export XDG_CONFIG_HOME="$project_dir/.tools/xvfb-config"
xfwm4 --compositor=off > .tools/xvfb-wm.log 2>&1 &
giga_test_wm=$!
cleanup() {
    if kill -0 "$giga_test_wm" 2>/dev/null; then
        kill "$giga_test_wm"
        if wait "$giga_test_wm"; then
            :
        fi
    fi
}
trap cleanup EXIT
for attempt in {1..50}; do
    property=$(xprop -root _NET_SUPPORTING_WM_CHECK)
    case "$property" in
        *'window id #'*) break ;;
    esac
    sleep 0.1
done
setxkbmap -layout us,ru
export QT_QPA_PLATFORM=xcb
export GIGA_DESKTOP_TESTS=1
./check.sh "$@"
