"""Text helpers for pdftotext output.

IMPORTANT: pdftotext emits form-feed (\\f) page breaks. Python's str.splitlines() treats \\f as a line
break but grep/sed do not, which silently shifts line numbers. Always use read_lines() so citations
(document, line) match `grep -n` / `sed -n` exactly.
"""

from __future__ import annotations

from pathlib import Path


def read_lines(path: Path | str) -> list[str]:
    """Lines split on '\\n' only (1-based line N == result[N-1], same as grep -n / sed -n)."""
    return Path(path).read_text(encoding="utf-8", errors="replace").split("\n")


def excerpt(lines: list[str], first_line: int, last_line: int) -> str:
    """Inclusive 1-based line range, matching `sed -n 'first,last p'`."""
    return "\n".join(lines[first_line - 1 : last_line])
