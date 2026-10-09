"""Receive real terminal input as data. No shell, no execution of dictated text."""

import argparse
import os
import select
import termios
import time
import tty
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--bracketed", action="store_true")
args = parser.parse_args()
output = Path(args.output)
print("talk2g test: this window records text without executing it.", flush=True)
descriptor = 0
previous = termios.tcgetattr(descriptor)
received = b""


def save_text():
    # Modern terminal applications enable bracketed paste. Keep the real byte
    # stream available while comparing only the text delivered inside markers.
    output.with_suffix(".raw").write_bytes(received)
    text = received.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"")
    output.write_text(text.decode("utf-8", errors="replace"), encoding="utf-8")


try:
    tty.setraw(descriptor)
    if args.bracketed:
        print("\x1b[?2004h", end="", flush=True)
    output.with_suffix(".ready").touch()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if not select.select([descriptor], [], [], 0.5)[0]:
            continue
        data = os.read(descriptor, 4096)
        if not data or b"\x04" in data:
            break
        received += data
        save_text()
finally:
    if args.bracketed:
        print("\x1b[?2004l", end="", flush=True)
    termios.tcsetattr(descriptor, termios.TCSANOW, previous)
    save_text()
