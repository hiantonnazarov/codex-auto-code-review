"""Regression coverage: failure paths, no duplicate reviews and round caps."""
from pathlib import Path
import json

from codex_auto_review.engine import handle
from codex_auto_review.reviewer import ReviewerError, Finding
from codex_auto_review.store import read, state_path


def hook(repo, event, session="s", turn="t", task="Почини ошибку", value="new", call="c1"):
    base = {"cwd": str(repo), "session_id": session, "turn_id": turn}
    if event == "UserPromptSubmit":
        base["prompt"] = task
    if event == "PostToolUse":
        base.update(tool_name="apply_patch", tool_use_id=call,
                    tool_input={"command": "*** Begin Patch\n*** Update File: x.py\n"
                                            f"@@\n-old\n+{value}\n*** End Patch\n"},
                    tool_response={"output": "Success. Updated the following files:\nM x.py\n",
                                   "exit_code": 0})
    return base


def test_reviewer_error_is_reported_not_passed(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    monkeypatch.setattr(engine, "run_review", lambda *a: (_ for _ in ()).throw(
        ReviewerError("bad JSON")
    ))
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit"), tmp_path)
    handle("PostToolUse", hook(repo, "PostToolUse"), tmp_path)
    result = handle("Stop", hook(repo, "Stop"), tmp_path)
    assert "НЕ ПРОЙДЕНО" in result["systemMessage"]
    state = read(state_path(repo, "s", tmp_path))
    assert state["status"] == "error"
    report = json.loads(Path(state["last_report"]).read_text(encoding="utf-8"))
    assert report["error"] == "bad JSON"


def test_new_patch_while_review_in_progress_is_not_accepted(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit"), tmp_path)
    handle("PostToolUse", hook(repo, "PostToolUse"), tmp_path)
    def add_more(*args):
        handle("PostToolUse", hook(repo, "PostToolUse", call="c2", value="other"), tmp_path)
        return []
    monkeypatch.setattr(engine, "run_review", add_more)
    result = handle("Stop", hook(repo, "Stop"), tmp_path)
    assert result["decision"] == "block"
    assert read(state_path(repo, "s", tmp_path))["status"] == "stale_review"


def test_retry_same_patch_not_charged_again(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    calls = []
    monkeypatch.setattr(engine, "run_review", lambda *args: calls.append(1) or [])
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit"), tmp_path)
    handle("PostToolUse", hook(repo, "PostToolUse"), tmp_path)
    assert "ПРОЙДЕНО" in handle("Stop", hook(repo, "Stop"), tmp_path)["systemMessage"]
    assert handle("Stop", hook(repo, "Stop"), tmp_path) == {}
    assert len(calls) == 1


def test_unknown_tool_response_does_not_pass(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    monkeypatch.setattr(engine, "run_review", lambda *a: 1 / 0)
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit"), tmp_path)
    p = hook(repo, "PostToolUse")
    p["tool_response"] = {"message": "unknown"}
    handle("PostToolUse", p, tmp_path)
    result = handle("Stop", hook(repo, "Stop"), tmp_path)
    assert "НЕ ПРОЙДЕНО" in result["systemMessage"]
    assert read(state_path(repo, "s", tmp_path))["status"] == "error"


def test_round_budget_persists_with_auto_continuation(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text(
        '[review]\nmode="all"\nmax_review_rounds=2\n')
    seen = []
    def reviewer(root, files, cfg, message):
        seen.append(message)
        return [Finding("P1", "x.py", 1, "Bug", "Wrong output", .99)]
    monkeypatch.setattr(engine, "run_review", reviewer)
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit", task="Оригинал"), tmp_path)
    handle("PostToolUse", hook(repo, "PostToolUse"), tmp_path)
    response = handle("Stop", hook(repo, "Stop"), tmp_path)
    assert response["decision"] == "block"
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit", turn="t2",
                                    task=response["reason"]), tmp_path)
    handle("PostToolUse", hook(repo, "PostToolUse", turn="t2", value="fixed"), tmp_path)
    final = handle("Stop", hook(repo, "Stop", turn="t2"), tmp_path)
    assert "последний раунд" in final["systemMessage"]
    assert seen == ["Оригинал", "Оригинал"]


def test_original_task_is_not_truncated(repo, tmp_path):
    big = "Напиши код " * 700
    handle("UserPromptSubmit", hook(repo, "UserPromptSubmit", task=big), tmp_path)
    assert read(state_path(repo, "s", tmp_path))["previous_message"] == big
