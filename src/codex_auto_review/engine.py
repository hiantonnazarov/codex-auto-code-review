"""Оркестрация: снимок -> значимость -> ревью -> замечания -> повторная проверка."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from .config import Config, load_config, matches, is_code
from .git import Change, GitError, changes, head_tree, root, snapshot
from .reviewer import Finding, ReviewerError, run_review
from .store import locked, read, state_path, write


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hook_output(decision: str | None = None, message: str = "") -> dict:
    if decision == "block":
        return {"decision": "block", "reason": message}
    return {"systemMessage": message} if message else {}


def _filtered(changeset: list[Change], cfg: Config) -> list[Change]:
    return [c for c in changeset
            if matches(c.path, cfg.include_globs) and not matches(c.path, cfg.exclude_globs)
            and (is_code(c.path) or matches(c.path, cfg.critical_globs))]


def significant(files: list[Change], cfg: Config) -> bool:
    if not cfg.enabled or not files:
        return False
    if cfg.mode == "all":
        return True
    return (
        len(files) >= cfg.min_changed_files
        or sum(c.additions + c.deletions for c in files) >= cfg.min_changed_lines
        or any(matches(c.path, cfg.critical_globs) for c in files)
    )


def _report_location(repo: Path, session: str, cfg: Config, home: Path | None) -> Path:
    state = state_path(repo, session, home)
    if not cfg.report_dir:
        directory = state.parent / "reports" / state.stem
    else:
        directory = Path(cfg.report_dir).expanduser()
        if not directory.is_absolute():
            directory = repo / directory
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _save_report(repo: Path, session: str, cfg: Config, home: Path | None,
                 baseline: str, current: str, checks: list[Finding], status: str) -> Path:
    folder = _report_location(repo, session, cfg, home)
    name = f"{current[:12]}-{status}.json"
    path = folder / name
    path.write_text(json.dumps({
        "created_at": _timestamp(), "repo": str(repo), "session": session,
        "baseline_tree": baseline, "reviewed_tree": current, "status": status,
        "findings": [asdict(x) for x in checks],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _feedback(blocking: list[Finding], all_findings: list[Finding], report: Path, round_no: int) -> str:
    out = [f"Независимое ревью Codex (раунд {round_no}): {len(blocking)} блокирующих замечаний ",
           f"(всего {len(all_findings)}). Исправьте подтверждённые ошибки P0–P2 и повторно запустите тесты.",
           "Если замечание ложно, обоснуйте это конкретными доказательствами. Не изменяйте несвязанные файлы."]
    for f in blocking[:12]:
        out.append(f"[{f.priority}, confidence={f.confidence:.2f}] {f.file}:{f.line} {f.title} — {f.explanation}")
    if len(blocking) > 12:
        out.append(f"Ещё {len(blocking) - 12} замечаний приведены в отчёте.")
    out.append(f"Полный отчёт JSON: {report}")
    return "\n".join(out)


def submit(repo: Path, session: str, previous_message: str = "", home: Path | None = None) -> dict:
    cfg = load_config(repo, home)
    if not cfg.enabled:
        return {}
    path = state_path(repo, session, home)
    try:
        with locked(path):
            tree = snapshot(repo)
            write(path, {
                "session": session, "repo": str(repo), "baseline": tree,
                "turn_started_at": _timestamp(), "rounds": 0, "reviewed": {},
                "previous_message": previous_message[:2500], "status": "pending",
            })
        return {}
    except Exception as e:
        return _hook_output(message=f"Не удалось сохранить исходное состояние для ревью: {e}")


def stop(repo: Path, session: str, home: Path | None = None) -> dict:
    path = state_path(repo, session, home)
    try:
        cfg = load_config(repo, home)
    except (OSError, ValueError) as e:
        return _hook_output(message=f"Ошибка конфигурации автоматического ревью: {e}")
    if not cfg.enabled:
        return {}
    try:
        with locked(path):
            state = read(path)
            if not state.get("baseline"):
                return _hook_output(message="Нет исходного снимка для этого хода. Запустите резервную команду codex-auto-review review.")
            return _stop_locked(repo, session, path, state, cfg, home)
    except (GitError, ReviewerError, OSError, ValueError, RuntimeError) as e:
        # Неизвестный результат ревью нельзя отмечать как успешный.
        msg = f"Автоматическое ревью не выполнено (проверка не пройдена): {e}. Повторите командой codex-auto-review review."
        try:
            state = read(path)
            state.update(status="error", last_error=str(e), updated_at=_timestamp())
            write(path, state)
        except (OSError, ValueError):
            pass
        return _hook_output(message=msg) if cfg.fail_open else _hook_output("block", msg)


def _stop_locked(repo: Path, session: str, path: Path, state: dict, cfg: Config, home: Path | None) -> dict:
    baseline = state["baseline"]
    current = snapshot(repo)
    changed = _filtered(changes(repo, baseline, current), cfg)
    if not significant(changed, cfg):
        state.update(status="skipped_insignificant", changed_files=len(changed), updated_at=_timestamp())
        write(path, state)
        return {}
    prior = state.get("reviewed", {}).get(current)
    if prior:
        if prior == "passed":
            return {}
        return _hook_output(message="Код не изменился, а замечания остались нерешёнными; см. предыдущий отчёт ревью.")
    if state["rounds"] >= cfg.max_review_rounds:
        state.update(status="limit_reached", updated_at=_timestamp())
        write(path, state)
        return _hook_output(message=f"Достигнут лимит раундов ревью ({cfg.max_review_rounds}); нерешённые замечания требуют решения пользователя.")
    findings = run_review(repo, baseline, current, changed, cfg, state.get("previous_message", ""))
    if snapshot(repo) != current:
        state.update(status="stale_review", updated_at=_timestamp())
        write(path, state)
        return _hook_output("block", "Файлы изменились во время ревью; повторите проверку нового снимка.")
    blocking = [x for x in findings if x.priority in cfg.blocking_priorities and x.confidence >= cfg.confidence_threshold]
    status = "blocked" if blocking else "passed"
    report = _save_report(repo, session, cfg, home, baseline, current, findings, status)
    reviewed = state.get("reviewed", {})
    reviewed[current] = status
    state.update(reviewed=reviewed, rounds=state["rounds"] + 1,
                 status=status, last_report=str(report), last_findings=len(findings),
                 changed_files=len(changed), updated_at=_timestamp())
    write(path, state)
    if blocking:
        if state["rounds"] >= cfg.max_review_rounds:
            return _hook_output(message="На последнем разрешённом раунде ревью остались неисправленные ошибки. " + _feedback(blocking, findings, report, state["rounds"]))
        return _hook_output("block", _feedback(blocking, findings, report, state["rounds"]))
    return _hook_output(message=f"Независимое ревью Codex ПРОЙДЕНО (файлов: {len(changed)}, неблокирующих замечаний: {len(findings)}). Отчёт: {report}")


def manual_review(repo: Path, home: Path | None = None, force: bool = True) -> dict:
    cfg = load_config(repo, home)
    current = snapshot(repo)
    base = head_tree(repo)
    selected = _filtered(changes(repo, base, current), cfg)
    if not selected:
        return {"status": "no_code_changes", "findings": []}
    if not force and not significant(selected, cfg):
        return {"status": "insignificant", "findings": []}
    findings = run_review(repo, base, current, selected, cfg)
    if snapshot(repo) != current:
        raise ReviewerError("Файлы изменились во время ревью; повторите проверку текущего снимка")
    blocking = [f for f in findings if f.priority in cfg.blocking_priorities and f.confidence >= cfg.confidence_threshold]
    report = _save_report(repo, "manual", cfg, home, base, current, findings, "blocked" if blocking else "passed")
    return {"status": "blocked" if blocking else "passed", "report": str(report),
            "findings": [asdict(f) for f in findings]}


def handle(event: str, payload: dict, home: Path | None = None) -> dict:
    if os.environ.get("CODEX_AUTO_REVIEW_CHILD") == "1" or payload.get("stop_hook_active") and event == "UserPromptSubmit":
        return {}
    session = str(payload.get("session_id") or payload.get("thread_id") or "")
    if not session:
        return _hook_output(message="Отсутствует session_id: хук не может связать изменения с текущим ходом.")
    try:
        repo = root(Path(payload.get("cwd") or os.getcwd()))
    except (GitError, FileNotFoundError, OSError):
        return {}  # Работа вне Git-репозитория допустима.
    if event == "UserPromptSubmit":
        return submit(repo, session, str(payload.get("prompt", "")), home)
    if event == "Stop":
        return stop(repo, session, home)
    raise ValueError(f"Неизвестное событие хука: {event}")
