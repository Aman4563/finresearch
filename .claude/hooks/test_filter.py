#!/usr/bin/env python3
"""Pipe filter for pytest and Playwright output in Claude Code sessions (wired by guard.py's pre-bash mode).

Only noise is dropped: pytest progress-dot lines, its warnings summary and Playwright's passing-test lines. Every
failure, error, traceback, skip reason and the final summary line pass through unchanged, and a closing note says
how many lines were hidden. The command runs under `set -o pipefail`, so its exit code survives the pipe. Stdlib
only and Python 3.9-compatible, like guard.py.
"""

from __future__ import annotations

import re
import sys

PROGRESS = re.compile(
    r"^(\S+\s)?[.sSxXFE]+\s*(\[\s*\d+%\])?\s*$"
)  # "tests/test_x.py ....s.. [ 12%]" or bare dots
PASSED_V = re.compile(r"\sPASSED(\s+\[\s*\d+%\])?\s*$")  # pytest -v
PW_PASSED = re.compile(
    r"^\s*(✓|ok)\s+\d+\s"
)  # Playwright list reporter: "  ✓  12 [desktop] › e2e/x.spec.ts ..."
SECTION = re.compile(r"^=+ (.+?) =+$")


def main() -> int:
    hidden = 0
    in_warnings = False
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        header = SECTION.match(line)
        if header:
            in_warnings = header.group(1).strip().lower() == "warnings summary"
            if in_warnings:
                hidden += 1
                continue
        elif in_warnings:
            hidden += 1
            continue
        if "F" not in line and "E" not in line and PROGRESS.match(line) and line.strip():
            hidden += 1
            continue
        if PASSED_V.search(line) or PW_PASSED.match(line):
            hidden += 1
            continue
        sys.stdout.write(raw)
        sys.stdout.flush()
    if hidden:
        print(
            f"[test filter: {hidden} passing/progress/warning lines hidden; failures and the summary are complete]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
