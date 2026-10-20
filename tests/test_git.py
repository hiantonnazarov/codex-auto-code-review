from pathlib import Path
import subprocess

from codex_auto_review.git import snapshot, changes, head_tree, root, patch


def test_snapshot_includes_untracked_without_touching_index(repo: Path) -> None:
    before_index = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-s"])
    base = snapshot(repo)
    (repo / "x.py").write_text("def f():\n    return 10\n")
    (repo / "untracked.py").write_text("a = 1\n")
    after = snapshot(repo)
    result = changes(repo, base, after)
    assert {x.path for x in result} == {"x.py", "untracked.py"}
    assert sum(x.additions for x in result) >= 2
    assert "return 10" in patch(repo, base, after, "x.py")
    assert subprocess.check_output(["git", "-C", str(repo), "ls-files", "-s"]) == before_index
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]).strip()


def test_index_staged_changes_and_deleted_files(repo: Path) -> None:
    baseline = snapshot(repo)
    (repo / "x.py").unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-u"], check=True)
    current = snapshot(repo)
    result = changes(repo, baseline, current)
    assert len(result) == 1
    assert result[0].deletions == 2
    assert root(repo) == repo.resolve()
    assert head_tree(repo) == baseline


def test_git_rename_numstat(repo: Path) -> None:
    before = snapshot(repo)
    (repo / "x.py").rename(repo / "y.py")
    result = changes(repo, before, snapshot(repo))
    assert result
    assert "y.py" in {c.path for c in result}
