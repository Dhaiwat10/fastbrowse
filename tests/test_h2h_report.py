import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("h2h_report", Path(__file__).parents[1] / "scripts" / "h2h_report.py")
assert _spec is not None and _spec.loader is not None
h2h_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2h_report)


def test_a_direct_run_wastes_nothing() -> None:
    trace = ["fill Where from? -> executed", "click London -> executed", "read  -> executed", "read  -> executed"]
    assert h2h_report.wasted(trace) == 0


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
    assert h2h_report.wasted(trace) == 5
