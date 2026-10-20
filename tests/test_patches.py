"""Контракт только PostToolUse(apply_patch): scope каждого хода и сессии."""
from pathlib import Path

import pytest

from codex_auto_review.config import Config
from codex_auto_review.patches import (
    CaptureError, code_changes, event_path, parse_patch, record_patch, repo_root,
)
from codex_auto_review.store import read


def patch(path="src/main.py", before="a", after="b"):
    return (f"*** Begin Patch\n*** Update File: {path}\n@@\n"
            f"-{before}\n+{after}\n*** End Patch\n")


def payload(cmd, session="A", turn="t1", call="call1", response=None):
    return {
        "session_id": session, "turn_id": turn, "tool_use_id": call,
        "tool_name": "apply_patch", "tool_input": {"command": cmd},
        "tool_response": response if response is not None else
        "Success. Updated the following files:\nM src/main.py\n",
    }


def test_capture_success_and_deduplicate(repo: Path, tmp_path: Path) -> None:
    cmd = patch()
    item = payload(cmd)
    assert record_patch(item, repo, tmp_path)
    assert not record_patch(item, repo, tmp_path)
    changes = code_changes(repo, "A", "t1", Config(), tmp_path)
    assert len(changes) == 1
    assert changes[0].path == "src/main.py"
    assert changes[0].additions == changes[0].deletions == 1
    assert "Update File: src/main.py" in changes[0].diff
    assert len(read(event_path(repo, "A", "t1", tmp_path))["calls"]) == 1


def test_parse_add_delete_move_and_multi_file(repo: Path) -> None:
    raw = ("*** Begin Patch\n"
           "*** Add File: src/new.py\n+hello\n+world\n"
           "*** Delete File: src/old.py\n"
           "*** Update File: src/before.py\n*** Move to: src/after.py\n"
           "@@ function\n-a\n+b\n"
           "*** End Patch\n")
    changes = parse_patch(raw, repo)
    assert [f.path for f in changes] == ["src/new.py", "src/old.py", "src/after.py"]
    assert [(f.additions, f.deletions) for f in changes] == [(2, 0), (0, 0), (1, 1)]
    assert "Move to" in changes[-1].diff


def test_scope_is_per_session_and_turn_and_patch_not_git(repo, tmp_path) -> None:
    assert record_patch(payload(patch("auth.py"), "A", "one"), repo, tmp_path)
    assert record_patch(payload(patch("payments.py"), "B", "one"), repo, tmp_path)
    assert record_patch(payload(patch("other.py"), "A", "two"), repo, tmp_path)
    assert [c.path for c in code_changes(repo, "A", "one", Config(), tmp_path)] == ["auth.py"]
    assert [c.path for c in code_changes(repo, "B", "one", Config(), tmp_path)] == ["payments.py"]
    assert [c.path for c in code_changes(repo, "A", "two", Config(), tmp_path)] == ["other.py"]
    assert event_path(repo, "A", "one", tmp_path) != event_path(repo, "B", "one", tmp_path)


def test_same_file_parallel_sessions_only_own_patch(repo, tmp_path):
    record_patch(payload(patch("shared.py", "before", "A"), "A", "one"), repo, tmp_path)
    record_patch(payload(patch("shared.py", "before", "B"), "B", "one"), repo, tmp_path)
    a = code_changes(repo, "A", "one", Config(), tmp_path)[0]
    b = code_changes(repo, "B", "one", Config(), tmp_path)[0]
    assert "+A" in a.diff and "+B" not in a.diff
    assert "+B" in b.diff and "+A" not in b.diff


def test_failed_patch_not_recorded(repo, tmp_path):
    p = payload(patch(), response={"status": "failed", "output": "Failed to find expected lines"})
    assert not record_patch(p, repo, tmp_path)
    assert code_changes(repo, "A", "t1", Config(), tmp_path) == []


def test_success_with_dictionary_tool_result(repo, tmp_path):
    p = payload(patch(), response={"exit_code": 0, "output": "Success. Updated the following files:\nM src/main.py"})
    assert record_patch(p, repo, tmp_path)
    assert len(code_changes(repo, "A", "t1", Config(), tmp_path)) == 1


def test_unknown_result_reports_scope_failure(repo, tmp_path):
    assert not record_patch(payload(patch(), response={"result": "maybe"}), repo, tmp_path)
    with pytest.raises(CaptureError, match="Неподтверждённый"):
        code_changes(repo, "A", "t1", Config(), tmp_path)


def test_missing_input_and_bad_path_are_capture_errors(repo, tmp_path):
    record_patch(payload(patch("../secret.py")), repo, tmp_path)
    with pytest.raises(CaptureError, match="Некорректный патч"):
        code_changes(repo, "A", "t1", Config(), tmp_path)
    with pytest.raises(CaptureError):
        parse_patch("*** Begin Patch\n*** Add File: /etc/out.py\n+bad\n*** End Patch\n", repo)
    with pytest.raises(CaptureError):
        parse_patch("*** Begin Patch\n*** Update File: .git/config\n@@\n-x\n+y\n*** End Patch\n", repo)


def test_only_code_and_ignore_masks(repo, tmp_path):
    full = ("*** Begin Patch\n"
            "*** Update File: README.md\n@@\n-old\n+new\n"
            "*** Update File: src/main.py\n@@\n-a\n+b\n"
            "*** Add File: node_modules/dep/x.py\n+hello\n"
            "*** Add File: security/auth.py\n+safe\n"
            "*** End Patch\n")
    record_patch(payload(full), repo, tmp_path)
    assert [c.path for c in code_changes(repo, "A", "t1", Config(), tmp_path)] == [
        "src/main.py", "security/auth.py"
    ]


def test_structural_delete_and_move_are_retained(repo, tmp_path):
    deletion = "*** Begin Patch\n*** Delete File: src/legacy.py\n*** End Patch\n"
    record_patch(payload(deletion), repo, tmp_path)
    deleted = code_changes(repo, "A", "t1", Config(), tmp_path)
    assert len(deleted) == 1
    assert deleted[0].path == "src/legacy.py"
    assert deleted[0].structural
    from codex_auto_review.engine import significant
    assert significant(deleted, Config())

    move = ("*** Begin Patch\n*** Update File: src/old.py\n"
            "*** Move to: src/new.py\n@@\n-old\n+new\n*** End Patch\n")
    record_patch(payload(move, call="c2"), repo, tmp_path)
    changes = code_changes(repo, "A", "t1", Config(), tmp_path)
    assert [change.path for change in changes] == ["src/legacy.py", "src/new.py"]
    assert all(change.structural for change in changes)


def test_unknown_apply_patch_result_fails_closed_without_false_success(repo, tmp_path):
    record_patch(payload(patch(), response="Unexpected output"), repo, tmp_path)
    with pytest.raises(CaptureError, match="Неподтверждённый"):
        code_changes(repo, "A", "t1", Config(), tmp_path)


def test_no_apply_patch_and_empty_turn(repo, tmp_path):
    p = payload(patch())
    p["tool_name"] = "Bash"
    assert record_patch(p, repo, tmp_path) is False
    assert code_changes(repo, "A", "t1", Config(), tmp_path) == []


def test_repo_root_with_git_file(repo, tmp_path):
    assert repo_root(repo / "x.py") == repo.resolve()
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /nowhere")
    assert repo_root(worktree) == worktree.resolve()
