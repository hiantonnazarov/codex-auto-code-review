"""Атомарное хранение состояния и межпроцессная блокировка без удаления активных lock-файлов."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Iterator
from uuid import uuid4

from .config import HOME_DIR


def state_path(repo: Path, session: str, home: Path | None = None) -> Path:
    key = sha256(f"{repo.resolve()}\0{session}".encode()).hexdigest()[:32]
    return (home or HOME_DIR) / "state" / (key + ".json")


def read(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Файл состояния повреждён: {path}")
    return value


def write(path: Path, payload: dict) -> None:
    """Атомарная замена; отдельный временный файл для каждого писателя."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def locked(path: Path, max_wait_seconds: float = 30.0) -> Iterator[None]:
    """Системная блокировка освобождается ОС после аварийного завершения процесса.

    Lock-файл намеренно остаётся на диске: удаление активного lock-файла
    допускает одновременные блокировки на разных inode (и гонку ревью).
    """
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    deadline = time.monotonic() + max_wait_seconds
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not acquired:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Сессия уже заблокирована другим ревью") from None
                    time.sleep(0.1)
        else:
            import fcntl
            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Сессия уже заблокирована другим ревью") from None
                    time.sleep(0.1)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
