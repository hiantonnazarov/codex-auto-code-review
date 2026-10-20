from pathlib import Path
import json

from codex_auto_review.config import Config
from codex_auto_review.engine import handle, significant, manual_review
from codex_auto_review.git import Change
from codex_auto_review.store import read, state_path
from codex_auto_review.reviewer import Finding, ReviewerError


def test_significance() -> None:
    cfg = Config()
    assert not significant([Change("a.py", 10, 2)], cfg)
    assert significant([Change("a.py", 31, 0)], cfg)
    assert significant([Change("a.py", 1, 0), Change("b.py", 1, 0)], cfg)
    assert significant([Change("src/auth/login.py", 1, 0)], cfg)
    assert significant([Change("foo.py", 1, 0)], Config(mode="all"))


def test_hook_roundtrip_fix_and_review(monkeypatch, repo: Path, tmp_path: Path) -> None:
    import codex_auto_review.engine as engine
    home = tmp_path / "settings"
    (home / "config.toml").parent.mkdir(exist_ok=True)
    (home / "config.toml").write_text('[review]\nmode="all"\nmax_review_rounds=3\n')
    calls = []
    def fake_reviewer(r, baseline, current, files, config, previous_message=""):
        calls.append(current)
        assert "x.py" in [c.path for c in files]
        if len(calls) == 1:
            return [Finding("P1", "x.py", 2, "Regressed return", "Now returns wrong number", .9)]
        return []
    monkeypatch.setattr(engine, "run_review", fake_reviewer)
    env = {"cwd": str(repo), "session_id": "session-1", "prompt": "Change function"}
    assert handle("UserPromptSubmit", env, home) == {}
    (repo / "x.py").write_text("def f():\n    return 2\n")
    blocked = handle("Stop", env, home)
    assert blocked["decision"] == "block"
    # Same tree is never reviewed twice or blocked forever.
    unchanged = handle("Stop", env, home)
    assert "unresolved" in unchanged["systemMessage"]
    assert len(calls) == 1
    (repo / "x.py").write_text("def f():\n    return 3\n")
    passed = handle("Stop", env, home)
    assert "PASSED" in passed["systemMessage"]
    assert len(calls) == 2
    assert handle("Stop", env, home) == {}
    state = read(state_path(repo, "session-1", home))
    assert state["rounds"] == 2 and state["status"] == "passed"
    assert Path(state["last_report"]).is_file()
    assert json.loads(Path(state["last_report"]).read_text())["reviewed_tree"] == calls[-1]


def test_no_review_for_docs_and_no_edits(monkeypatch, repo, tmp_path) -> None:
    import codex_auto_review.engine as engine
    monkeypatch.setattr(engine, "run_review", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should skip")))
    payload = {"cwd": str(repo), "session_id": "s"}
    assert handle("UserPromptSubmit", payload, tmp_path) == {}
    assert handle("Stop", payload, tmp_path) == {}
    (repo / "README.md").write_text("docs\n")
    assert handle("Stop", payload, tmp_path) == {}


def test_max_rounds_finishes_with_warning(monkeypatch, repo, tmp_path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\nmax_review_rounds=1\n')
    monkeypatch.setattr(engine, "run_review", lambda *a, **kw: [Finding("P2", "x.py", 1, "Wrong", "Broken", .8)])
    env = {"cwd": str(repo), "session_id": "s"}
    handle("UserPromptSubmit", env, tmp_path)
    (repo / "x.py").write_text("a=5\n")
    response = handle("Stop", env, tmp_path)
    assert "final allowed review round" in response["systemMessage"]
    assert "Wrong" in response["systemMessage"]


def test_failure_visible_not_passed(monkeypatch, repo, tmp_path) -> None:
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    monkeypatch.setattr(engine, "run_review", lambda *a, **kw: (_ for _ in ()).throw(ReviewerError("rate limited")))
    env = {"cwd": str(repo), "session_id": "s"}
    handle("UserPromptSubmit", env, tmp_path)
    (repo / "x.py").write_text("a=4\n")
    response = handle("Stop", env, tmp_path)
    assert "not passed" in response["systemMessage"]
    assert read(state_path(repo, "s", tmp_path))["status"] == "error"


def test_manual_review_includes_untracked(monkeypatch, repo, tmp_path) -> None:
    import codex_auto_review.engine as engine
    captured = []
    monkeypatch.setattr(engine, "run_review", lambda *args: captured.append(args[3]) or [])
    (repo / "y.py").write_text("y=1\n")
    result = manual_review(repo, tmp_path)
    assert result["status"] == "passed"
    assert "y.py" in [x.path for x in captured[0]]


def test_changed_during_review_never_marked_pass(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    def mutate(*args, **kwargs):
        (repo / "x.py").write_text("late change\n")
        return []
    monkeypatch.setattr(engine, "run_review", mutate)
    env = {"cwd": str(repo), "session_id": "stale"}
    handle("UserPromptSubmit", env, tmp_path)
    (repo / "x.py").write_text("initial edit\n")
    result = handle("Stop", env, tmp_path)
    assert result["decision"] == "block" and "files changed" in result["reason"]
    assert read(state_path(repo, "stale", tmp_path))["status"] == "stale_review"
