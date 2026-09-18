import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("h2h_report", Path(__file__).parents[1] / "scripts" / "h2h_report.py")
assert _spec is not None and _spec.loader is not None
h2h_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2h_report)


def test_a_direct_run_wastes_nothing() -> None:
    trace = ["fill Where from? -> executed", "click London -> executed", "read  -> executed", "read  -> executed"]
    assert h2h_report.wasted({"trace": trace}) == 0


def test_help_requests_dead_clicks_and_repeats_are_waste() -> None:
    trace = [
        "click Done -> executed",
        "click Search -> executed",
        "escalate  -> executed",
        "click Done -> executed",
        "click Search -> executed",
        "click Friday -> stale",
        "click Next -> unchanged",
    ]
    assert h2h_report.wasted({"trace": trace}) == 5


def _step(operation: str, target: str | None, url: str) -> dict[str, object]:
    return {"operation": operation, "target": target, "url": url, "outcome": "executed"}


def test_the_same_action_from_another_page_is_not_a_repeat() -> None:
    home, results = "https://pypi.org/", "https://pypi.org/search/?q=httpx"
    log = [_step("fill", "Search", home), _step("click", "Go", home), _step("fill", "Search", results)]
    assert h2h_report.wasted({"step_log": log, "trace": ["fill Search -> executed"] * 3}) == 0
    assert h2h_report.wasted({"step_log": [*log, _step("fill", "Search", results)]}) == 1


def test_an_arm_that_reports_no_steps_is_not_counted() -> None:
    assert h2h_report.wasted({"answer": "42"}) is None
