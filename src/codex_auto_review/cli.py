"""Одна установка глобальных Codex hooks: UserPromptSubmit, PostToolUse, Stop."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from uuid import uuid4

from .config import HOME_DIR
from .engine import handle
from .store import read

HOOK_EVENTS = ("UserPromptSubmit", "PostToolUse", "Stop")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def command_for(event: str) -> str:
    cmd = [sys.executable, "-m", "codex_auto_review.cli", "hook", event]
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def _command_owned(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command", "")
    return isinstance(command, str) and re.search(
        r"(?:^|\s)-m\s+codex_auto_review\.cli\s+hook\s+"
        r"(?:Stop|UserPromptSubmit|PostToolUse)(?:\s|$)", command
    ) is not None


def _without_owned(entries: list) -> list:
    """Сохранять чужие hook handlers и все остальные настройки группы."""
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            result.append(entry)
            continue
        remaining = [h for h in entry["hooks"] if not _command_owned(h)]
        if remaining:
            result.append({**entry, "hooks": remaining})
    return result


def _owned(entry: dict) -> bool:
    return isinstance(entry, dict) and any(
        _command_owned(h) for h in entry.get("hooks", [])
    )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        if os.name != "nt":
            tmp.chmod(stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def install(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks", {}), dict):
        raise ValueError("hooks.json: ожидается JSON-объект с hooks")
    hooks = payload.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event}: ожидался массив")
        hooks[event] = _without_owned(entries)
        handler = {
            "type": "command",
            "command": command_for(event),
            "timeout": 900 if event == "Stop" else 20,
        }
        if event == "Stop":
            handler["statusMessage"] = "Auto Code Review: выполняю ревью изменений кода"
        group = {"hooks": [handler]}
        if event == "PostToolUse":
            group["matcher"] = "^apply_patch$"
        hooks[event].append(group)
    _atomic_json(path, payload)


def uninstall(path: Path) -> None:
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), dict):
        raise ValueError("hooks.json: ожидается JSON-объект с hooks")
    for event in HOOK_EVENTS:
        if isinstance(payload["hooks"].get(event), list):
            payload["hooks"][event] = _without_owned(payload["hooks"][event])
    _atomic_json(path, payload)


def doctor() -> dict:
    path = codex_home() / "hooks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        registered = {
            key: any(_owned(group) for group in data.get("hooks", {}).get(key, []))
            for key in HOOK_EVENTS
        }
        post_matcher_ok = any(
            group.get("matcher") == "^apply_patch$" and _owned(group)
            for group in data.get("hooks", {}).get("PostToolUse", [])
        )
    except (OSError, ValueError, TypeError, AttributeError):
        registered = {key: False for key in HOOK_EVENTS}
        post_matcher_ok = False
    try:
        events = read(HOME_DIR / "hook-events.json")
    except (OSError, ValueError):
        events = {}
    return {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "codex": shutil.which("codex"),
        "hooks_file": str(path),
        "registered": registered,
        "posttooluse_matcher_ok": post_matcher_ok,
        "last_seen": {key: events.get(key) for key in HOOK_EVENTS},
        "note": "Регистрация не гарантирует доставку hooks в Codex Desktop. "
                "Проверьте время событий после тестового редактирования apply_patch.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-auto-review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="Зарегистрировать глобальные хуки Codex")
    sub.add_parser("uninstall", help="Удалить только хуки Auto Code Review")
    sub.add_parser("doctor", help="Диагностика регистрации и получения хуков")
    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument("event", choices=HOOK_EVENTS)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            path = codex_home() / "hooks.json"
            install(path)
            print(f"Codex hooks установлены: {path}. Перезапустите Codex и разрешите их через /hooks.")
        elif args.command == "uninstall":
            uninstall(codex_home() / "hooks.json")
            print("Хуки проекта удалены. Перезапустите Codex.")
        elif args.command == "doctor":
            print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        elif args.command == "hook":
            try:
                payload = json.load(sys.stdin)
                if not isinstance(payload, dict):
                    raise ValueError("Ожидался JSON-объект от Codex")
                answer = handle(args.event, payload)
            except Exception as exc:
                answer = {
                    "systemMessage":
                    f"Auto Code Review: ошибка хука, ревью не подтверждено: "
                    f"{type(exc).__name__}: {exc}"
                }
            print(json.dumps(answer, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"codex-auto-review: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
