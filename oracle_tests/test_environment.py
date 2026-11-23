"""The suite runs against the oracle versions pinned in pyproject.toml (the ones docs/dev/ORACLE_TESTS.md records),
and against no database."""

from __future__ import annotations

import os
import re
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_installed_oracles_are_the_pinned_versions():
    pins = dict(
        re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)", spec).groups()  # type: ignore[union-attr]
        for spec in tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["oracle"]
    )
    assert set(pins) == {"QuantLib", "arch", "scipy", "statsmodels", "mpmath"}
    assert {name: metadata.version(name) for name in pins} == pins


def test_no_database_is_configured():
    assert ".invalid/" in os.environ["FINRESEARCH_DATABASE_URL"]
