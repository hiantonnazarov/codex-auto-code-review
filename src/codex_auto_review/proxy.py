"""Прозрачный stdio relay для уже поддерживаемого Codex App Server JSONL.

Клиент подключается к этой программе ВМЕСТО запуска codex app-server.
Все JSON-RPC сообщения неизменными передаются в настоящий Codex App Server.
Копируются ТОЛЬКО события turn/started, turn/diff/updated, turn/completed.
Подключаться к уже запущенному стороннему Codex Desktop через этот relay нельзя.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO

from .appserver import AppServerError, ingest
from .config import Config


def forward_input(source: BinaryIO, destination: BinaryIO) -> None:
    try:
        for line in source:
            destination.write(line)
            destination.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            destination.close()
        except OSError:
            pass


def forward_output(source: BinaryIO, destination: BinaryIO,
                   home: Path | None = None) -> None:
    for raw_line in source:
        try:
            message = json.loads(raw_line)
            if isinstance(message, dict):
                ingest(message, home)
        except (UnicodeError, ValueError, AppServerError, OSError) as exc:
            # Ошибка захвата diff видна через stderr, RPC-поток никогда не меняем.
            print(f"codex-auto-review: ошибка события app-server: {exc}",
                  file=sys.stderr, flush=True)
        destination.write(raw_line)
        destination.flush()


def run_proxy(config: Config, *, home: Path | None = None,
              source: BinaryIO | None = None, destination: BinaryIO | None = None) -> int:
    executable = shutil.which(config.codex_command) or config.codex_command
    env = dict(os.environ)
    # Запущенный дочерний reviewer НЕ должен перехватываться этим relay.
    process = subprocess.Popen(
        [executable, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=None, env=env,
    )
    assert process.stdin is not None and process.stdout is not None
    upstream = source if source is not None else sys.stdin.buffer
    downstream = destination if destination is not None else sys.stdout.buffer
    sender = threading.Thread(
        target=forward_input, args=(upstream, process.stdin), daemon=True
    )
    sender.start()
    try:
        forward_output(process.stdout, downstream, home)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return process.returncode or 0
