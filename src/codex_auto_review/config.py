"""Проверка конфигурации, значения по умолчанию и переопределения настроек."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from fnmatch import fnmatch
from pathlib import Path
import tomllib
import warnings
from typing import Any

CONFIG_NAME = "config.toml"
LEGACY_CONFIG_NAME = ".codex-auto-review.toml"
HOME_DIR = Path.home() / ".codex-auto-code-review"
CODE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
    ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".cs",
    ".rb", ".php", ".sh", ".ps1", ".sql", ".vue", ".svelte", ".dart",
)


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    mode: str = "significant"  # significant | all (только существенные изменения или любые)
    min_changed_lines: int = 30
    min_changed_files: int = 2
    exclude_globs: tuple[str, ...] = (
        "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**",
        "**/.venv/**", "**/coverage/**", "**/*.min.js", "**/*.lock",
    )
    reviewer_model: str = ""  # пустая строка — модель из настроек Codex
    reasoning_effort: str = "high"
    codex_command: str = "codex"
    confidence_threshold: float = 0.65
    blocking_priorities: tuple[str, ...] = ("P0", "P1", "P2")
    max_review_rounds: int = 3
    max_task_chars: int = 50000  # полный исходный запрос, не обрезать молча
    max_diff_chars_per_chunk: int = 26000
    max_chunks: int = 6
    timeout_seconds_per_chunk: int = 100
    fail_open: bool = False
    report_dir: str = ""  # пустая строка — каталог состояния приложения вне исходного кода


def _tuple_strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(i, str) and i for i in value):
        raise ValueError(f"{field}: ожидался список строк (допускается пустой список)")
    return tuple(value)


def _apply(config: Config, changes: dict[str, Any], origin: Path) -> Config:
    known = {f.name: f for f in fields(Config)}
    result: dict[str, Any] = {}
    for name, value in changes.items():
        if name in ("critical_globs", "include_globs"):
            warnings.warn(
                f"{name} больше не используется: PostToolUse review поддерживает только exclude_globs",
                DeprecationWarning, stacklevel=2,
            )
            continue
        if name not in known:
            raise ValueError(f"Неизвестная настройка {name!r} в {origin}")
        current = getattr(config, name)
        if isinstance(current, tuple):
            value = _tuple_strings(value, name)
        elif isinstance(current, bool):
            if type(value) is not bool:
                raise ValueError(f"{name}: ожидается логическое значение true/false")
        elif isinstance(current, int):
            if type(value) is not int:
                raise ValueError(f"{name}: ожидается целое число")
        elif isinstance(current, float):
            if type(value) not in (int, float):
                raise ValueError(f"{name}: ожидается число")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"{name}: ожидается строка")
        result[name] = value
    config = replace(config, **result)
    if config.mode not in ("all", "significant"):
        raise ValueError("mode: допустимые значения all или significant")
    if config.reasoning_effort not in ("low", "medium", "high", "xhigh"):
        raise ValueError("reasoning_effort: допустимые значения low/medium/high/xhigh")
    if not 0 <= config.confidence_threshold <= 1:
        raise ValueError("confidence_threshold должен находиться в диапазоне от 0 до 1")
    if any(getattr(config, k) < minimum for k, minimum in (
        ("min_changed_lines", 0), ("min_changed_files", 1),
        ("max_review_rounds", 1), ("max_task_chars", 1000),
        ("max_diff_chars_per_chunk", 1000),
        ("max_chunks", 1), ("timeout_seconds_per_chunk", 1),
    )):
        raise ValueError("Недопустимое отрицательное или слишком маленькое числовое значение настройки")
    if not config.codex_command.strip():
        raise ValueError("codex_command не должен быть пустым")
    if any(x not in ("P0", "P1", "P2", "P3") for x in config.blocking_priorities):
        raise ValueError("blocking_priorities: допустимы только P0, P1, P2, P3")
    return config


def load_config(repo: Path | None = None, home: Path | None = None) -> Config:
    config = Config()
    locations = [(home or HOME_DIR) / "config.toml"]
    if repo is not None:
        # Старый локальный файл загружается первым; корневой config.toml имеет приоритет.
        # config.toml может принадлежать другому инструменту: без [review] он игнорируется.
        locations.extend([repo / LEGACY_CONFIG_NAME, repo / CONFIG_NAME])
    for path in locations:
        if not path.is_file():
            continue
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        if path.name == CONFIG_NAME and repo is not None and path == repo / CONFIG_NAME and "review" not in payload:
            continue
        if not isinstance(payload.get("review", {}), dict):
            raise ValueError(f"Ожидалась таблица [review] в {path}")
        config = _apply(config, payload.get("review", {}), path)
    return config


def matches(path: str, globs: tuple[str, ...]) -> bool:
    norm = path.replace("\\", "/")
    return any(fnmatch(norm, pat) or (pat.startswith("**/") and fnmatch(norm, pat[3:])) for pat in globs)


def is_code(path: str) -> bool:
    p = Path(path)
    return p.suffix.lower() in CODE_SUFFIXES or p.name in ("Dockerfile", "Makefile", "Containerfile")
