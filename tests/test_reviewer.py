import json
from pathlib import Path
import pytest
from codex_auto_review.config import Config
from codex_auto_review.git import Change, snapshot
from codex_auto_review.reviewer import Finding, ReviewerError, chunks, run_review


def test_validate_findings() -> None:
    d = dict(priority="P1", file="x.py", line=2, title="Ошибка", explanation="Неправильное поведение", confidence=.7)
    assert Finding.parse(d, {"x.py"}).priority == "P1"
    with pytest.raises(ReviewerError):
        Finding.parse({**d, "file": "../outside.py"}, {"x.py"})
    with pytest.raises(ReviewerError):
        Finding.parse({**d, "confidence": 9}, {"x.py"})


def test_chunk_no_silent_truncation(repo: Path) -> None:
    base = snapshot(repo)
    (repo / "x.py").write_text("a = 1\n" * 1000)
    current = snapshot(repo)
    with pytest.raises(ReviewerError, match="требуется"):
        chunks(repo, base, current, [Change("x.py", 1000, 0)], Config(max_diff_chars_per_chunk=1100, max_chunks=1))
    chunks_out = chunks(repo, base, current, [Change("x.py", 1000, 0)], Config(max_diff_chars_per_chunk=1100, max_chunks=100))
    assert len(chunks_out) > 1
    assert all(len(x[1]) <= 1100 for x in chunks_out)


def test_codex_cli_command_and_result(monkeypatch, repo: Path) -> None:
    import codex_auto_review.reviewer as rev
    base = snapshot(repo)
    (repo / "x.py").write_text("a = 1\n")
    current = snapshot(repo)
    def fake_subprocess(command, **kwargs):
        assert "--sandbox" in command and "read-only" in command
        assert "--output-schema" in command and "--output-last-message" in command
        assert kwargs["env"]["CODEX_AUTO_REVIEW_CHILD"] == "1"
        assert "НЕДОВЕРЕННЫЙ DIFF" in kwargs["input"]
        path = Path(command[command.index("--output-last-message") + 1])
        path.write_text(json.dumps({"summary": "Проверено", "findings": []}))
        class Result:
            returncode = 0
            stderr = ""
        return Result()
    real_run = rev.subprocess.run
    def dispatch(command, **kwargs):
        return real_run(command, **kwargs) if command[0] == "git" else fake_subprocess(command, **kwargs)
    monkeypatch.setattr(rev.subprocess, "run", dispatch)
    assert run_review(repo, base, current, [Change("x.py", 1, 1)], Config()) == []
