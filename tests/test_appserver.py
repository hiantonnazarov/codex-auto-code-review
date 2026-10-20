"""Контракт единственного источника diff — уведомления Codex App Server."""
import json
from pathlib import Path

import pytest

from codex_auto_review.appserver import (
    AppServerError, code_changes, event_path, get_turn, ingest, parse_diff, repo_root,
)
from codex_auto_review.config import Config
from codex_auto_review.proxy import forward_output


def sample_diff(path: str = "x.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def started(thread: str, turn: str) -> dict:
    return {"method": "turn/started", "params": {
        "threadId": thread, "turn": {"id": turn, "status": "inProgress"}
    }}


def updated(thread: str, turn: str, diff: str) -> dict:
    return {"method": "turn/diff/updated", "params": {
        "threadId": thread, "turnId": turn, "diff": diff
    }}


def test_ingest_replaces_cumulative_diff_and_keeps_turns_isolated(tmp_path: Path) -> None:
    ingest(started("a", "t"), tmp_path)
    ingest(updated("a", "t", sample_diff("x.py")), tmp_path)
    ingest(updated("a", "t", sample_diff("y.py")), tmp_path)
    ingest(started("b", "t"), tmp_path)
    assert "y.py" in get_turn("a", "t", tmp_path)["diff"]
    assert "x.py" not in get_turn("a", "t", tmp_path)["diff"]
    assert get_turn("b", "t", tmp_path).get("diff") is None
    assert event_path("a", "t", tmp_path) != event_path("b", "t", tmp_path)


def test_missing_appserver_events_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(AppServerError, match="Нет событий App Server"):
        get_turn("s", "t", tmp_path)
    with pytest.raises(AppServerError, match="threadId"):
        ingest({"method": "turn/diff/updated", "params": {
            "threadId": "s", "diff": "x"
        }}, tmp_path)


def test_completed_failed_never_success(tmp_path: Path) -> None:
    ingest(started("s", "t"), tmp_path)
    ingest({"method": "turn/completed", "params": {
        "threadId": "s", "turn": {"id": "t", "status": "failed"}
    }}, tmp_path)
    with pytest.raises(AppServerError, match="failed"):
        get_turn("s", "t", tmp_path)


def test_only_code_not_documentation_or_ignored(tmp_path: Path) -> None:
    combined = (sample_diff("README.md") + sample_diff("src/app.py")
                + sample_diff("node_modules/subdep/a.py")
                + sample_diff("docs/tutorial.md") + sample_diff("security/login.py"))
    items = code_changes(combined, Config())
    assert [item.path for item in items] == ["src/app.py", "security/login.py"]
    assert all(item.additions == 1 and item.deletions == 1 for item in items)


def test_quoted_utf8_and_spaces_and_deletion() -> None:
    quoted = (
        'diff --git "a/space name.py" "b/space name.py"\n'
        '--- "a/space name.py"\n+++ "b/space name.py"\n'
        '@@ -1 +1 @@\n-a\n+b\n'
    )
    assert parse_diff(quoted)[0].path == "space name.py"
    deleted = ('diff --git a/obsolete.py b/obsolete.py\n'
               '--- a/obsolete.py\n+++ /dev/null\n'
               '@@ -1 +0,0 @@\n-deleted\n')
    assert parse_diff(deleted)[0].path == "obsolete.py"
    with pytest.raises(AppServerError):
        parse_diff("diff --git a/unsafe b/unsafe\n--- a/../escape.py\n+++ b/../escape.py\n")


def test_appserver_relay_forwards_original_rpc_bytes(tmp_path: Path) -> None:
    import io
    message = json.dumps(updated("s", "t", sample_diff()))
    original = (message + "\n" + '{"id":1,"result":{"ok":true}}\n').encode()
    result = io.BytesIO()
    forward_output(io.BytesIO(original), result, tmp_path)
    assert result.getvalue() == original
    assert get_turn("s", "t", tmp_path)["diff"] == sample_diff()


def test_repo_root_never_calls_git(repo: Path) -> None:
    assert repo_root(repo / "x.py") == repo.resolve()
    assert repo_root(repo) == repo.resolve()
