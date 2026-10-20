from pathlib import Path
import json
from codex_auto_review.cli import install, uninstall, _owned
from codex_auto_review.config import Config, load_config


def test_install_preserves_other_hooks_and_uninstall(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-hook"}]}],
                       "SessionStart": [{"hooks": [{"type": "command", "command": "start"}]}]}}
    path.write_text(json.dumps(other))
    install(path)
    install(path)
    payload = json.loads(path.read_text())
    assert len(payload["hooks"]["Stop"]) == 2
    assert len(payload["hooks"]["UserPromptSubmit"]) == 1
    assert any(_owned(x) for x in payload["hooks"]["Stop"])
    uninstall(path)
    assert json.loads(path.read_text())["hooks"]["Stop"] == other["hooks"]["Stop"]
    assert json.loads(path.read_text())["hooks"]["SessionStart"] == other["hooks"]["SessionStart"]


def test_config_overrides(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[review]\nmin_changed_lines=200\nmode="all"\n')
    (repo / ".codex-auto-review.toml").write_text('[review]\nmin_changed_lines=12\n')
    cfg = load_config(repo, tmp_path)
    assert cfg.min_changed_lines == 12 and cfg.mode == "all"


def test_uninstall_preserves_mixed_group(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python -m codex_auto_review.cli hook Stop"},
        {"type": "command", "command": "other-hook"},
    ]}]}}))
    uninstall(path)
    entries = json.loads(path.read_text())["hooks"]["Stop"]
    assert entries == [{"hooks": [{"type": "command", "command": "other-hook"}]}]


def test_root_config_takes_precedence_over_legacy(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[review]\nmin_changed_lines=100\n')
    (repo / ".codex-auto-review.toml").write_text('[review]\nmin_changed_lines=60\n')
    (repo / "config.toml").write_text('[review]\nmin_changed_lines=10\n')
    assert load_config(repo, tmp_path).min_changed_lines == 10


def test_unrelated_root_config_is_ignored(repo: Path, tmp_path: Path) -> None:
    (repo / "config.toml").write_text('[tool]\nmode="another"\n')
    assert load_config(repo, tmp_path).min_changed_lines == 30


def test_root_config_recognized_without_legacy(repo: Path, tmp_path: Path) -> None:
    (repo / "config.toml").write_text('[review]\nmode="all"\n')
    assert load_config(repo, tmp_path).mode == "all"
