"""Валидатор и запуск reviewer без Git-снимков."""
import json
from pathlib import Path
import subprocess

import pytest

from codex_auto_review.patches import Change, parse_patch
from codex_auto_review.config import Config
from codex_auto_review.reviewer import Finding, ReviewerError, chunks, run_review


def diff(file: str = "x.py", body: str = "a = 1\n") -> str:
    return (f"*** Begin Patch\n*** Add File: {file}\n"
            f"+{body.rstrip()}\n*** End Patch\n")


def test_strict_findings_validation() -> None:
    data = dict(priority="P1", file="x.py", line=1, title="Bug",
                explanation="Wrong value", confidence=0.9)
    assert Finding.parse(data, {"x.py"}).priority == "P1"
    for update in ({"file": "../escape.py"}, {"confidence": float("nan")},
                   {"confidence": True}, {"line": 0}, {"priority": "P4"}):
        with pytest.raises(ReviewerError):
            Finding.parse({**data, **update}, {"x.py"})


def test_chunks_no_silent_truncation() -> None:
    data = diff(body="a=1\n") + "+a = 5\n" * 1000
    changes = [Change("x.py", 1001, 0, data)]
    with pytest.raises(ReviewerError, match="требуется"):
        chunks(changes, Config(max_diff_chars_per_chunk=1100, max_chunks=1))
    pieces = chunks(changes, Config(max_diff_chars_per_chunk=1100, max_chunks=100))
    assert len(pieces) > 1
    assert all(len(text) <= 1100 for _, text in pieces)


def test_codex_readonly_command_and_structured_output(monkeypatch, repo: Path) -> None:
    import codex_auto_review.reviewer as reviewer
    (repo / "x.py").write_text("a = 1\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        assert "--sandbox" in command and "read-only" in command
        assert "--output-schema" in command
        assert kwargs["env"]["CODEX_AUTO_REVIEW_CHILD"] == "1"
        assert "Недоверенный" not in kwargs["input"] or "Diff" in kwargs["input"]
        out = Path(command[command.index("--output-last-message") + 1])
        out.write_text(json.dumps({"summary": "ok", "findings": []}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
    assert run_review(repo, parse_patch(diff(), repo), Config()) == []
    assert len(calls) == 1


def test_integrated_review_after_multiple_chunks(monkeypatch, repo: Path) -> None:
    import codex_auto_review.reviewer as reviewer
    (repo / "x.py").write_text("a = 1\n")
    (repo / "y.py").write_text("b = 2\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["input"])
        out = Path(command[command.index("--output-last-message") + 1])
        out.write_text(json.dumps({"summary": "ok", "findings": []}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
    files = [Change("x.py", 250, 0, diff() + "+x = 1\n" * 250),
             Change("y.py", 250, 0, diff("y.py") + "+y = 1\n" * 250)]
    assert run_review(repo, files, Config(max_diff_chars_per_chunk=1100)) == []
    assert len(calls) > 2
    assert "ИНТЕГРАЦИОННОЕ" in calls[-1]


def test_absent_source_file_is_valid_for_deleted_code(repo: Path) -> None:
    from codex_auto_review.reviewer import _line_limits
    assert _line_limits(repo, {"old-file.py"}) == {"old-file.py": 1}
