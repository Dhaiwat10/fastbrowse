"""A throwaway headless Chrome on this machine."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from fastbrowse.models import BrowserConnection


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_chrome() -> str | None:
    if override := os.environ.get("FASTBROWSE_CHROME"):
        return shutil.which(override)
    for name in (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if binary := shutil.which(name):
            return binary
    return None


@asynccontextmanager
async def async_local_chrome() -> AsyncGenerator[BrowserConnection]:
    manager = local_chrome()
    opening = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        try:
            connection = await asyncio.shield(opening)
        finally:
            # Threads cannot be cancelled; wait for startup before attempting to release its process.
            await asyncio.gather(opening, return_exceptions=True)
        yield connection
    finally:
        closing = asyncio.create_task(asyncio.to_thread(manager.__exit__, None, None, None))
        try:
            await asyncio.shield(closing)
        finally:
            await asyncio.gather(closing, return_exceptions=True)


@contextmanager
def local_chrome() -> Generator[BrowserConnection]:
    """Yield a connection to a headless Chrome with a fresh profile, killed on exit."""
    binary = find_chrome()
    if binary is None:
        raise RuntimeError("Chrome is not installed")
    port = free_port()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                # Headless defaults to 800x600, where responsive sites collapse their header into a
                # toggle and the control the agent needs is not in the page at all.
                "--window-size=1280,900",
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
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


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
