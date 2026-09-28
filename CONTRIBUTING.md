# Contributing to FinResearch

FinResearch is a personal, local-first research engine that produces deep, fact-checked investment research. It starts with Indian IPOs and will extend to stocks, mutual funds, bonds and F&O.

Most changes touch one of these areas:
- the Claude Bridge (`bridge/`);
- documents and search (`ingest/`);
- data sources (`adapters/`);
- calculations (`fincalc/`);
- the MCP tools and claim ledger (`mcp_server/`, `db/`);
- later, the research agents and the app.

This guide covers the workflow, the gates a change must pass, and the constraints that are easy to break.

## Getting set up

See **Setup** in [README.md](README.md). You need:
- macOS with Homebrew `postgresql@17`, `pgvector`, `tesseract` and `poppler`;
- Ollama with the models listed there;
- `uv`;
- Claude Code logged in with the Max subscription (`claude auth status` shows `"authMethod": "claude.ai"`).

Never commit `.env`, anything under `data/`, downloaded documents, tokens or keys.

## Workflow

1. **Open or pick an issue** that describes the problem and its acceptance criteria. Use the issue forms: bug, feature or research-quality. Put it in the right milestone (`vX.Y.Z — …`) and give it `area:*` and `type:*` labels.
2. **Branch from `main`** (for example `feat/ipo-agents` or `fix/nse-cookie-rewarm`). Keep one coherent change per pull request. Prefixes: `feat/ fix/ chore/ docs/ ci/ perf/ spike/`. Spikes are throwaway and never merged as-is.
3. **Write concise, imperative commit subjects** ("Route MCP tool names only through --allowedTools"), with a short body explaining why when it isn't obvious. No `feat:` prefixes or ticket numbers in the subject.
4. **Open a pull request against `main`** using the template, and link the issue with `Closes #N`. CI runs on every pull request.
5. **Keep the branch up to date with `main`** before merging. Pull requests are **merged with a merge commit** and the branch is deleted automatically. Never push directly to `main`.

## Checkpoints and releases

Each milestone ends with a release:
1. A `chore/release-X.Y.Z` pull request updates the version in `pyproject.toml` and the PRODUCT_REQUIREMENTS verification log.
2. After it merges, tag `vX.Y.Z` on `main`.
3. Publish a GitHub Release whose notes group the highlights by area and reference the pull requests (`gh release create vX.Y.Z --title "FinResearch vX.Y.Z" --notes-file …`).

Versions follow semantic versioning; before 1.0, a minor bump is a milestone.

## The gates

Run these before asking for review:

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                                  # offline suite + DB tests on finresearch_test
```

**Live acceptance runs**, which are local only because CI has no Claude login or Ollama:

| Change touches | Run |
|---|---|
| Bridge, local models, OCR | `uv run python scripts/smoke_live.py` |
| MCP server, claim ledger, agents | `uv run python scripts/smoke_mcp_live.py` |
| Report engine | Gold-set comparison against the Moneyview and Orient Cables fact-check logs; paste the summary in the PR |

**Testing rules:**
- Add a regression test for every bug you fix, and make sure it fails against the code before your fix.
- Never weaken an existing assertion to make a change pass. Update an assertion only when the behaviour changed on purpose, and say so in the pull request.
- Offline tests never call the real `claude` CLI, Ollama, NSE or SEBI. Use the fake CLI, `respx` and recorded fixtures.

## Constraints to respect

- **Invariants.** [ENGINEERING_HANDOFF.md](ENGINEERING_HANDOFF.md) lists the data, citation, engine and security invariants every change must preserve.
- **Claude access** goes only through the official `claude` CLI or Agent SDK under the owner's login, for personal use. Never add code that extracts OAuth tokens or re-exposes the subscription as an API.
- **16 GB RAM.** Local generation is single-flight. Don't load two large models at once, and don't run Ollama or parsers inside Docker (no Metal GPU).
- **Line numbers** come from `ingest.text.read_lines()` on a document's canonical `text.txt`. Never use `str.splitlines()` (form feeds).
- **Schema changes** need an Alembic migration (`uv run alembic revision --autogenerate -m "…"`), applied to both `finresearch` and `finresearch_test`.

## Tracking status

[PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md) is the status authority.
- When a change ships or verifies a requirement, append a dated row to its verification log. Never rewrite earlier rows.
- Add a short dated entry to [docs/FUNCTIONAL_TESTING.md](docs/FUNCTIONAL_TESTING.md) for each bug you reproduced, saying how it is now checked.
