import json

import pytest

from fastbrowse import cli


def test_a_preflight_error_under_json_still_leaves_a_result_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOT_SET", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["fastbrowse", "t", "--start", "https://example.com", "--json", "--secret", "p=NOT_SET"]
    )
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "error" and result["answer"] is None and result["steps"] == []
    assert "NOT_SET" in result["error"]
    assert "NOT_SET" in str(exit_.value.code)


def test_a_preflight_error_without_json_writes_nothing_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOT_SET", raising=False)
    monkeypatch.setattr("sys.argv", ["fastbrowse", "t", "--start", "https://example.com", "--secret", "p=NOT_SET"])
    with pytest.raises(SystemExit):
        cli.main()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("flag", "value", "named"), [("--max-steps", "0", "--max-steps"), ("--max-dollars", "-1", "--max-dollars")]
)
def test_a_limit_argparse_accepts_but_the_run_cannot_use_is_refused_as_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str, value: str, named: str
) -> None:
    # `argparse` types these but does not bound them, and the model that does raises a validation error, which
    # would have reached the terminal as a traceback with nothing on stdout for a caller parsing `--json`.
    monkeypatch.setattr("sys.argv", ["fastbrowse", "t", "--start", "https://example.com", "--json", flag, value])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error" and result["steps"] == []
    assert named in result["error"] and named in str(exit_.value.code)
