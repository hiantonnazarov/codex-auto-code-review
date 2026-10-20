"""Lifecycle: original task, PostToolUse, independent review and continuation."""
from pathlib import Path
import json

from codex_auto_review.config import Config
from codex_auto_review.engine import handle, significant
from codex_auto_review.patches import Change
from codex_auto_review.reviewer import Finding
from codex_auto_review.store import read, state_path


def patch(path="x.py", value="new"):
    return f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+{value}\n*** End Patch"


def post(repo, home, session="S", turn="T", path="x.py", value="new", call="c1"):
    return handle("PostToolUse", {
        "session_id": session, "turn_id": turn, "tool_use_id": call,
        "cwd": str(repo), "tool_name": "apply_patch",
        "tool_input": {"command": patch(path, value)},
        "tool_response": "Success. Updated the following files:\nM x.py\n",
    }, home)


def prompt(repo, session="S", turn="T", value="Реализуй функцию"):
    return {"session_id": session, "turn_id": turn, "cwd": str(repo),
            "prompt": value}


def test_significant_with_no_critical_folders():
    cfg = Config()
    assert not significant([Change("auth/login.py", 1, 1, "patch")], cfg)
    assert significant([Change("x.py", 30, 0, "patch")], cfg)
    assert significant([Change("x.py", 1, 0, "patch"),
                        Change("y.py", 0, 1, "patch")], cfg)
    assert significant([Change("x.py", 1, 0, "patch")], Config(mode="all"))


def test_review_roundtrip_original_task_and_fix(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    review_calls = []

    def fake_review(root, files, cfg, original=""):
        review_calls.append((original, [f.path for f in files], files[0].diff))
        if len(review_calls) == 1:
            return [Finding("P1", "x.py", 1, "Ошибка", "Неверный результат", .9)]
        return []

    monkeypatch.setattr(engine, "run_review", fake_review)
    initial = prompt(repo, value="Добавь обязательную проверку")
    assert handle("UserPromptSubmit", initial, tmp_path) == {}
    assert post(repo, tmp_path) == {}
    answer = handle("Stop", initial, tmp_path)
    assert answer["decision"] == "block"
    assert "НАЙДЕНЫ ОШИБКИ" in answer["reason"]
    assert "раунд 1/3" in answer["reason"]

    followup = prompt(repo, turn="T2", value=answer["reason"])
    assert handle("UserPromptSubmit", followup, tmp_path) == {}
    assert post(repo, tmp_path, turn="T2", value="fixed") == {}
    approved = handle("Stop", followup, tmp_path)
    assert "ПРОЙДЕНО" in approved["systemMessage"]
    assert "раунд 2/3" in approved["systemMessage"]
    assert handle("Stop", followup, tmp_path) == {}
    assert len(review_calls) == 2
    assert all(x[0] == "Добавь обязательную проверку" for x in review_calls)
    assert "+new" in review_calls[0][2]
    assert "+fixed" in review_calls[1][2]
    state = read(state_path(repo, "S", tmp_path))
    assert state["status"] == "passed" and state["rounds"] == 2
    report = json.loads(Path(state["last_report"]).read_text(encoding="utf-8"))
    assert report["source"] == "posttooluse-apply-patch"


def test_empty_docs_and_foreign_session_do_not_invoke_reviewer(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    monkeypatch.setattr(engine, "run_review", lambda *args: 1 / 0)
    a = prompt(repo, session="A")
    b = prompt(repo, session="B")
    handle("UserPromptSubmit", a, tmp_path)
    handle("UserPromptSubmit", b, tmp_path)
    post(repo, tmp_path, session="B", path="payments.py")
    post(repo, tmp_path, session="A", path="README.md")
    assert handle("Stop", a, tmp_path) == {}
    missing = handle("Stop", prompt(repo, session="C"), tmp_path)
    assert "НЕ ПРОЙДЕНО" in missing["systemMessage"]


def test_missing_capture_fields_do_not_mark_review_success(repo, tmp_path):
    p = prompt(repo)
    handle("UserPromptSubmit", p, tmp_path)
    item = {"session_id": "S", "turn_id": "T", "cwd": str(repo),
            "tool_name": "apply_patch", "tool_input": {"command": patch()},
            "tool_response": "Success. Updated the following files:\nM x.py\n"}
    assert handle("PostToolUse", item, tmp_path) == {}
    failure = handle("Stop", p, tmp_path)
    assert "НЕ ПРОЙДЕНО" in failure["systemMessage"]


def test_small_fix_is_reviewed_after_blocking_finding(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    calls = []

    def mock_review(root, files, cfg, original):
        calls.append((original, [f.path for f in files]))
        return [Finding("P1", "x.py", 1, "Bug", "Wrong", .98)] if len(calls) == 1 else []

    monkeypatch.setattr(engine, "run_review", mock_review)
    p = prompt(repo, value="Проверь обработку ошибок")
    handle("UserPromptSubmit", p, tmp_path)
    post(repo, tmp_path, path="x.py", call="a")
    post(repo, tmp_path, path="y.py", call="b")
    first = handle("Stop", p, tmp_path)
    assert first["decision"] == "block"
    followup = prompt(repo, turn="T2", value=first["reason"])
    handle("UserPromptSubmit", followup, tmp_path)
    post(repo, tmp_path, turn="T2", path="x.py", call="c")
    second = handle("Stop", followup, tmp_path)
    assert "ПРОЙДЕНО" in second["systemMessage"]
    assert calls == [
        ("Проверь обработку ошибок", ["x.py", "y.py"]),
        ("Проверь обработку ошибок", ["x.py"]),
    ]


def test_original_task_required_for_actual_patch(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    monkeypatch.setattr(engine, "run_review", lambda *args: 1 / 0)
    initial = prompt(repo, value="")
    handle("UserPromptSubmit", initial, tmp_path)
    post(repo, tmp_path)
    outcome = handle("Stop", initial, tmp_path)
    assert "НЕ ПРОЙДЕНО" in outcome["systemMessage"]
    assert "Исходное пользовательское задание" in outcome["systemMessage"]


def test_missing_fix_does_not_silently_pass(monkeypatch, repo, tmp_path):
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\nmax_review_rounds=2\n')
    monkeypatch.setattr(engine, "run_review",
                        lambda *args: [Finding("P1", "x.py", 1, "Bug", "Fails", .99)])
    request = prompt(repo)
    handle("UserPromptSubmit", request, tmp_path)
    post(repo, tmp_path)
    first = handle("Stop", request, tmp_path)
    assert first["decision"] == "block"
    nofix = prompt(repo, turn="T2", value=first["reason"])
    handle("UserPromptSubmit", nofix, tmp_path)
    again = handle("Stop", nofix, tmp_path)
    assert again["decision"] == "block"
    last = prompt(repo, turn="T3", value=again["reason"])
    handle("UserPromptSubmit", last, tmp_path)
    outcome = handle("Stop", last, tmp_path)
    assert "НЕ ПРОЙДЕНО" in outcome["systemMessage"]
    assert "Остались блокирующие" in outcome["systemMessage"]
    assert read(state_path(repo, "S", tmp_path))["status"] == "limit_reached"


def test_no_git_scan_even_if_committed_and_staged_files(monkeypatch, repo, tmp_path):
    import subprocess
    import codex_auto_review.engine as engine
    (tmp_path / "config.toml").write_text('[review]\nmode="all"\n')
    (repo / "committed.py").write_text("c=1")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "outside"], check=True)
    (repo / "staged.py").write_text("s=1")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    captured = []
    monkeypatch.setattr(engine, "run_review",
                        lambda r, fs, cfg, msg="": captured.extend(f.path for f in fs) or [])
    p = prompt(repo)
    handle("UserPromptSubmit", p, tmp_path)
    post(repo, tmp_path)
    assert "ПРОЙДЕНО" in handle("Stop", p, tmp_path)["systemMessage"]
    assert captured == ["x.py"]
