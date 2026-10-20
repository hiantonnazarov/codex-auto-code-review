import subprocess
from pathlib import Path
import pytest

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    p = tmp_path / "work"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "test"], check=True)
    (p / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(p), "add", "."], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-qm", "initial"], check=True)
    return p
