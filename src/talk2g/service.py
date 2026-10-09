from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .config import Settings, project_home


def health(url: str) -> dict:
    address = urlsplit(url)
    scheme = "https" if address.scheme == "wss" else "http"
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(f"{scheme}://{address.netloc}/health"), timeout=2) as response:
        result = json.load(response)
    if result.get("app") != "talk2g" or result.get("protocol") != 1:
        raise ValueError("На этом адресе работает другой сервис")
    return result


class LocalService:
    def __init__(self, settings: Settings, home: Path | None = None):
        self.settings = settings
        self.home = home or project_home()
        self.process: subprocess.Popen | None = None
        self.log_file = None

    def start(self) -> None:
        address = urlsplit(self.settings.server_url)
        if address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost", "::1"):
            return
        try:
            health(self.settings.server_url)
            return
        except URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError):
                raise
        self.home.joinpath(".data").mkdir(parents=True, exist_ok=True)
        self.log_file = self.home.joinpath(".data/server.log").open("a", encoding="utf-8")
        prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "talk2g"]
        command = prefix + [
            "server",
            "--host",
            "::1" if address.hostname == "::1" else "127.0.0.1",
            "--port",
            str(address.port or 80),
            "--threads",
            str(self.settings.threads),
            "--model",
            self.settings.model,
        ]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.process = subprocess.Popen(
            command, cwd=self.home, stdout=self.log_file, stderr=subprocess.STDOUT, **options
        )

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log_file:
            self.log_file.close()
