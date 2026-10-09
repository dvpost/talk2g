from __future__ import annotations

import os
import sys
from pathlib import Path


def desktop_runtime() -> None:
    """Start Qt with an optional project-local XCB library on Debian/Ubuntu."""
    if sys.platform != "linux" or getattr(sys, "frozen", False):
        return
    installation = Path(__file__).resolve().parents[2]
    libraries = installation / ".tools/system-libs/usr/lib" / (os.uname().machine + "-linux-gnu")
    if not libraries.is_dir() or str(libraries) in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
        return
    os.environ["LD_LIBRARY_PATH"] = str(libraries) + (
        ":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""
    )
    os.execv(sys.executable, [sys.executable, "-m", "talk2g", *sys.argv[1:]])
