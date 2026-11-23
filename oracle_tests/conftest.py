"""The ORACLE suite (#503): the in-house maths compared with mature outside libraries over grids of inputs.

Run it with one command (the libraries live in the uv group `oracle`, which nothing else installs):

    uv run --group oracle pytest oracle_tests -q

This folder is outside `testpaths`, so the normal `uv run pytest` never collects it and its counts do not move. Rules
of the suite (docs/dev/ORACLE_TESTS.md has the licences, the comparison table and the findings):

* synthetic or hand-made inputs only: no market data, no database, no `data/`;
* every comparison states its tolerance and why (floating point, an optimiser's stopping rule, a documented
  convention), and reports the largest difference it saw through the `oracle` fixture, so the table printed at the
  end of the run is measured, not typed;
* a difference beyond tolerance is a finding to explain: (a) an in-house bug, (b) a convention difference, or (c) an
  oracle limitation. It is never tuned away with a loose tolerance.
"""

from __future__ import annotations

import importlib.util
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# import name -> what `uv run --group oracle` installs (pyproject.toml [dependency-groups].oracle)
ORACLES = {
    "QuantLib": "QuantLib",
    "arch": "arch",
    "scipy": "scipy",
    "statsmodels": "statsmodels",
    "mpmath": "mpmath",
}
RUN = "uv run --group oracle pytest oracle_tests -q"

# No test here may reach a database: the default URL is the owner's live one, so point it at a host that cannot exist.
os.environ["FINRESEARCH_DATABASE_URL"] = "postgresql+psycopg://oracle-suite-uses-no-database.invalid/none"


def pytest_addoption(parser):
    parser.addoption(
        "--oracle-report", default=None, help="also write the comparison table (Markdown) to this path"
    )


def pytest_configure(config):
    missing = [name for name in ORACLES if importlib.util.find_spec(name) is None]
    if missing:  # a skip here would read as a pass: the suite refuses to run without its oracles
        raise pytest.UsageError(f"oracle libraries not installed: {', '.join(missing)}. Run: {RUN}")
    config.stash[_ROWS] = []


@dataclass
class Row:
    function: str
    oracle: str
    grid: int
    max_diff: float
    tolerance: str
    verdict: str
    order: int = field(default=0, compare=False)


_ROWS = pytest.StashKey[list]()


class Recorder:
    """Collects one row per comparison for the table at the end of the run."""

    def __init__(self, rows: list[Row]):
        self._rows = rows

    def add(self, function: str, oracle: str, *, grid: int, max_diff: float, tolerance: str, verdict: str):
        """`max_diff` is the largest difference seen (in the unit the tolerance names); `verdict` is `agree`,
        or the class of the finding: `(a) bug: ...`, `(b) convention: ...`, `(c) oracle limit: ...`."""
        assert grid > 0, "a comparison over an empty grid proves nothing"
        assert math.isfinite(max_diff), f"{function}: the largest difference must be a number, got {max_diff}"
        self._rows.append(Row(function, oracle, grid, float(max_diff), tolerance, verdict, len(self._rows)))


@pytest.fixture
def oracle(request) -> Recorder:
    return Recorder(request.config.stash[_ROWS])


def table(rows: list[Row]) -> str:
    lines = [
        "| Function | Oracle | Grid | Max difference | Tolerance | Verdict |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for r in sorted(rows, key=lambda r: r.order):
        lines.append(
            f"| {r.function} | {r.oracle} | {r.grid:,} | {r.max_diff:.3g} | {r.tolerance} | {r.verdict} |"
        )
    return "\n".join(lines)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    rows = config.stash.get(_ROWS, [])
    if not rows:
        return
    text = table(rows)
    terminalreporter.section("oracle comparisons")
    terminalreporter.write_line(text)
    out = config.getoption("--oracle-report")
    if out:
        Path(out).write_text(text + "\n")
