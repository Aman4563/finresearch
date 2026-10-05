# FinResearch: rules for Claude working in this repo

- **Personal-use research engine.** Architecture and invariants: `docs/dev/ENGINEERING_HANDOFF.md`.
- **Principles:**
  - The LLM reads and writes; Python computes and verifies.
  - The primary document beats secondary sources.
  - Cite page/line or URL+timestamp for every number.
  - Mark INTERIM / UNVERIFIED.
  - Never silently truncate or fabricate.
- **Claude access goes only through the official `claude` CLI / Agent SDK** (`finresearch.bridge`). Never extract OAuth tokens, and never add third-party proxies or "bridges" that re-expose the subscription.
- **Never read or print secrets** (`.env`, credential folders, keychains). `data/` is gitignored; don't commit PDFs.
- **Personal data:** never read, print, commit or send to a model the user's portfolio, statements, AIS, journal or
  wealth data. Tests use synthetic fixtures (made-up names, fake ISINs/PANs); the repository is public.
- **Tests and live services:** the live app uses the `finresearch` DB on 127.0.0.1:8710/3100. Tests and any extra
  server must use a `*_test` database (`FINRESEARCH_TEST_DATABASE_URL`); parallel work uses its own worktree and DB.
  Never `git stash` in worktrees (the stash is shared).
- **pdftotext line numbers:** always use `finresearch.ingest.text.read_lines()`, never `str.splitlines()`, because of `\f`.
- **Before committing:**
  - `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
  - Offline tests must not call the real CLI or Ollama; live checks live in `scripts/smoke_live.py`.
- Workflow (mirrors Aman4563/lumen-ai-notes): issue first → `feat|fix|chore|docs|ci|perf|spike/<name>` branch →
  imperative commit subjects → PR from the template with `Closes #N` → merge commit, branch auto-deleted. Never push
  to `main`. Append a dated row to docs/dev/PRODUCT_REQUIREMENTS.md when a requirement ships; add
  docs/dev/FUNCTIONAL_TESTING.md entries for reproduced bugs. Invariants: docs/dev/ENGINEERING_HANDOFF.md.
  Community files live in .github/.
