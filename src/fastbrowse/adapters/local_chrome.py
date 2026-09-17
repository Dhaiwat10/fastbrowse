"""A throwaway headless Chrome on this machine."""

import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager

from fastbrowse.models import BrowserConnection


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def local_chrome() -> Generator[BrowserConnection]:
    """Yield a connection to a headless Chrome with a fresh profile, killed on exit."""
    binary = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if binary is None:
        raise RuntimeError("Chrome is not installed")
    port = free_port()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-popup-blocking",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield BrowserConnection(cdp_url=_wait_for_ws(port), live_url=None, remote=False)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def _wait_for_ws(port: int, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                return str(json.load(response)["webSocketDebuggerUrl"])
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
