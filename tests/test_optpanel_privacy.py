"""The market-wide options panel reveals no portfolio and never reaches a model (P1.12, #374): static guards. The
panel package reads no held or watched list, and no prompt-building or agent-tool package imports the panel or its
tables (the panel is not personal, but the owner asked that it never be sent to a model)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "finresearch"
PANEL_MODELS = {"OptPanelEod", "OptPanelRow", "OptPanelDay", "MwplOi", "FnoBanNotice", "OptPanelFileDay"}
LLM_PACKAGES = ("agents", "orchestrator", "mcp_server", "bridge", "verify", "ingest", "render", "suggest")


def _imports(f: Path) -> list[tuple[str, set[str]]]:
    out = []
    for node in ast.walk(ast.parse(f.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, {a.name for a in node.names}))
        elif isinstance(node, ast.Import):
            out += [(a.name, set()) for a in node.names]
    return out


def test_llm_paths_never_import_the_panel() -> None:
    bad = []
    for pkg in LLM_PACKAGES:
        for f in (SRC / pkg).rglob("*.py"):
            for mod, names in _imports(f):
                if mod.startswith("finresearch.optpanel") or mod == "finresearch.adapters.nse_mwpl":
                    bad.append(f"{f.relative_to(SRC)}: {mod}")
                if mod == "finresearch.db.models" and names & PANEL_MODELS:
                    bad.append(f"{f.relative_to(SRC)}: {sorted(names & PANEL_MODELS)}")
                if mod == "finresearch.optpanel" or "optpanel" in names:
                    bad.append(f"{f.relative_to(SRC)}: {mod}")
    assert not bad, bad


def test_the_panel_reads_no_held_or_watched_list() -> None:
    bad = []
    for f in (SRC / "optpanel").rglob("*.py"):
        for mod, names in _imports(f):
            if mod.startswith(("finresearch.portfolio", "finresearch.wealth", "finresearch.fnobook",
                               "finresearch.disclosures")):  # fmt: skip
                bad.append(f"{f.name}: {mod}")
            if names & {"tracked", "tracked_stocks", "keeper"}:
                bad.append(f"{f.name}: {sorted(names)}")
        assert "tracked(" not in f.read_text(), f.name
    assert not bad, bad
