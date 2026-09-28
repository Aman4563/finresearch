from pathlib import Path

from finresearch.bridge.ollama_engine import find_repetition
from finresearch.ingest.layout_table import parse_layout_table
from finresearch.ingest.text import excerpt, read_lines

FIX = Path(__file__).parent / "fixtures"


def rows(table):
    return {label: vals for label, vals in table.rows}


def test_orient_pnl_multiline_headers_and_values():
    t = parse_layout_table((FIX / "orient_rhp_pnl_layout.txt").read_text())
    assert t.periods == [
        "3M ended 2026-06-30",
        "FY ended 2026-03-31",
        "FY ended 2025-03-31",
        "FY ended 2024-03-31",
    ]
    r = rows(t)
    assert r["(a) Revenue from Operations"] == ["4,891.56", "11,716.54", "8,249.58", "6,577.67"]
    assert r["Profit / (Loss) for the year"] == ["327.83", "535.61", "533.21", "400.69"]
    assert r["Exceptional items"] == ["-", "-", "-", "-"]
    assert "million" in t.unit_note


def test_moneyview_pnl_shifted_rows_and_note_column():
    t = parse_layout_table((FIX / "moneyview_rhp_pnl_layout.txt").read_text())
    assert t.periods == ["3M ended 2026-06-30", "3M ended 2025-06-30", "FY ended 2026-03-31",
                         "FY ended 2025-03-31", "FY ended 2024-03-31"]  # fmt: skip
    r = rows(t)
    fees = next(v for k, v in r.items() if "Fees and commission" in k)
    assert fees == ["6,328.65", "3,917.61", "18,994.55", "14,867.98", "10,153.77"]
    exc = next((k, v) for k, v in r.items() if k.startswith("Exceptional items, (gain)"))
    assert exc[1] == ["(2.32)", "-", "2,066.53", "-", "-"] and "[note 38]" in exc[0]
    assert "Million" in t.unit_note


def test_markdown_uses_period_labels():
    md = parse_layout_table((FIX / "orient_rhp_pnl_layout.txt").read_text()).to_markdown()
    assert "| Particulars | 3M ended 2026-06-30 | FY ended 2026-03-31 |" in md


def test_read_lines_ignores_form_feeds(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("a\fb\nc\n")
    lines = read_lines(p)
    assert lines[0] == "a\fb" and excerpt(lines, 2, 2) == "c"


def test_find_repetition_paragraph_and_token_loops():
    para = "This is a sufficiently long paragraph of OCR text."
    assert find_repetition(f"{para}\nOther line that is also long enough here.\n{para}\n") is not None
    assert find_repetition("Act, " + "2013, " * 20) is not None
    assert find_repetition(f"{para}\nA different long paragraph without loops in it.") is None
