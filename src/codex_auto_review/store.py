"""Атомарное сохранение состояния сессии с блокировкой от дублирующих ревью."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Iterator

from .config import HOME_DIR


def state_path(repo: Path, session: str, home: Path | None = None) -> Path:
    key = sha256(f"{repo.resolve()}\0{session}".encode()).hexdigest()[:32]
    return (home or HOME_DIR) / "state" / (key + ".json")


def read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def locked(path: Path, stale_seconds: int = 1800) -> Iterator[None]:
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Нельзя снимать действующую блокировку только потому, что ревьюер работает долго.
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            age = 0
        if age > stale_seconds:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        else:
            raise RuntimeError("В этой сессии уже выполняется ревью") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)
