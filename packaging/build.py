"""Build a folder that runs without Python/uv, including offline model weights."""

import os
import platform
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from giga_dictation.config import Settings
from giga_dictation.model import prepare_models

root = Path(__file__).resolve().parents[1]
os.chdir(root)
model, vad = prepare_models(Settings())
command = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name",
    "GigaDictation",
    "--collect-all",
    "onnx_asr",
    "--collect-all",
    "onnxruntime",
    "--collect-all",
    "sounddevice",
    "--collect-submodules",
    "huggingface_hub",
    "--collect-submodules",
    "websockets",
    "--hidden-import",
    "dbus_next.aio",
    "packaging/entry.py",
]
environment = os.environ.copy()
if sys.platform == "linux":
    local_libraries = root / ".tools/system-libs/usr/lib" / f"{platform.machine()}-linux-gnu"
    if local_libraries.is_dir():
        environment["LD_LIBRARY_PATH"] = str(local_libraries) + (
            os.pathsep + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else ""
        )
subprocess.run(command, check=True, env=environment)
output = root / "dist/GigaDictation"
for source in (model, vad.parent):
    shutil.copytree(
        source, output / "models" / source.name, ignore=shutil.ignore_patterns(".cache"), dirs_exist_ok=True
    )
for name in (
    "README.md",
    "LICENSE",
    "THIRD_PARTY.md",
    "VALIDATION.md",
    "PROTOCOL.md",
    "ARCHITECTURE.md",
):
    shutil.copy2(root / name, output / name)
if (root / "packaging/licenses").is_dir():
    shutil.copytree(root / "packaging/licenses", output / "licenses/Qt", dirs_exist_ok=True)
for distribution in metadata.distributions():
    for file in distribution.files or []:
        if any(name in str(file).lower() for name in ("license", "copying", "notice")):
            source = Path(distribution.locate_file(file))
            if source.is_file():
                target = output / "licenses" / distribution.metadata["Name"] / str(file)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
print(f"Ready: {output}")
