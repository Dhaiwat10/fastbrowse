"""Run one task from the terminal.

    fastbrowse "What is the latest version of httpx?" --start https://pypi.org/
    fastbrowse "Send the form" --start https://example.com/contact --authorize --cloud --json

A local headless Chrome by default; `--headed` shows it, and `--profile DIR` keeps its profile so a site
signed into there once stays signed in. `--cloud` runs on a Browser Use Cloud browser (BROWSER_USE_API_KEY)
and prints where to watch it live.
Secrets come from `--secret NAME=ENV_VAR`: the value is read from that variable, usable only on the start origin.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from fastbrowse.clients.environment import ConfigurationError, load_settings
from fastbrowse.models import Authorization, BrowserEvent, Limits, LocalChrome, StepEvent
from fastbrowse.run import run_task
from fastbrowse.safety import ScopedSecrets, origin_of


def _secrets(pairs: list[tuple[str, str]], start: str) -> ScopedSecrets | None:
    """Values read from the named variables now, so a missing one fails before a browser is opened."""
    if not pairs:
        return None
    missing = [variable for _, variable in pairs if variable not in os.environ]
    if missing:
        raise ConfigurationError(f"--secret names unset variables: {', '.join(missing)}")
    return ScopedSecrets({name: os.environ[variable] for name, variable in pairs}, origin_of(start))


def _secret(pair: str) -> tuple[str, str]:
    name, sep, variable = pair.partition("=")
    if not (sep and name and variable):
        raise argparse.ArgumentTypeError(f"expected NAME=ENV_VAR, got {pair!r}")
    return name, variable


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fastbrowse", description="Run one browser task.")
    parser.add_argument("task")
    parser.add_argument("--start", required=True, help="URL to open before the task starts")
    parser.add_argument("--cloud", action="store_true", help="use a Browser Use Cloud browser")
    parser.add_argument("--headed", action="store_true", help="show the local Chrome window")
    parser.add_argument("--profile", type=Path, default=None, help="Chrome profile directory kept between runs")
    parser.add_argument("--authorize", action="store_true", help="allow submit/pay/delete/send without pausing")
    parser.add_argument("--secret", action="append", default=[], type=_secret, metavar="NAME=ENV_VAR")
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--max-dollars", type=float, default=None)
    parser.add_argument("--downloads", type=Path, default=None, help="directory for downloaded files")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    return parser.parse_args(argv)


def _browser_key(cloud: bool) -> str | None:
    if not cloud:
        return None
    return load_settings().browser_key()


def _chrome(headed: bool, profile: Path | None) -> LocalChrome:
    """The flags add to FASTBROWSE_HEADED and FASTBROWSE_PROFILE; they cannot unset them."""
    chrome = load_settings().local_chrome()
    return chrome.model_copy(update={"headed": headed or chrome.headed, "profile": profile or chrome.profile})


async def _print_step(event: StepEvent | BrowserEvent) -> None:
    if isinstance(event, BrowserEvent):
        if event.live_url:
            print(f"  watch live: {event.live_url}", file=sys.stderr)
        return
    step = event.step
    print(f"  {step.index:>2} {step.operation.value} {step.target or ''} -> {step.outcome.value}", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    result = await run_task(
        args.task,
        start=args.start,
        browser_api_key=_browser_key(args.cloud),
        chrome=_chrome(args.headed, args.profile),
        secrets=_secrets(args.secret, args.start),
        limits=Limits(max_steps=args.max_steps, max_dollars=args.max_dollars),
        authorization=Authorization(irreversible_actions=args.authorize),
        downloads=args.downloads,
        on_event=_print_step,
    )
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(f"{result.status.value} (${result.cost.known_dollars:.4f}, {len(result.steps)} steps)")
        print(result.answer or result.error or "")
    return 0 if result.succeeded else 1


def main() -> None:
    args = _parse(sys.argv[1:])
    try:
        sys.exit(asyncio.run(run(args)))
    except ConfigurationError as exc:
        sys.exit(f"fastbrowse: {exc}")
