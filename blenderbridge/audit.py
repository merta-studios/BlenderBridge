"""Append-only audit log plus an in-memory ring buffer for the UI.

The audit log answers "what did the agent do on my machine, when, and was it
approved?".  It is JSON Lines, rotated at a size limit, and every value is
redacted before it is written.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import protocol, security

MAX_AUDIT_BYTES = 4 * 1024 * 1024


class AuditLog:
    def __init__(self, directory: Path, max_bytes: int = MAX_AUDIT_BYTES, keep: int = 3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "audit.jsonl"
        self.max_bytes = max_bytes
        self.keep = keep
        self._lock = threading.Lock()
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=500)

    def write(self, record: Dict[str, Any]) -> None:
        entry = {
            "ts": protocol.now_iso(),
            **security.redact(record),
            "pid": os.getpid(),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self._recent.appendleft(entry)
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def _rotate_if_needed(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.keep - 1, 0, -1):
            src = self.directory / f"audit.{i}.jsonl"
            dst = self.directory / f"audit.{i + 1}.jsonl"
            if src.exists():
                os.replace(src, dst)
        os.replace(self.path, self.directory / "audit.1.jsonl")

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)[: max(1, min(limit, 500))]

    def tail(self, lines: int = 200) -> List[str]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.readlines()
            return data[-lines:]
        except OSError:
            return []


def setup_file_logger(directory: Path, name: str = "connector",
                      level: int = logging.INFO) -> logging.Logger:
    """Debug log written next to the audit log, with token redaction."""
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"blenderbridge.{name}")
    logger.setLevel(level)
    if logger.handlers:
        return logger
    handler = logging.FileHandler(directory / f"{name}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    class _RedactFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                record.msg = security.redact_text(str(record.msg))
            except Exception:  # noqa: BLE001
                pass
            return True

    logger.addFilter(_RedactFilter())
    return logger


class DebugLog:
    """Tiny in-memory log surfaced in the UI (last 200 lines)."""

    def __init__(self, capacity: int = 200):
        self._lines: Deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._lines.append(f"{stamp} [{level}] {security.redact_text(message, 500)}")

    def lines(self, limit: int = 200) -> List[str]:
        with self._lock:
            return list(self._lines)[-limit:]


DEBUG = DebugLog()
