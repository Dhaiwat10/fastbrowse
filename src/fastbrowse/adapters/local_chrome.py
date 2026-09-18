"""Chrome on this machine: headless with a throwaway profile by default."""

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager, nullcontext

from fastbrowse.models import BrowserConnection, LocalChrome


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_chrome(override: str | None) -> str | None:
    """`override` is `Settings.chrome`, a name or path that replaces discovery rather than joining it."""
    if override:
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
async def async_local_chrome(options: LocalChrome) -> AsyncGenerator[BrowserConnection]:
    manager = local_chrome(options)
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
def local_chrome(options: LocalChrome) -> Generator[BrowserConnection]:
    """Yield a connection to a Chrome launched with `options`, killed on exit."""
    binary = find_chrome(options.binary)
    if binary is None:
        raise RuntimeError("Chrome is not installed")
    port = free_port()
    kept = options.profile
    with (
        nullcontext(str(kept.expanduser()))
        if kept
        else tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile
    ):
        proc = subprocess.Popen(
            [
                binary,
                *(() if options.headed else ("--headless=new",)),
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
