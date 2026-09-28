<!-- Describe what was broken or missing and what changes, in plain prose. Explain why when it is not obvious. -->

Closes #

## Validation

- [ ] `uv run ruff check . && uv run ruff format --check .` pass
- [ ] `uv run pytest -q` passes (offline suite + DB tests on `finresearch_test`)
- [ ] Each bug fixed has a regression test that fails without the fix
- [ ] `uv run python scripts/smoke_live.py` passes (bridge, local models or OCR changes)
- [ ] `uv run python scripts/smoke_mcp_live.py` passes (MCP server, claim ledger or agent changes)
- [ ] Gold-set comparison against the Moneyview / Orient Cables fact-check logs (report-engine changes; paste the summary)

## Checklist

- [ ] ENGINEERING_HANDOFF.md invariants still hold
- [ ] No assertion was weakened; intentional test changes are explained above
- [ ] PRODUCT_REQUIREMENTS.md verification row and docs/FUNCTIONAL_TESTING.md entry added where relevant
- [ ] No secrets, `.env`, tokens, keys, downloaded documents or `data/` in the diff
