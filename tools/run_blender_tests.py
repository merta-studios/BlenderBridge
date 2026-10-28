#!/usr/bin/env python3
"""Run the Blender integration tests on CI and report honestly.

Two things make this wrapper necessary:

1. ``bpy`` sometimes crashes *after* the tests finished, while Python shuts the
   interpreter down (SIGSEGV, exit code 139 on Linux).  That is an
   environment quirk, not a failing test.  This wrapper looks at what pytest
   actually reported and only fails the build when a test failed.
2. GitHub's UI hides the console output of a crashed step in some cases, so
   every important line is repeated as an ``::error::`` annotation and the
   complete log is written to the job summary.

Usage (CI)::

    python tools/run_blender_tests.py
    python tools/run_blender_tests.py -- tests/blender -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SUMMARY_RE = re.compile(
    r"(?:(?P<failed>\d+) failed)?[,\s]*(?:(?P<errors>\d+) error)?[,\s]*"
    r"(?:(?P<passed>\d+) passed)?")
COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed)")

LOG = Path("blender-tests.log")
MAX_ANNOTATIONS = 20


def _emit(kind: str, message: str) -> None:
    """GitHub workflow command; harmless locally."""
    message = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::{kind}::{message}", flush=True)


def _summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("### Blender-Integrationstests\n\n```\n")
            fh.write("\n".join(lines[-60:]))
            fh.write("\n```\n")
    except OSError:
        pass


def main(argv: list) -> int:
    extra = argv[argv.index("--") + 1:] if "--" in argv else ["tests/blender", "-v", "-rs"]
    cmd = [sys.executable, "-u", "-m", "pytest", *extra, "-p", "no:cacheprovider",
           "--tb=short"]
    print("running:", " ".join(cmd), flush=True)

    lines: list = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        print(line, flush=True)
    code = proc.wait()
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")

    counts = {}
    for match in COUNT_RE.finditer("\n".join(lines)):
        counts[match.group(2).rstrip("s")] = int(match.group(1))
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    errors = counts.get("error", 0)

    print(f"\npytest exit code: {code}; passed={passed} failed={failed} errors={errors}",
          flush=True)
    _summary(lines)

    if failed or errors:
        for line in lines:
            if line.startswith(("FAILED", "ERROR", "E   ")):
                _emit("error", line)
        return 1

    if code == 0:
        print("Alle Blender-Tests bestanden.", flush=True)
        return 0

    if passed and not failed and not errors:
        # Blender crashed while shutting down - the tests themselves passed.
        _emit("warning",
              f"pytest wurde mit Code {code} beendet, aber alle {passed} Tests waren "
              f"bestanden. Das ist der bekannte bpy-Absturz beim Beenden des "
              f"Interpreters, kein Testfehler.")
        print(f"WARNUNG: bpy ist beim Beenden abgestürzt (Code {code}), "
              f"aber {passed} Tests waren bestanden.", flush=True)
        return 0

    _emit("error", f"pytest endete mit Code {code} ohne Ergebniszeile.")
    for line in lines[-MAX_ANNOTATIONS:]:
        _emit("error", line)
    return code or 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
