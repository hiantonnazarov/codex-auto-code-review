"""Validated configuration, global defaults and local overrides."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from fnmatch import fnmatch
from pathlib import Path
import tomllib
from typing import Any

CONFIG_NAME = ".codex-auto-review.toml"
HOME_DIR = Path.home() / ".codex-auto-code-review"
CODE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
    ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".cs",
    ".rb", ".php", ".sh", ".ps1", ".sql", ".vue", ".svelte", ".dart",
)


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    mode: str = "significant"  # significant | all
    min_changed_lines: int = 30
    min_changed_files: int = 2
    critical_globs: tuple[str, ...] = (
        "**/auth/**", "**/security/**", "**/migrations/**", "**/schema/**",
        "**/api/**", "**/concurrency/**", "**/payment/**",
    )
    include_globs: tuple[str, ...] = ("*",)
    exclude_globs: tuple[str, ...] = (
        "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**",
        "**/.venv/**", "**/coverage/**", "**/*.min.js", "**/*.lock",
    )
    reviewer_model: str = ""  # empty: Codex configured default
    reasoning_effort: str = "high"
    codex_command: str = "codex"
    confidence_threshold: float = 0.65
    blocking_priorities: tuple[str, ...] = ("P0", "P1", "P2")
    max_review_rounds: int = 3
    max_diff_chars_per_chunk: int = 26000
    max_chunks: int = 6
    timeout_seconds_per_chunk: int = 100
    fail_open: bool = True
    report_dir: str = ""  # empty: app state folder, not the source tree


def _tuple_strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(i, str) and i for i in value):
        raise ValueError(f"{field}: expected a non-empty list of strings (or an empty list)")
    return tuple(value)


def _apply(config: Config, changes: dict[str, Any], origin: Path) -> Config:
    known = {f.name: f for f in fields(Config)}
    result: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in known:
            raise ValueError(f"Unknown setting {name!r} in {origin}")
        current = getattr(config, name)
        if isinstance(current, tuple):
            value = _tuple_strings(value, name)
        elif isinstance(current, bool):
            if type(value) is not bool:
                raise ValueError(f"{name}: expected boolean")
        elif isinstance(current, int):
            if type(value) is not int:
                raise ValueError(f"{name}: expected integer")
        elif isinstance(current, float):
            if type(value) not in (int, float):
                raise ValueError(f"{name}: expected number")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"{name}: expected string")
        result[name] = value
    config = replace(config, **result)
    if config.mode not in ("all", "significant"):
        raise ValueError("mode must be all or significant")
    if config.reasoning_effort not in ("low", "medium", "high", "xhigh"):
        raise ValueError("reasoning_effort must be low/medium/high/xhigh")
    if not 0 <= config.confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be in [0, 1]")
    if any(getattr(config, k) < minimum for k, minimum in (
        ("min_changed_lines", 0), ("min_changed_files", 1),
        ("max_review_rounds", 1), ("max_diff_chars_per_chunk", 1000),
        ("max_chunks", 1), ("timeout_seconds_per_chunk", 1),
    )):
        raise ValueError("invalid non-positive or too-small numeric setting")
    if not config.codex_command.strip():
        raise ValueError("codex_command cannot be blank")
    if any(x not in ("P0", "P1", "P2", "P3") for x in config.blocking_priorities):
        raise ValueError("blocking_priorities must be P0..P3")
    return config


def load_config(repo: Path | None = None, home: Path | None = None) -> Config:
    config = Config()
    locations = [(home or HOME_DIR) / "config.toml"]
    if repo is not None:
        locations.append(repo / CONFIG_NAME)
    for path in locations:
        if path.is_file():
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload.get("review", {}), dict):
                raise ValueError(f"Expected [review] table in {path}")
            config = _apply(config, payload.get("review", {}), path)
    return config


def matches(path: str, globs: tuple[str, ...]) -> bool:
    norm = path.replace("\\", "/")
    return any(fnmatch(norm, pat) or (pat.startswith("**/") and fnmatch(norm, pat[3:])) for pat in globs)


def is_code(path: str) -> bool:
    p = Path(path)
    return p.suffix.lower() in CODE_SUFFIXES or p.name in ("Dockerfile", "Makefile", "Containerfile")
