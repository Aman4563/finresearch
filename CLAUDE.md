# FinResearch: rules for Claude working in this repo

- **Personal-use research engine.** Architecture: `../FinResearch_App_Blueprint/02_Final_Architecture_v1.2.md`.
- **Principles:**
  - The LLM reads and writes; Python computes and verifies.
  - The primary document beats secondary sources.
  - Cite page/line or URL+timestamp for every number.
  - Mark INTERIM / UNVERIFIED.
  - Never silently truncate or fabricate.
- **Claude access goes only through the official `claude` CLI / Agent SDK** (`finresearch.bridge`). Never extract OAuth tokens, and never add third-party proxies or "bridges" that re-expose the subscription.
- **Never read or print secrets** (`.env`, `~/Desktop/credential folders`, keychains). `data/` is gitignored; don't commit PDFs.
- **pdftotext line numbers:** always use `finresearch.ingest.text.read_lines()`, never `str.splitlines()`, because of `\f`.
- **Before committing:**
  - `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
  - Offline tests must not call the real CLI or Ollama; live checks live in `scripts/smoke_live.py`.
