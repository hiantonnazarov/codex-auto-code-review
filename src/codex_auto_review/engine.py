"""Оркестрация жизненного цикла Codex: baseline, review, feedback, повторная проверка."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from .config import HOME_DIR, Config, load_config, matches, is_code
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
        or any(matches(c.path, cfg.critical_globs) or c.binary or c.metadata for c in files)
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
                 baseline: str, current: str, checks: list[Finding], status: str,
                 round_no: int = 0, error: str | None = None) -> Path:
    folder = _report_location(repo, session, cfg, home)
    path = folder / f"{current[:12] or 'unknown'}-{status}-{uuid4().hex[:12]}.json"
    write(path, {
        "created_at": _timestamp(), "repo": str(repo), "session": session,
        "baseline_tree": baseline, "reviewed_tree": current, "status": status,
        "round": round_no, "error": error,
        "findings": [asdict(x) for x in checks],
    })
    return path


def _feedback(blocking: list[Finding], all_findings: list[Finding],
              report: Path, round_no: int, cfg: Config, token: str) -> str:
    priorities = ", ".join(sorted(set(cfg.blocking_priorities)))
    out = [f"Независимое ревью Codex (раунд {round_no}): {len(blocking)} блокирующих замечаний "
           f"(всего {len(all_findings)}). Проверьте и исправьте ошибки приоритетов {priorities}, "
           "затем выполните соответствующие тесты.",
           "Если замечание ложно, обоснуйте это конкретными доказательствами из кода.",
           f"[codex-auto-review-continuation:{token}]"]
    for f in blocking[:12]:
        out.append(f"[{f.priority}, confidence={f.confidence:.2f}] {f.file}:{f.line} {f.title} — {f.explanation}")
    if len(blocking) > 12:
        out.append(f"Ещё {len(blocking) - 12} замечаний приведены в отчёте.")
    out.append(f"Полный отчёт JSON: {report}")
    return "\n".join(out)


def submit(repo: Path, session: str, previous_message: str = "",
           home: Path | None = None, turn_id: str = "") -> dict:
    cfg = load_config(repo, home)
    if not cfg.enabled:
        return {}
    path = state_path(repo, session, home)
    try:
        with locked(path):
            old = read(path)
            # Stop->block создаёт автоматическое продолжение как новый prompt.
            # Если оно содержит собственный маркер, baseline исходной задачи сохраняется.
            token = old.get("continuation_token", "")
            if (token and old.get("status") in ("blocked", "stale_review", "error")
                    and (f"[codex-auto-review-continuation:{token}]" in previous_message
                         or not previous_message.strip())):
                old.update(turn_id=turn_id, updated_at=_timestamp())
                write(path, old)
                return {}
            # Повторная доставка того же события не должна перезаписывать baseline.
            if turn_id and old.get("turn_id") == turn_id and old.get("baseline"):
                return {}
            tree = snapshot(repo)
            write(path, {
                "session": session, "repo": str(repo), "baseline": tree,
                "turn_id": turn_id, "turn_started_at": _timestamp(),
                "continuation_token": secrets.token_hex(12),
                "rounds": 0, "reviewed": {},
                "previous_message": previous_message[:2500], "status": "pending",
            })
        return {}
    except Exception as e:
        return _hook_output(message=f"Не удалось сохранить исходное состояние для ревью: {e}")


def _policy_key(cfg: Config) -> str:
    return sha256(json.dumps(asdict(cfg), sort_keys=True).encode()).hexdigest()[:16]


def _record_error(repo: Path, session: str, path: Path, state: dict,
                  cfg: Config, home: Path | None, error: Exception) -> dict:
    current = state.get("last_current", "")
    message = f"Автоматическое ревью не выполнено (проверка не пройдена): {error}. "
    message += "Устраните причину или запустите codex-auto-review review --cwd ."
    try:
        report = _save_report(repo, session, cfg, home, state.get("baseline", ""),
                              current, [], "error", state.get("rounds", 0), str(error))
        state.update(last_report=str(report))
    except (OSError, ValueError) as report_error:
        message += f" Ошибка сохранения отчёта: {report_error}."
    state.update(status="error", last_error=str(error), last_error_tree=current, updated_at=_timestamp())
    try:
        write(path, state)
    except (OSError, ValueError):
        pass
    if cfg.fail_open:
        return _hook_output(message=message)
    token = state.get("continuation_token", "")
    return _hook_output("block", message + f" [codex-auto-review-continuation:{token}]")


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
                return _hook_output(message="Нет исходного снимка для этого хода. Запустите codex-auto-review review --cwd .")
            try:
                return _stop_locked(repo, session, path, state, cfg, home)
            except (GitError, ReviewerError, OSError, ValueError, RuntimeError) as e:
                return _record_error(repo, session, path, state, cfg, home, e)
    except (OSError, ValueError, RuntimeError) as e:
        message = f"Не удалось получить состояние или блокировку ревью: {e}."
        return _hook_output(message=message) if cfg.fail_open else _hook_output("block", message)


def _stop_locked(repo: Path, session: str, path: Path, state: dict,
                 cfg: Config, home: Path | None) -> dict:
    baseline = state["baseline"]
    current = snapshot(repo)
    state["last_current"] = current
    changed = _filtered(changes(repo, baseline, current), cfg)
    if not significant(changed, cfg):
        state.update(status="skipped_insignificant", changed_files=len(changed), updated_at=_timestamp())
        write(path, state)
        return {}
    cache_key = current + ":" + _policy_key(cfg)
    prior = state.get("reviewed", {}).get(cache_key)
    if prior in ("passed", "blocked"):
        state.update(status=prior, last_report=state.get("reports_by_tree", {}).get(
            cache_key, state.get("last_report")), updated_at=_timestamp())
        write(path, state)
    if prior == "passed":
        return {}
    if prior == "blocked":
        # Повторный block на том же дереве без правок создавал бы бесконечный
        # цикл автоматических продолжений. Сохраняем явный статус, не "passed".
        return _hook_output(message=f"Ревью НЕ ПРОЙДЕНО: код не изменён после замечаний. "
                            f"Нужно исправление или решение пользователя. Отчёт: {state.get('last_report')}")
    if state.get("last_error_tree") == current and state.get("status") == "error":
        message = ("Ревью НЕ ПРОЙДЕНО: повторная проверка после ошибки требует "
                   f"явного вмешательства. {state.get('last_error')}")
        if not cfg.fail_open:
            return {"continue": False, "stopReason": message, "systemMessage": message}
        return _hook_output(message=message)
    if state["rounds"] >= cfg.max_review_rounds:
        state.update(status="limit_reached", updated_at=_timestamp())
        write(path, state)
        return _hook_output(message=f"Ревью НЕ ПРОЙДЕНО: достигнут лимит раундов "
                            f"({cfg.max_review_rounds}); нерешённые замечания требуют решения пользователя. "
                            f"Отчёт: {state.get('last_report', '')}")
    findings = run_review(repo, baseline, current, changed, cfg, state.get("previous_message", ""))
    if snapshot(repo) != current:
        report = _save_report(repo, session, cfg, home, baseline, current, [],
                              "stale_review", state.get("rounds", 0))
        state.update(status="stale_review", last_report=str(report), updated_at=_timestamp())
        write(path, state)
        return _hook_output("block", "Файлы изменились во время ревью; повторите проверку нового снимка. "
                            f"[codex-auto-review-continuation:{state['continuation_token']}]")
    blocking = [x for x in findings if x.priority in cfg.blocking_priorities
                and x.confidence >= cfg.confidence_threshold]
    status = "blocked" if blocking else "passed"
    round_no = state["rounds"] + 1
    report = _save_report(repo, session, cfg, home, baseline, current, findings, status, round_no)
    reviewed = state.get("reviewed", {})
    reviewed[cache_key] = status
    reports_by_tree = state.get("reports_by_tree", {})
    reports_by_tree[cache_key] = str(report)
    state.update(reviewed=reviewed, reports_by_tree=reports_by_tree,
                 rounds=round_no, status=status, last_report=str(report),
                 last_findings=len(findings), changed_files=len(changed),
                 last_error_tree="", updated_at=_timestamp())
    write(path, state)
    if blocking:
        feedback = _feedback(blocking, findings, report, round_no, cfg, state["continuation_token"])
        if round_no >= cfg.max_review_rounds:
            state["status"] = "limit_reached"
            write(path, state)
            return _hook_output(message="Ревью НЕ ПРОЙДЕНО: последний разрешённый раунд; "
                                "нужно решение пользователя.\n" + feedback)
        return _hook_output("block", feedback)
    return _hook_output(message=f"Независимое ревью Codex ПРОЙДЕНО "
                        f"(файлов: {len(changed)}, неблокирующих замечаний: {len(findings)}). Отчёт: {report}")


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
    blocking = [f for f in findings if f.priority in cfg.blocking_priorities
                and f.confidence >= cfg.confidence_threshold]
    report = _save_report(repo, "manual", cfg, home, base, current, findings,
                          "blocked" if blocking else "passed", 1)
    return {"status": "blocked" if blocking else "passed", "report": str(report),
            "findings": [asdict(f) for f in findings]}


def _record_hook_event(event: str, home: Path | None) -> None:
    """Диагностика фактической доставки событий, без хранения пользовательского текста."""
    file = (home or HOME_DIR) / "hook-events.json"
    try:
        with locked(file, max_wait_seconds=2):
            events = read(file)
            events[event] = _timestamp()
            write(file, events)
    except (OSError, ValueError, RuntimeError):
        pass  # Мониторинг не должен ломать пользовательскую сессию.


def handle(event: str, payload: dict, home: Path | None = None) -> dict:
    if os.environ.get("CODEX_AUTO_REVIEW_CHILD") == "1":
        return {}
    _record_hook_event(event, home)
    session = str(payload.get("session_id") or payload.get("thread_id") or "")
    if not session:
        return _hook_output(message="Отсутствует session_id: хук не может связать изменения с текущим ходом.")
    try:
        repo = root(Path(payload.get("cwd") or os.getcwd()))
    except (GitError, FileNotFoundError, OSError):
        return {}  # Работа вне Git-репозитория допустима.
    if event == "UserPromptSubmit":
        return submit(repo, session, str(payload.get("prompt", "")), home, str(payload.get("turn_id") or ""))
    if event == "Stop":
        return stop(repo, session, home)
    raise ValueError(f"Неизвестное событие хука: {event}")
