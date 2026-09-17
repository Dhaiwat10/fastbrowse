"""Run one task from the terminal.

    fastbrowse "What is the latest version of httpx?" --start https://pypi.org/
    fastbrowse "Send the form" --start https://example.com/contact --authorize --cloud --json

A local headless Chrome by default; `--cloud` runs on a Browser Use Cloud browser (BROWSER_USE_API_KEY).
Secrets come from `--secret NAME=ENV_VAR`: the value is read from that variable, usable only on the start origin.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from fastbrowse.clients.environment import MissingKeyError
from fastbrowse.models import Authorization, Limits, RunEvent, SecretRef, StepEvent
from fastbrowse.run import run_task
from fastbrowse.safety import origin_of


class EnvironmentSecrets:
    def __init__(self, names: dict[str, str], origin: str) -> None:
        self._names = names
        self._origin = origin

    def available(self) -> tuple[SecretRef, ...]:
        return tuple(SecretRef(name=name, origins=(self._origin,)) for name in self._names)

    async def resolve(self, name: str, origin: str) -> str | None:
        variable = self._names.get(name)
        return os.environ.get(variable) if variable and origin == self._origin else None


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fastbrowse", description="Run one browser task.")
    parser.add_argument("task")
    parser.add_argument("--start", required=True, help="URL to open before the task starts")
    parser.add_argument("--cloud", action="store_true", help="use a Browser Use Cloud browser")
    parser.add_argument("--authorize", action="store_true", help="allow submit/pay/delete/send without pausing")
    parser.add_argument("--secret", action="append", default=[], metavar="NAME=ENV_VAR")
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--max-dollars", type=float, default=None)
    parser.add_argument("--downloads", type=Path, default=None, help="directory for downloaded files")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    return parser.parse_args(argv)


def _browser_key(cloud: bool) -> str | None:
    if not cloud:
        return None
    key = os.environ.get("BROWSER_USE_API_KEY")
    if not key:
        raise MissingKeyError("set BROWSER_USE_API_KEY for --cloud")
    return key


async def _print_step(event: RunEvent) -> None:
    if isinstance(event, StepEvent):
        step = event.step
        print(f"  {step.index:>2} {step.operation.value} {step.target or ''} -> {step.outcome.value}", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    names = dict(pair.split("=", 1) for pair in args.secret)
    result = await run_task(
        args.task,
        start=args.start,
        browser_api_key=_browser_key(args.cloud),
        secrets=EnvironmentSecrets(names, origin_of(args.start)) if names else None,
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
    except MissingKeyError as exc:
        sys.exit(f"fastbrowse: {exc}")
