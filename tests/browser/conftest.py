"""Fixtures for browser-layer tests: a real headless Chrome plus two-host fixture sites.

Skips the whole module cleanly when `google-chrome-stable` is not installed, since these tests exercise
real CDP mechanics (OOPIFs, downloads, dialogs) that cannot be faked without losing their signal.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio

from fastbrowse.browser.page import CdpPage
from fastbrowse.browser.session import BrowserSession
from fastbrowse.config import Config
from fastbrowse.models import Artifact, ArtifactKind, BrowserConnection

SITES = Path(__file__).parent / "sites"
CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome")

if CHROME is None:
    pytest.skip("google-chrome-stable is not installed", allow_module_level=True)


class RecordingArtifactSink:
    """Test double for `ArtifactSink`: keeps every artifact in memory for assertions."""

    def __init__(self) -> None:
        self.artifacts: list[Artifact] = []

    async def put(self, kind: ArtifactKind, name: str, mime_type: str, content: bytes) -> Artifact:
        artifact = Artifact(
            kind=kind,
            name=name,
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            uri=f"memory://{name}",
        )
        self.artifacts.append(artifact)
        return artifact


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _handler_for(directory: Path, iframe_origin: str | None = None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/download":
                body = b"fastbrowse fixture attachment bytes for download checksum test"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="spike.bin"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html") and iframe_origin is not None:
                content = (directory / "index.html").read_text().replace("__IFRAME_ORIGIN__", iframe_origin)
                body = content.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            target = directory / path.lstrip("/")
            if not target.is_file():
                self.send_response(404)
                self.end_headers()
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture(scope="session")
def iframe_site() -> Iterator[str]:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(SITES / "iframe"))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://localhost:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="session")
def main_site(iframe_site: str) -> Iterator[str]:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(SITES / "main", iframe_origin=iframe_site))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="session")
def chrome_ws_url() -> Iterator[str]:
    assert CHROME is not None
    port = _free_port()
    profile = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [
            CHROME,
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
        ws_url = ""
        for _ in range(100):
            try:
                info = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
                ws_url = json.loads(info)["webSocketDebuggerUrl"]
                break
            except OSError:
                time.sleep(0.1)
        if not ws_url:
            raise RuntimeError("Chrome did not expose a DevTools endpoint in time")
        yield ws_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


@pytest_asyncio.fixture
async def artifact_sink() -> RecordingArtifactSink:
    return RecordingArtifactSink()


@pytest_asyncio.fixture
async def browser_session(chrome_ws_url: str, artifact_sink: RecordingArtifactSink) -> AsyncIterator[BrowserSession]:
    connection = BrowserConnection(cdp_url=chrome_ws_url, remote=False)
    async with BrowserSession(connection, artifact_sink) as session:
        yield session


@pytest_asyncio.fixture
async def page(browser_session: BrowserSession) -> CdpPage:
    return CdpPage(browser_session, Config())
