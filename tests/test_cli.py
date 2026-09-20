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
