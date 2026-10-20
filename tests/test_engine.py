"""Проверки хода без Git diff и без вмешательства в чужие изменения."""
import json
from pathlib import Path
import subprocess

from codex_auto_review.appserver import ingest
from codex_auto_review.config import Config
from codex_auto_review.engine import handle, manual_review, significant
from codex_auto_review.reviewer import Finding, ReviewerError
from codex_auto_review.store import read, state_path


def event(diff: str, session: str = "s", turn: str = "t") -> dict:
    return {"method": "turn/diff/updated", "params": {
        "threadId": session, "turnId": turn, "diff": diff
    }}


def sample(file: str = "x.py", value: int = 2) -> str:
    return (f"diff --git a/{file} b/{file}\n"
            f"--- a/{file}\n+++ b/{file}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            f"+new_{value}\n")


def test_significant_with_no_critical_paths() -> None:
    from codex_auto_review.appserver import Change
    cfg = Config()
    assert not significant([Change("auth/login.py", 1, 1, "test")], cfg)
    assert significant([Change("x.py", 25, 5, "test")], cfg)
    assert significant([Change("x.py", 1, 0, "test"), Change("y.py", 1, 0, "test")], cfg)
    assert significant([Change("x.py", 1, 0, "test")], Config(mode="all"))


def test_roundtrip_blocks_then_passes_without_git_diff(monkeypatch, repo: Path,
                                                        tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    calls = []
    def reviewer(r, files, cfg, message=""):
        calls.append([f.path for f in files])
        if len(calls) == 1:
            return [Finding("P1", "x.py", 2, "Ошибка", "Возвращает неверное значение", .9)]
        return []
    monkeypatch.setattr(engine, "run_review", reviewer)
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t", "prompt": "Измени функцию"}
    assert handle("UserPromptSubmit", env, tmp_path) == {}
    ingest(event(sample()), tmp_path)
    blocked = handle("Stop", env, tmp_path)
    assert blocked["decision"] == "block"
    assert "НАЙДЕНЫ ОШИБКИ" in blocked["reason"]
    assert "файлов: 1" in blocked["reason"]
    assert "замечаний: 1 (P1: 1)" in blocked["reason"]
    assert "раунд 1/3" in blocked["reason"]
    assert handle("Stop", env, tmp_path)["systemMessage"].startswith("Ревью НЕ ПРОЙДЕНО")
    ingest(event(sample(value=3)), tmp_path)
    summary = handle("Stop", env, tmp_path)["systemMessage"]
    assert "ПРОЙДЕНО" in summary
    assert "файлов: 1" in summary
    assert "замечаний: 0 (нет)" in summary
    assert "раунд 2/3" in summary
    assert handle("Stop", env, tmp_path) == {}
    assert calls == [["x.py"], ["x.py"]]
    state = read(state_path(repo, "s", tmp_path))
    assert state["rounds"] == 2 and state["status"] == "passed"
    assert json.loads(Path(state["last_report"]).read_text())["source"] == "codex-app-server"


def test_docs_and_ignored_diff_never_invoke_review(monkeypatch, repo, tmp_path) -> None:
    import codex_auto_review.engine as engine
    monkeypatch.setattr(engine, "run_review", lambda *a: 1 / 0)
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    ingest(event(sample("README.md") + sample("node_modules/dep/x.py")), tmp_path)
    assert handle("Stop", env, tmp_path) == {}


def test_committed_git_changes_are_not_scanned(monkeypatch, repo: Path,
                                               tmp_path: Path) -> None:
    """Старый committed и новый staged код отсутствуют в App Server diff."""
    import codex_auto_review.engine as engine
    (repo / "foreign.py").write_text("other = True\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "external"], check=True,
                   capture_output=True)
    (repo / "staged.py").write_text("staged = True\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    captured = []
    monkeypatch.setattr(engine, "run_review",
                        lambda r, files, cfg, message="": captured.extend(f.path for f in files) or [])
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    ingest(event(sample("x.py")), tmp_path)
    assert "ПРОЙДЕНО" in handle("Stop", env, tmp_path)["systemMessage"]
    assert captured == ["x.py"]


def test_missing_event_is_error_never_passed(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    env = {"cwd": str(repo), "session_id": "s", "turn_id": "t"}
    handle("UserPromptSubmit", env, tmp_path)
    result = handle("Stop", env, tmp_path)
    assert "НЕ ПРОЙДЕНО" in result["systemMessage"]
    assert read(state_path(repo, "s", tmp_path))["status"] == "error"


def test_manual_review_only_uses_appserver_saved_diff(monkeypatch, repo: Path,
                                                        tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    monkeypatch.setattr(engine, "run_review", lambda *a: [])
    ingest(event(sample("x.py"), "session", "turn"), tmp_path)
    result = manual_review(repo, "session", "turn", tmp_path)
    assert result["status"] == "passed"
    assert Path(result["report"]).exists()
