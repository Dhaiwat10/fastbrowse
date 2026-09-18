"""Diagnostic, not for merge: launch Chrome repeatedly and report start-up hangs."""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def test_chrome_start_probe() -> None:
    binary = shutil.which("google-chrome-stable") or "chrome"
    report: list[str] = [subprocess.run([binary, "--version"], capture_output=True, text=True).stdout]
    hangs = 0
    for n in range(40):
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
            while not active.exists() and proc.poll() is None and time.monotonic() - started < 20:
                time.sleep(0.05)
            took = time.monotonic() - started
            ok = active.exists()
            report.append(f"launch {n}: {'ok' if ok else 'HANG'} {took:.2f}s rc={proc.poll()}")
            if not ok:
                hangs += 1
                log.seek(0)
                report.extend(log.read().decode(errors="replace").splitlines()[-120:])
                report.append(
                    subprocess.run(["ps", "-eo", "pid,stat,etime,args"], capture_output=True, text=True).stdout
                )
            proc.kill()
            proc.wait()
    report.append(f"hangs={hangs}/40")
    raise AssertionError("\n".join(report))
