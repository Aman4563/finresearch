"""Live end-to-end smoke test of the Claude Bridge on this Mac.

1. Claude Max tier: tiny structured task (Haiku) -> checks login, schema output, rate-limit snapshot.
2. Local tier (forced): structured extraction from a real RHP excerpt with qwen3.5:9b.
3. Failover: simulate a Max limit -> router must route to local and mark the result degraded.
4. Embeddings (qwen3-embedding) and OCR (glm-ocr) on a scanned annual-report page.
Keeps Claude usage tiny (one Haiku call).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from finresearch.bridge import AgentTask, ModelClass, Tier, build_router

RESEARCH = Path(__file__).resolve().parents[2]
RHP_TXT = RESEARCH / "OrientCables_IPO_Research/_text_extracts/OrientCables_RHP_Sep2026.txt"
SCANNED_PDF = RESEARCH / "OrientCables_IPO_Research/02_Financial_Reports/Annual_Report_FY2024-25.pdf"

FIN_SCHEMA = {
    "type": "object",
    "properties": {
        "periods": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "period": {"type": "string"},
                    "revenue_from_operations_mn": {"type": ["number", "null"]},
                    "profit_after_tax_mn": {"type": ["number", "null"]},
                },
                "required": ["period", "revenue_from_operations_mn", "profit_after_tax_mn"],
            },
        }
    },
    "required": ["periods"],
}


def ok(msg):
    print(f"  ✅ {msg}")


def fail(msg):
    print(f"  ❌ {msg}")


async def main() -> int:
    router = build_router()
    failures = 0

    print("1) Claude Max tier (Haiku, structured)")
    try:
        res = await router.run(
            AgentTask(name="smoke-max", prompt="Return the NSE full form and the number 272.",
                      json_schema={"type": "object", "properties": {"full_form": {"type": "string"},
                                   "n": {"type": "integer"}}, "required": ["full_form", "n"]},
                      model_class=ModelClass.FAST, max_turns=3),
            force_tier=Tier.CLAUDE_MAX,
        )  # fmt: skip
        rl = res.rate_limit
        ok(f"{res.model}: {res.structured_output}  ({res.duration_s:.1f}s, est ${res.cost_usd_estimate:.3f})")
        if rl:
            ok(
                f"5h window {rl.five_hour_utilization:.0%}, 7d window {rl.seven_day_utilization:.0%}, status={rl.status}"
            )
    except Exception as e:
        failures += 1
        fail(f"{type(e).__name__}: {e}")

    print("2) Local tier (qwen3.5:9b) — real RHP P&L, normalised by ingest.layout_table")
    from finresearch.ingest.layout_table import parse_layout_table
    from finresearch.ingest.text import excerpt, read_lines

    table = parse_layout_table(excerpt(read_lines(RHP_TXT), 4932, 4970))  # sed -n '4932,4970p'
    expected = {
        "3M ended 2026-06-30": (4891.56, 327.83),
        "FY ended 2026-03-31": (11716.54, 535.61),
        "FY ended 2025-03-31": (8249.58, 533.21),
        "FY ended 2024-03-31": (6577.67, 400.69),
    }
    try:
        res = await router.run(
            AgentTask(name="smoke-local-extract", model_class=ModelClass.STANDARD, json_schema=FIN_SCHEMA,
                      prompt="Extract revenue from operations and profit after tax for every period column of "
                             "this table. Use the column headers exactly as the period value.\n\n"
                             + table.to_markdown()),
            force_tier=Tier.LOCAL,
        )  # fmt: skip
        ok(f"{res.model} in {res.duration_s:.1f}s, warnings={res.warnings}")
        got = {p["period"]: (p["revenue_from_operations_mn"], p["profit_after_tax_mn"])
               for p in res.structured_output["periods"]}  # fmt: skip
        bad = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
        if bad:
            failures += 1
            fail(f"mismatches vs RHP: {bad}")
        else:
            ok("all 8 values match the RHP exactly")
    except Exception as e:
        failures += 1
        fail(f"{type(e).__name__}: {e}")

    print("3) Failover: simulated Max limit -> local, marked degraded")
    with tempfile.TemporaryDirectory() as td:
        from finresearch.config import Settings

        s = Settings(state_dir=Path(td))
        r2 = build_router(s)
        r2.tracker.record_limit(Tier.CLAUDE_MAX, int(time.time()) + 600, "simulated limit")
        try:
            res = await r2.run(AgentTask(name="smoke-failover", model_class=ModelClass.FAST,
                                         prompt="Classify sentiment of: 'IPO subscribed 32x, GMP firm'.",
                                         json_schema={"type": "object", "properties": {"sentiment": {
                                             "enum": ["positive", "neutral", "negative"]}},
                                             "required": ["sentiment"]}))  # fmt: skip
            assert res.tier is Tier.LOCAL and res.degraded
            ok(f"routed to {res.tier.value} ({res.model}) degraded={res.degraded}: {res.structured_output}")
            ok(f"attempts: {res.attempts}")
        except Exception as e:
            failures += 1
            fail(f"{type(e).__name__}: {e}")

    print("4) Embeddings + OCR")
    local = router.engines[Tier.LOCAL]
    try:
        vecs = await local.embed(["Orient Cables networking cables", "Moneyview digital lending NBFC"])
        ok(f"embeddings: {len(vecs)} x {len(vecs[0])} dims")
    except Exception as e:
        failures += 1
        fail(f"embed {type(e).__name__}: {e}")
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-r", "110", "-f", "5", "-l", "5", "-png", str(SCANNED_PDF),
                            f"{td}/p"], check=True)  # fmt: skip
            img = sorted(Path(td).glob("p*.png"))[0]
            ocr = await local.ocr_image(img)
            ok(f"OCR page 5 of scanned AR: {ocr!r} -> {ocr.text[:120]!r}")
            if len(ocr.text) < 500 or len(ocr.text) > 8000:
                failures += 1
                fail("OCR length implausible for one page")
    except Exception as e:
        failures += 1
        fail(f"ocr {type(e).__name__}: {e}")

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILURE(S)'}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
