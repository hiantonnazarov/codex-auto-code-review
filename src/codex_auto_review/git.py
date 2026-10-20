"""Безопасные снимки Git с временным индексом и объектами дерева.

Реальный индекс и пользовательские файлы не меняются. Новые неигнорируемые файлы включаются.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile


class GitError(RuntimeError):
    pass


def run(repo: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, check=False)
    if p.returncode:
        raise GitError(f"Сбой git {' '.join(args[:3])}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def root(path: Path) -> Path:
    return Path(os.fsdecode(run(path, "rev-parse", "--show-toplevel")).strip()).resolve()


def head_tree(repo: Path) -> str:
    try:
        return run(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    except GitError:
        return run(repo, "mktree").decode().strip()


def snapshot(repo: Path) -> str:
    """Создать дерево через временный индекс, не затрагивая .git/index."""
    fd, filename = tempfile.mkstemp(prefix="codex-auto-index-")
    os.close(fd)
    try:
        Path(filename).unlink()  # git read-tree expects a not-yet-initialized index
        env = dict(os.environ, GIT_INDEX_FILE=filename, GIT_OPTIONAL_LOCKS="0")
        try:
            run(repo, "rev-parse", "--verify", "HEAD^{tree}")
        except GitError:
            run(repo, "read-tree", "--empty", env=env)
        else:
            run(repo, "read-tree", "HEAD", env=env)
        run(repo, "add", "-A", "--", ".", env=env)
        return run(repo, "write-tree", env=env).decode().strip()
    finally:
        for name in (filename, filename + ".lock"):
            try:
                Path(name).unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class Change:
    path: str
    additions: int
    deletions: int
    binary: bool = False
    metadata: bool = False  # изменение режима, переименование либо иное изменение без numstat


def changes(repo: Path, baseline: str, current: str) -> list[Change]:
    if baseline == current:
        return []
    raw = run(repo, "diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", baseline, current)
    # Формат Git --numstat -z: A\tD\tpath\0; при переименовании A\tD\t\0old\0new\0
    tokens = raw.split(b"\0")
    result: list[Change] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        parts = tokens[i].split(b"\t", 2)
        if len(parts) != 3:
            raise GitError("Неожиданный формат git numstat")
        a, d, path = parts
        i += 1
        if not path:
            if i + 1 >= len(tokens):
                raise GitError("Некорректная запись о переименовании в numstat")
            path = tokens[i + 1]
            i += 2
        binary = a == b"-" or d == b"-"
        additions, deletions = (0, 0) if binary else (int(a), int(d))
        result.append(Change(os.fsdecode(path), additions, deletions, binary,
                             not binary and additions == 0 and deletions == 0))
    # --numstat пропускает некоторые изменения атрибутов файлов (например chmod).
    # --name-only здесь является отдельной проверкой полноты списка.
    names = run(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", baseline, current)
    known = {item.path for item in result}
    for name in names.split(b"\0"):
        if name:
            path = os.fsdecode(name)
            if path not in known:
                result.append(Change(path, 0, 0, metadata=True))
                known.add(path)
    return result


def patch(repo: Path, baseline: str, current: str, path: str) -> str:
    return run(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3", baseline, current, "--", path).decode("utf-8", "replace")
