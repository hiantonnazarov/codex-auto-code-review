"""Глобальная установка хуков и CLI: без WSL, дополнительных библиотек Node.js и API-ключей."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import re
import stat
from uuid import uuid4
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .config import HOME_DIR, load_config
from .engine import handle, manual_review
from .git import GitError, root
from .store import read, state_path

HOOK_EVENTS = ("UserPromptSubmit", "Stop")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def command_for(event: str) -> str:
    parts = [sys.executable, "-m", "codex_auto_review.cli", "hook", event]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def _command_owned(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command", "")
    return isinstance(command, str) and re.search(
        r"(?:^|\s)-m\s+codex_auto_review\.cli\s+hook\s+(?:Stop|UserPromptSubmit)(?:\s|$)",
        command,
    ) is not None


def _owned(entry: dict) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
            and any(_command_owned(h) for h in entry["hooks"]))


def _without_owned(entries: list[dict]) -> list[dict]:
    """Удалять только собственные команды, сохраняя сторонние хуки в общих группах."""
    result: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            result.append(entry)
            continue
        remaining = [hook for hook in entry["hooks"] if not _command_owned(hook)]
        if remaining:
            result.append({**entry, "hooks": remaining})
    return result


def install(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks", {}), dict):
        raise ValueError("Некорректный hooks.json: ожидается объект hooks")
    hooks = payload.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event}: ожидается массив")
        entries[:] = _without_owned(entries)
        entries.append({"hooks": [{"type": "command", "command": command_for(event),
                                     "timeout": 900 if event == "Stop" else 20}]})
    _atomic_json(path, payload)


def uninstall(path: Path) -> None:
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    for event in HOOK_EVENTS:
        entries = payload.get("hooks", {}).get(event, [])
        if isinstance(entries, list):
            payload["hooks"][event] = _without_owned(entries)
    _atomic_json(path, payload)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            tmp.chmod(mode)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _doctor() -> dict:
    file = codex_home() / "hooks.json"
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
        registered = {event: any(_owned(e) for e in payload.get("hooks", {}).get(event, []))
                      for event in HOOK_EVENTS}
    except (OSError, ValueError, TypeError):
        registered = {event: False for event in HOOK_EVENTS}
    try:
        observed = read(HOME_DIR / "hook-events.json")
    except (ValueError, OSError):
        observed = {}
    return {
        "hook_events_observed": {
            "last_UserPromptSubmit": observed.get("UserPromptSubmit"),
            "last_Stop": observed.get("Stop"),
            "stop_delivery_confirmed": bool(observed.get("Stop")),
            "note": "Регистрация не подтверждает доставку Stop: проверьте время последнего события после пробного хода.",
        },
        "python": sys.version.split()[0], "python_executable": sys.executable,
        "codex": shutil.which("codex"), "git": shutil.which("git"),
        "hooks_file": str(file), "registered": registered,
        "known_limitation": "В некоторых версиях Codex Desktop/app-server событие Stop не вызывается; используйте ручную проверку.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-auto-review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="Установить глобальные хуки Codex без удаления существующих")
    sub.add_parser("uninstall", help="Удалить только хуки этого инструмента")
    sub.add_parser("doctor", help="Проверить зависимости и регистрацию хуков")
    sub.add_parser("config", help="Показать действующую конфигурацию ревью")
    status = sub.add_parser("status", help="Показать состояние сессии")
    status.add_argument("--session", required=True)
    review = sub.add_parser("review", help="Ручная проверка всех локальных изменений относительно HEAD")
    review.add_argument("--cwd", default=".")
    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument("event", choices=HOOK_EVENTS)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            path = codex_home() / "hooks.json"
            install(path)
            print(f"Глобальные хуки Codex установлены: {path}. Перезапустите Codex и разрешите хуки через /hooks.")
        elif args.command == "uninstall":
            uninstall(codex_home() / "hooks.json")
            print("Хуки codex-auto-review удалены. Перезапустите Codex.")
        elif args.command == "doctor":
            print(json.dumps(_doctor(), indent=2, ensure_ascii=False))
        elif args.command == "config":
            try:
                repo = root(Path.cwd())
            except GitError:
                repo = None
            print(json.dumps(asdict(load_config(repo, HOME_DIR)), indent=2))
        elif args.command == "status":
            print(json.dumps(read(state_path(root(Path.cwd()), args.session)), indent=2, ensure_ascii=False))
        elif args.command == "review":
            answer = manual_review(root(Path(args.cwd)))
            print(json.dumps(answer, ensure_ascii=False, indent=2))
            return 1 if answer["status"] == "blocked" else 0
        elif args.command == "hook":
            try:
                payload = json.load(sys.stdin)
                if not isinstance(payload, dict):
                    raise ValueError("Ввод хука через stdin должен содержать JSON-объект")
                result = handle(args.event, payload)
            except Exception as e:
                # Вывод всегда должен оставаться корректным JSON; ошибка не равнозначна успешному ревью.
                result = {"systemMessage": f"Ошибка хука автоматического ревью (проверка не пройдена): {type(e).__name__}: {e}"}
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, GitError, RuntimeError) as e:
        print(f"codex-auto-review: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
