"""Local infrastructure for evals: a fixture server that records submissions, and a headless Chrome."""

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

FIXTURES = Path(__file__).with_name("fixtures")


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._posts: dict[str, list[dict[str, str]]] = {}

    def add(self, path: str, fields: dict[str, str]) -> None:
        with self._lock:
            self._posts.setdefault(path, []).append(fields)

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        with self._lock:
            return {path: list(posts) for path, posts in self._posts.items()}

    def clear(self) -> None:
        with self._lock:
            self._posts.clear()


def _handler(recorder: Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            target = FIXTURES / self.path.split("?", 1)[0].lstrip("/")
            if not target.is_file() or target.parent != FIXTURES:
                self.send_error(404)
                return
            self._send(200, target.read_bytes())

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            recorder.add(self.path, dict(parse_qsl(self.rfile.read(length).decode())))
            self._send(200, b"<!doctype html><title>Thanks</title><h1>Thanks, we received it.</h1>")

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def fixture_server() -> Generator[tuple[str, Recorder]]:
    recorder = Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), _handler(recorder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorder
    finally:
        server.shutdown()


@contextmanager
def local_chrome() -> Generator[str]:
    """Yield a DevTools WebSocket URL for a throwaway headless Chrome."""
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
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield _wait_for_ws(port)
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
