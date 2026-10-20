"""Регрессии: изменение App Server diff, кеш, отказоустойчивость, фильтры."""
import json
from pathlib import Path

import pytest

from codex_auto_review.appserver import AppServerError, ingest, get_turn
from codex_auto_review.engine import handle
from codex_auto_review.reviewer import Finding, ReviewerError
from codex_auto_review.store import read, state_path


def capture(diff: str, home: Path, session: str = "s", turn: str = "t") -> None:
    ingest({"method": "turn/diff/updated", "params": {
        "threadId": session, "turnId": turn, "diff": diff
    }}, home)


def patch(value: str = "1") -> str:
    return ("diff --git a/x.py b/x.py\n"
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
            "-old\n+" + value + "\n")


def test_error_report_not_passed(monkeypatch, repo: Path, tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    def broken(*args):
        raise ReviewerError("invalid JSON")
    monkeypatch.setattr(engine, "run_review", broken)
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t", "prompt": "change"}
    handle("UserPromptSubmit", env, tmp_path)
    capture(patch(), tmp_path)
    result = handle("Stop", env, tmp_path)
    assert "НЕ ПРОЙДЕНО" in result["systemMessage"]
    state = read(state_path(repo, "s", tmp_path))
    report = json.loads(Path(state["last_report"]).read_text())
    assert state["status"] == report["status"] == "error"
    assert report["error"] == "invalid JSON"


def test_stale_appserver_diff_never_passed(monkeypatch, repo: Path,
                                          tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    capture(patch(), tmp_path)
    def change(*args):
        capture(patch("2"), tmp_path)
        return []
    monkeypatch.setattr(engine, "run_review", change)
    assert handle("Stop", env, tmp_path)["decision"] == "block"
    assert read(state_path(repo, "s", tmp_path))["status"] == "stale_review"


def test_replay_same_appserver_diff_does_not_repeat_paid_call(
    monkeypatch, repo: Path, tmp_path: Path
) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    calls = []
    def reviewer(*args):
        calls.append(1)
        return []
    monkeypatch.setattr(engine, "run_review", reviewer)
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    capture(patch(), tmp_path)
    assert "ПРОЙДЕНО" in handle("Stop", env, tmp_path)["systemMessage"]
    assert handle("Stop", env, tmp_path) == {}
    assert len(calls) == 1


def test_failed_codex_turn_cannot_pass(monkeypatch, repo: Path,
                                      tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    monkeypatch.setattr(engine, "run_review", lambda *args: 1 / 0)
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    capture(patch(), tmp_path)
    ingest({"method": "turn/completed", "params": {
        "threadId": "s", "turn": {"id": "t", "status": "failed"}
    }}, tmp_path)
    assert "НЕ ПРОЙДЕНО" in handle("Stop", env, tmp_path)["systemMessage"]


def test_no_git_fallback_if_appserver_event_missing(repo: Path, tmp_path: Path) -> None:
    (repo / "x.py").write_text("large = 1\n" * 100)
    env = {"cwd": str(repo), "session_id": "no-stream", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    response = handle("Stop", env, tmp_path)
    assert "НЕ ПРОЙДЕНО" in response["systemMessage"]
    assert read(state_path(repo, "no-stream", tmp_path))["status"] == "error"


def test_source_of_large_task_preserved(repo: Path, tmp_path: Path) -> None:
    message = "Описание задачи " * 1000
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t", "prompt": message}
    handle("UserPromptSubmit", env, tmp_path)
    assert read(state_path(repo, "s", tmp_path))["previous_message"] == message
