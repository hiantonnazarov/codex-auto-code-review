"""Регрессии по контрактам FR-03/08/12/16/17/18/21/22/25."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from codex_auto_review.config import Config
from codex_auto_review.engine import handle, significant
from codex_auto_review.git import Change, changes, snapshot
from codex_auto_review.reviewer import Finding, ReviewerError, run_review
from codex_auto_review.store import locked, read, state_path


def test_turn_id_duplicate_does_not_move_baseline(repo: Path, tmp_path: Path) -> None:
    payload = {"cwd": str(repo), "session_id": "double", "turn_id": "turn-1", "prompt": "изменить"}
    assert handle("UserPromptSubmit", payload, tmp_path) == {}
    before = read(state_path(repo, "double", tmp_path))["baseline"]
    (repo / "x.py").write_text("changed = True\n")
    assert handle("UserPromptSubmit", payload, tmp_path) == {}
    assert read(state_path(repo, "double", tmp_path))["baseline"] == before


def test_continuation_preserves_initial_baseline(monkeypatch, repo: Path, tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n', encoding="utf-8")
    monkeypatch.setattr(engine, "run_review", lambda *args, **kw: [
        Finding("P1", "x.py", 1, "Ошибка", "Плохая функция", 0.9)
    ])
    prompt = {"cwd": str(repo), "session_id": "continuation", "turn_id": "first", "prompt": "исправь"}
    handle("UserPromptSubmit", prompt, tmp_path)
    original = read(state_path(repo, "continuation", tmp_path))["baseline"]
    (repo / "x.py").write_text("error = 1\n")
    response = handle("Stop", prompt, tmp_path)
    assert response["decision"] == "block"
    continued = {**prompt, "turn_id": "synthetic", "prompt": response["reason"]}
    assert handle("UserPromptSubmit", continued, tmp_path) == {}
    assert read(state_path(repo, "continuation", tmp_path))["baseline"] == original
    assert "Ревью НЕ ПРОЙДЕНО" in handle("Stop", continued, tmp_path)["systemMessage"]


def test_review_metadata_significant(repo: Path) -> None:
    assert significant([Change("source.py", 0, 0, metadata=True)], Config())
    if os.name == "nt":
        pytest.skip("Git chmod tests are POSIX-specific")
    before = snapshot(repo)
    path = repo / "x.py"
    path.chmod(path.stat().st_mode ^ 0o111)
    after = snapshot(repo)
    modified = changes(repo, before, after)
    assert modified and modified[0].metadata


def test_out_of_range_and_nonfinite_findings(repo: Path) -> None:
    from codex_auto_review.reviewer import _validate_result
    data = {"priority": "P1", "file": "x.py", "line": 99, "title": "Bug",
            "explanation": "Concrete failure", "confidence": 0.9}
    with pytest.raises(ReviewerError, match="Номер строки"):
        _validate_result({"summary": "", "findings": [data]}, {"x.py"}, {"x.py": 2})
    for confidence in (float("nan"), float("inf"), True):
        with pytest.raises(ReviewerError):
            Finding.parse({**data, "confidence": confidence}, {"x.py"})


def test_error_creates_report(monkeypatch, repo: Path, tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n', encoding="utf-8")
    def broken(*args, **kwargs):
        raise ReviewerError("malformed JSON")
    monkeypatch.setattr(engine, "run_review", broken)
    env = {"cwd": str(repo), "session_id": "report-error", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    (repo / "x.py").write_text("a = 3\n")
    result = handle("Stop", env, tmp_path)
    assert "не пройдена" in result["systemMessage"]
    state = read(state_path(repo, "report-error", tmp_path))
    report = json.loads(Path(state["last_report"]).read_text(encoding="utf-8"))
    assert state["status"] == report["status"] == "error"
    assert report["error"] == "malformed JSON"
    assert report["reviewed_tree"] == state["last_current"]


def test_cross_file_integration_pass(monkeypatch, repo: Path) -> None:
    import codex_auto_review.reviewer as rev
    base = snapshot(repo)
    (repo / "x.py").write_text("a = 1\n" * 220, encoding="utf-8")
    (repo / "y.py").write_text("b = 2\n" * 220, encoding="utf-8")
    head = snapshot(repo)
    selected = changes(repo, base, head)
    calls: list[str] = []
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        calls.append(kwargs["input"])
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({"summary": "ok", "findings": []}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rev.subprocess, "run", fake_run)
    cfg = Config(max_diff_chars_per_chunk=1100, max_chunks=100)
    assert run_review(repo, base, head, selected, cfg) == []
    assert len(calls) > 2
    assert "ИНТЕГРАЦИОННОЕ ревью" in calls[-1]
    assert "x.py" in calls[-1] and "y.py" in calls[-1]


def test_os_lock_rejects_concurrent_owner(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ready = threading.Event()
    release = threading.Event()
    failures: list[Exception] = []

    def second() -> None:
        ready.set()
        try:
            with locked(path, max_wait_seconds=0.02):
                pass
        except Exception as exc:
            failures.append(exc)

    with locked(path):
        t = threading.Thread(target=second)
        t.start()
        assert ready.wait(3)
        t.join(timeout=3)
        assert not t.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], RuntimeError)
    with locked(path, max_wait_seconds=0.02):
        pass  # ОС освободила блокировку


def test_fail_closed_error_stops_automatic_retry_loop(monkeypatch, repo: Path, tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text(
        '[review]\nmode="all"\nfail_open=false\n', encoding="utf-8"
    )
    calls = []
    def broken(*args, **kwargs):
        calls.append(1)
        raise ReviewerError("unavailable")
    monkeypatch.setattr(engine, "run_review", broken)
    payload = {"cwd": str(repo), "session_id": "closed", "turn_id": "first", "prompt": "изменить"}
    handle("UserPromptSubmit", payload, tmp_path)
    original = read(state_path(repo, "closed", tmp_path))["baseline"]
    (repo / "x.py").write_text("x = 5\n")
    first = handle("Stop", payload, tmp_path)
    assert first["decision"] == "block"
    continuation = {**payload, "turn_id": "cont", "prompt": first["reason"]}
    handle("UserPromptSubmit", continuation, tmp_path)
    second = handle("Stop", continuation, tmp_path)
    assert second["continue"] is False
    assert "НЕ ПРОЙДЕНО" in second["systemMessage"]
    assert len(calls) == 1
    assert read(state_path(repo, "closed", tmp_path))["baseline"] == original


def test_doctor_tracks_observed_stop(monkeypatch, tmp_path: Path) -> None:
    import codex_auto_review.cli as cli
    monkeypatch.setattr(cli, "HOME_DIR", tmp_path)
    (tmp_path / "hook-events.json").write_text(
        json.dumps({"UserPromptSubmit": "2026-10-20", "Stop": "2026-10-20"}),
        encoding="utf-8"
    )
    info = cli._doctor()["hook_events_observed"]
    assert info["stop_delivery_confirmed"] is True
    assert info["last_Stop"] == "2026-10-20"


def test_preexisting_uncommitted_code_is_out_of_review_scope(
    monkeypatch, repo: Path, tmp_path: Path
) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n', encoding="utf-8")
    (repo / "x.py").write_text("already = 1\n")  # Изменение ДО пользовательского хода
    env = {"cwd": str(repo), "session_id": "existing", "turn_id": "first", "prompt": "Новый файл"}
    handle("UserPromptSubmit", env, tmp_path)
    (repo / "new.py").write_text("new = 2\n")
    captured = []
    monkeypatch.setattr(engine, "run_review", lambda r, b, h, files, *args: captured.extend(files) or [])
    assert "ПРОЙДЕНО" in handle("Stop", env, tmp_path)["systemMessage"]
    assert [c.path for c in captured] == ["new.py"]


def test_binary_edit_significant_under_default_policy(repo: Path) -> None:
    before = snapshot(repo)
    (repo / "binary.py").write_bytes(b"\x00\x01\x02")
    actual = changes(repo, before, snapshot(repo))
    assert len(actual) == 1 and actual[0].binary
    assert significant(actual, Config())


def test_ownership_detection_does_not_delete_foreign_commands() -> None:
    from codex_auto_review.cli import _owned
    assert not _owned({"hooks": [{"type": "command",
                                 "command": "echo 'codex_auto_review.cli hook Stop'"}]})
    assert not _owned({"hooks": None})
    assert _owned({"hooks": [{"type": "command",
                             "command": "python -m codex_auto_review.cli hook Stop"}]})
