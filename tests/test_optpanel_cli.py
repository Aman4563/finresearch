"""`finresearch fno panel ...` (P1.12, #374): the commands exist, and the downloads refuse until the owner records a
data-rights decision. No network."""

from __future__ import annotations

from typer.testing import CliRunner

from finresearch.cli import app


def test_panel_commands_are_listed() -> None:
    out = CliRunner().invoke(app, ["fno", "panel", "--help"]).output
    for cmd in ("build", "mwpl", "ban-check", "coverage", "reconcile"):
        assert cmd in out, cmd


def test_mwpl_backfill_refuses_without_a_decision(env) -> None:
    r = CliRunner().invoke(app, ["fno", "panel", "mwpl", "--from", "2026-03-02", "--to", "2026-03-03"])
    assert r.exit_code != 0 and "no recorded data-rights decision" in r.output


def test_build_refuses_the_vault(env) -> None:
    r = CliRunner().invoke(app, ["fno", "panel", "build", "--from", "2021-12-01"])
    assert r.exit_code != 0 and "vault" in r.output
