"""Единственный источник области ревью: успешные PostToolUse(apply_patch).

Здесь нет Git diff, App Server, снимков каталога и чтения истории сессии.
Сохраняются именно операции, выполненные инструментом конкретного хода.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import re

from .config import Config, HOME_DIR, is_code, matches
from .store import locked, read, write


class CaptureError(RuntimeError):
    """Не удалось достоверно прочитать подтверждённый apply_patch."""


@dataclass(frozen=True)
class Change:
    path: str
    additions: int
    deletions: int
    diff: str


def repo_root(start: Path) -> Path:
    directory = start.resolve()
    if directory.is_file():
        directory = directory.parent
    for candidate in (directory, *directory.parents):
        # В worktree .git может быть файлом, не нужно вызывать Git.
        if (candidate / ".git").exists():
            return candidate
    raise CaptureError(f"Не найден корень Git-проекта: {start}")


def event_path(repo: Path, session: str, turn: str, home: Path | None = None) -> Path:
    if not session or not turn:
        raise CaptureError("Не указаны session_id или turn_id")
    digest = sha256(json.dumps(
        [str(repo.resolve()), session, turn], ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    return (home or HOME_DIR) / "patches" / f"{digest}.json"


def _relative(raw: str, repo: Path) -> str:
    if not raw or "\0" in raw:
        raise CaptureError("Пустой или некорректный путь в apply_patch")
    # Пути Windows и POSIX валидируются без угадывания текущей директории.
    normalized = raw.replace("\\", "/")
    if any(part in (".", "..", "") for part in normalized.split("/")):
        raise CaptureError(f"Небезопасный путь: {raw!r}")
    if PureWindowsPath(raw).drive or normalized.startswith("/"):
        resolved = Path(raw).resolve()
    else:
        resolved = (repo / normalized).resolve()
    root = repo.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise CaptureError(f"Путь за пределами проекта: {raw!r}")
    return resolved.relative_to(root).as_posix()


def parse_patch(command: str, repo: Path) -> list[Change]:
    """Разбирает формат Codex apply_patch; сохраняет фрагменты каждого файла.

    Это НЕ unified diff. Исходные контекстные строки/якоря остаются в тексте
    операции; счётчики включают только +/- строки патча.
    """
    if not isinstance(command, str):
        raise CaptureError("Отсутствует текст apply_patch в tool_input.command")
    lines = command.splitlines(keepends=True)
    if len(lines) < 3 or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise CaptureError("Неверные границы apply_patch")
    current: list[str] = []
    groups: list[list[str]] = []
    for line in lines[1:-1]:
        if line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
            if current:
                groups.append(current)
            current = [line]
        else:
            if not current:
                raise CaptureError("Строка до первого заголовка файла")
            current.append(line)
    if current:
        groups.append(current)
    if not groups:
        raise CaptureError("В apply_patch отсутствуют изменения файлов")

    out: list[Change] = []
    for group in groups:
        header = group[0].rstrip("\r\n")
        kind, raw = header[4:].split(" File: ", 1)
        old_path = _relative(raw, repo)
        path = old_path
        offset = 1
        if kind == "Update" and len(group) > 1 and group[1].startswith("*** Move to: "):
            path = _relative(group[1][len("*** Move to: "):].strip(), repo)
            offset = 2
        if kind not in {"Add", "Update", "Delete"}:
            raise CaptureError("Неизвестная операция apply_patch")
        additions = deletions = 0
        for line in group[offset:]:
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
            elif not (line.startswith((" ", "@@", "*** End of File"))):
                raise CaptureError(f"Неподдерживаемая строка apply_patch в {path}")
        # При удалении нет -строк; строка файла не читается, чтобы не захватить чужие изменения.
        fragment = "*** Begin Patch\n" + "".join(group) + "*** End Patch\n"
        out.append(Change(path, additions, deletions, fragment))
    return out


def _response_text(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if result.get("isError") is True or result.get("success") is False:
            return None
        if result.get("status") in ("failed", "error"):
            return None
        if isinstance(result.get("exit_code"), int) and result["exit_code"] != 0:
            return None
        if isinstance(result.get("exitCode"), int) and result["exitCode"] != 0:
            return None
        for key in ("output", "stdout", "text"):
            if isinstance(result.get(key), str):
                return result[key]
        content = result.get("content")
        if isinstance(content, list):
            parts = [item["text"] for item in content
                     if isinstance(item, dict) and isinstance(item.get("text"), str)]
            return "\n".join(parts) if parts else None
    return None


def _success(result: object) -> bool | None:
    """True = полностью успешная операция; False = ошибка; None = неизвестный ответ."""
    text = _response_text(result)
    if text is None:
        if isinstance(result, dict) and (
            result.get("isError") is True or result.get("success") is False
            or result.get("status") in ("failed", "error")
            or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
            or (isinstance(result.get("exitCode"), int) and result["exitCode"] != 0)
        ):
            return False
        return None
    if re.search(r"(?m)^Success\. Updated the following files:\s*$", text):
        return True
    if re.search(r"(?i)\b(failed|error|invalid patch|could not apply)\b", text):
        return False
    return None


def record_patch(payload: dict, repo: Path, home: Path | None = None) -> bool:
    """Только apply_patch, только успешные вызовы. Дедуп по tool_use_id."""
    if payload.get("tool_name") != "apply_patch":
        return False
    session = payload.get("session_id")
    turn = payload.get("turn_id")
    call = payload.get("tool_use_id")
    if not all(isinstance(x, str) and x for x in (session, turn, call)):
        raise CaptureError("PostToolUse apply_patch: обязательны session_id, turn_id и tool_use_id")
    path = event_path(repo, session, turn, home)
    with locked(path):
        state = read(path)
        seen = state.setdefault("calls", {})
        if call in seen:
            return False
        result = _success(payload.get("tool_response"))
        if result is False:
            seen[call] = {"status": "failed"}
        elif result is None:
            seen[call] = {"status": "unknown"}
            state.setdefault("errors", []).append(f"Неподтверждённый результат apply_patch: {call}")
        else:
            try:
                tool_input = payload.get("tool_input")
                if not isinstance(tool_input, dict):
                    raise CaptureError("Отсутствует tool_input")
                files = parse_patch(tool_input.get("command"), repo)
                seen[call] = {
                    "status": "success",
                    "files": [
                        {"path": f.path, "additions": f.additions,
                         "deletions": f.deletions, "diff": f.diff} for f in files
                    ],
                }
            except (CaptureError, TypeError, ValueError) as exc:
                seen[call] = {"status": "invalid"}
                state.setdefault("errors", []).append(f"Некорректный патч {call}: {exc}")
        write(path, state)
        return result is True and seen[call]["status"] == "success"


def code_changes(repo: Path, session: str, turn: str, cfg: Config,
                 home: Path | None = None) -> list[Change]:
    data = read(event_path(repo, session, turn, home))
    errors = data.get("errors", [])
    if errors:
        raise CaptureError("; ".join(errors[:5]))
    merged: dict[str, Change] = {}
    for item in data.get("calls", {}).values():
        if item.get("status") != "success":
            continue
        for raw in item.get("files", []):
            p = raw["path"]
            if not is_code(p) or matches(p, cfg.exclude_globs):
                continue
            previous = merged.get(p)
            if previous is None:
                merged[p] = Change(p, raw["additions"], raw["deletions"], raw["diff"])
            else:
                merged[p] = Change(p, previous.additions + raw["additions"],
                                   previous.deletions + raw["deletions"],
                                   previous.diff + "\n" + raw["diff"])
    return list(merged.values())
