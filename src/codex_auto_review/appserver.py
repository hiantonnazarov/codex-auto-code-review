"""Только данные Codex App Server: хранение последнего агрегированного diff хода.

Никаких вызовов Git для определения изменённых файлов или проверки состояния коммитов.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .config import HOME_DIR, Config, is_code
from .store import locked, read, write


class AppServerError(RuntimeError):
    """Нет достоверного события или оно нарушает контракт."""


@dataclass(frozen=True)
class Change:
    path: str
    additions: int
    deletions: int
    diff: str
    binary: bool = False


def repo_root(start: Path) -> Path:
    """Найти каталог Git-проекта без чтения Git-индекса или diff."""
    path = start.resolve()
    if path.is_file():
        path = path.parent
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    raise AppServerError(f"Не найден корень Git-проекта: {start}")


def event_path(thread_id: str, turn_id: str, home: Path | None = None) -> Path:
    if not thread_id or not turn_id:
        raise AppServerError("В событии обязательны threadId и turnId")
    digest = sha256(json.dumps([thread_id, turn_id]).encode()).hexdigest()
    return (home or HOME_DIR) / "appserver" / (digest + ".json")


def _ids(message: dict) -> tuple[str, str]:
    params = message.get("params")
    if not isinstance(params, dict):
        raise AppServerError("App Server: params должен быть объектом")
    thread = params.get("threadId")
    turn = params.get("turnId")
    if message.get("method") in ("turn/started", "turn/completed"):
        obj = params.get("turn")
        if not isinstance(obj, dict):
            raise AppServerError("App Server: нет объекта turn")
        turn = obj.get("id")
    if not isinstance(thread, str) or not thread or not isinstance(turn, str) or not turn:
        raise AppServerError("App Server: отсутствует threadId или идентификатор turn")
    return thread, turn


def ingest(message: dict, home: Path | None = None) -> bool:
    """Сохранить состояние только протокольных turn-событий App Server.

    diff/updated содержит ПОЛНЫЙ последний diff, поэтому предыдущее значение
    замещается, а не конкатенируется. Ответы RPC и другие уведомления игнорируются.
    """
    method = message.get("method")
    if method not in {"turn/started", "turn/diff/updated", "turn/completed"}:
        return False
    thread, turn = _ids(message)
    params = message["params"]
    path = event_path(thread, turn, home)
    with locked(path):
        state = read(path)
        state.update(source="codex-app-server", thread_id=thread, turn_id=turn)
        if method == "turn/started":
            state.setdefault("observed_start", True)
        elif method == "turn/diff/updated":
            diff = params.get("diff")
            if not isinstance(diff, str):
                raise AppServerError("turn/diff/updated: поле diff должно быть строкой")
            state["diff"] = diff
            state["observed_start"] = True
        else:
            turn_status = params["turn"].get("status")
            if turn_status not in ("completed", "interrupted", "failed"):
                raise AppServerError(f"Недопустимый статус завершения: {turn_status}")
            state["completed_status"] = turn_status
            state["observed_start"] = True
        write(path, state)
    return True


def get_turn(thread_id: str, turn_id: str, home: Path | None = None) -> dict:
    state = read(event_path(thread_id, turn_id, home))
    if not state.get("observed_start") or state.get("source") != "codex-app-server":
        raise AppServerError(
            "Нет событий App Server для этого хода: требуется клиент/relay, "
            "передающий turn/started и turn/diff/updated. Из Git ничего не восстанавливается."
        )
    if state.get("completed_status") in ("failed", "interrupted"):
        raise AppServerError(f"Ход Codex завершился с ошибкой: {state['completed_status']}")
    return state


def _path_from_header(line: str) -> str | None:
    """Путь из +++/---, включая пробелы и Git C-style quoted paths."""
    value = line[4:].rstrip("\r\n")
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        # Git экранирует пути как C-строки (включая \ddd для UTF-8 байтов).
        match = re.fullmatch(r'"((?:\\.|[^"\\])*)"(?:\t.*)?', value)
        if not match:
            raise AppServerError("Некорректный экранированный путь в unified diff")
        raw = match.group(1)
        data = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] != "\\":
                data.extend(raw[i].encode("utf-8"))
                i += 1
                continue
            i += 1
            if i >= len(raw):
                raise AppServerError("Незавершённая escape-последовательность Git")
            c = raw[i]
            if c in "01234567" and i + 2 < len(raw) and all(
                ch in "01234567" for ch in raw[i:i + 3]
            ):
                data.append(int(raw[i:i + 3], 8))
                i += 3
            else:
                data.extend({ "t": b"\t", "n": b"\n", "r": b"\r",
                              '"': b'"', "\\": b"\\" }.get(c, c.encode()))
                i += 1
        value = data.decode("utf-8", errors="strict")
    else:
        value = value.split("\t", 1)[0]
    if not value.startswith(("a/", "b/")):
        raise AppServerError(f"Недопустимый заголовок пути в unified diff: {value!r}")
    name = value[2:].replace("\\", "/")
    if not name or name.startswith("/") or "\0" in name or any(
        part in ("", ".", "..") for part in name.split("/")
    ) or re.match(r"^[A-Za-z]:", name):
        raise AppServerError(f"Недопустимый путь изменённого файла: {name!r}")
    return name


def parse_diff(diff: str) -> list[Change]:
    """Разделить unified diff App Server по файлам; не брать данные из Git."""
    if not isinstance(diff, str):
        raise AppServerError("Ожидалась строка unified diff")
    if not diff.strip():
        return []
    lines = diff.splitlines(keepends=True)
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git ") and current:
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)

    # Некоторые клиенты предоставляют unified diff без служебного diff --git.
    if len(groups) == 1 and not groups[0][0].startswith("diff --git "):
        sections: list[list[str]] = []
        current = []
        for line in lines:
            if line.startswith("--- ") and current and any(
                part.startswith("+++ ") for part in current
            ):
                sections.append(current)
                current = []
            current.append(line)
        if current:
            sections.append(current)
        groups = sections

    result: list[Change] = []
    for block in groups:
        old = new = None
        additions = deletions = 0
        in_hunk = False
        binary = False
        for line in block:
            if line.startswith("--- ") and not in_hunk:
                old = _path_from_header(line)
            elif line.startswith("+++ ") and not in_hunk:
                new = _path_from_header(line)
            elif line.startswith("@@ "):
                in_hunk = True
            elif in_hunk and line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif in_hunk and line.startswith("-") and not line.startswith("---"):
                deletions += 1
            elif line.startswith(("Binary files ", "GIT binary patch")):
                binary = True
        path = new or old
        if path is None:
            # Не гадать по описанию команды или git status.
            raise AppServerError("Не удалось извлечь путь файла из diff App Server")
        result.append(Change(path, additions, deletions, "".join(block), binary))
    return result


def _ignored(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(path, pattern) or (
        pattern.startswith("**/") and fnmatch(path, pattern[3:])
    ) for pattern in patterns)


def code_changes(diff: str, cfg: Config) -> list[Change]:
    """Только код; текстовые документы и маски исключений никогда не анализируем."""
    return [change for change in parse_diff(diff)
            if is_code(change.path) and not _ignored(change.path, cfg.exclude_globs)]


def diff_for_review(files: list[Change]) -> str:
    return "".join(change.diff for change in files)
