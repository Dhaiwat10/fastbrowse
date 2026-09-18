"""Diagnostic, not for merge: time the runner's first, cold Chrome launch and show where it stalls."""

import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def test_cold_chrome_start_probe() -> None:
    binary = shutil.which("google-chrome-stable") or "chrome"
    report: list[str] = []
    with tempfile.TemporaryDirectory() as profile, tempfile.TemporaryFile() as log:
        started = time.monotonic()
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--window-size=1280,900",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-popup-blocking",
                "--enable-logging=stderr",
                "--v=1",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        active = Path(profile) / "DevToolsActivePort"
        file_at = answer_at = None
        while time.monotonic() - started < 90 and proc.poll() is None:
            now = time.monotonic() - started
            if file_at is None and active.exists() and active.read_text().strip():
                file_at = now
            if file_at is not None:
                try:
                    port = int(active.read_text().split("\n")[0])
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                        answer_at = now
                        break
                except OSError, ValueError:
                    pass
            time.sleep(0.1)
        report.append(f"file_at={file_at} answer_at={answer_at} rc={proc.poll()}")
        report.append(
            subprocess.run(["ps", "-eLo", "pid,tid,stat,wchan:30,comm"], capture_output=True, text=True).stdout[-6000:]
        )
        proc.kill()
        proc.wait()
        log.seek(0)
        lines = log.read().decode(errors="replace").splitlines()
        report.append(f"{len(lines)} log lines")
        report.extend(lines[:60])
        report.append("...")
        report.extend(lines[-100:])
    raise AssertionError("\n".join(report))
