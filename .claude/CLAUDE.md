# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

FinResearch is a personal, local-first research app for Indian markets: fact-checked multi-agent research reports
(IPOs, stocks, funds, bonds), live market data and signals, a local portfolio and tax tracker, and F&O analysis. One
Mac runs everything. Python 3.12 + FastAPI + SQLAlchemy/Alembic + Postgres 17/pgvector (`src/finresearch`), Next.js 16
/ React 19 / Tailwind 4 (`web/`). The repository is public. Invariants every change must keep:
`docs/dev/ENGINEERING_HANDOFF.md`; design: `docs/ARCHITECTURE.md`; CLI reference: `docs/USAGE.md`.

## Commands

Claude Code's Bash keeps no shell variables between calls: set `T` in the same command that uses it (an empty URL
skips every database test while pytest still exits 0, so read `-rs`).

```bash
uv sync --locked                                     # as CI
uv run ruff check . && uv run ruff format --check .  # lint + format as CI; fix: uv run ruff check --fix . && uv run ruff format .

# Tests: your OWN database whose name ends in _test (conftest drops and recreates its schema every session)
createdb finresearch_<you>_test && psql -d finresearch_<you>_test -c 'create extension if not exists vector'
T=postgresql+psycopg://localhost/finresearch_<you>_test; FINRESEARCH_TEST_DATABASE_URL=$T FINRESEARCH_DATABASE_URL=$T uv run pytest -q -rs
T=postgresql+psycopg://localhost/finresearch_<you>_test; FINRESEARCH_TEST_DATABASE_URL=$T FINRESEARCH_DATABASE_URL=$T uv run pytest -q -rs tests/test_x.py::test_name   # or -k '<expr>'
uv run pytest --collect-only -q                      # list tests; needs no database
# the full suite is serial and takes ~6 min: give the Bash call a 600000 ms timeout or run it in the background.
# tests shell out to pdftotext/pdftoppm and tesseract (brew install poppler tesseract; CI apt-installs them).

# Migrations: tests build their schema with create_all, so only CI's Alembic round trip catches a missing migration.
# Use a SECOND scratch database built by Alembic (a pytest-built one has no alembic_version).
createdb finresearch_<you>_mig_test
M=postgresql+psycopg://localhost/finresearch_<you>_mig_test; export FINRESEARCH_DATABASE_URL=$M; uv run alembic upgrade head && uv run alembic revision --autogenerate -m "Title (#N): tables"
uv run ruff check --fix migrations && uv run ruff format migrations   # the template's output fails CI's ruff otherwise
M=postgresql+psycopg://localhost/finresearch_<you>_mig_test; export FINRESEARCH_DATABASE_URL=$M; uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade -1 && uv run alembic upgrade head && uv run alembic check   # CI's gate (on a fresh DB: dropdb/createdb first)
uv run alembic heads                                 # must print exactly one head; needs no database

# Web (in web/)
pnpm install --frozen-lockfile
pnpm typecheck && pnpm lint                          # typecheck = next typegen && tsc --noEmit (raw tsc lacks the generated types)
NEXT_PUBLIC_FINRESEARCH_API=http://127.0.0.1:8799 pnpm build   # e2e runs `next start` on the LAST build: rebuild after changes
E2E_PORT=3051 pnpm e2e                               # outside CI Playwright reuses any server already on the port (default 3005)
E2E_PORT=3051 pnpm exec playwright test e2e/fno-plan.spec.ts --project=desktop   # one spec; -g '<title>' for one test
pnpm exec playwright install chromium                # once, after a Playwright upgrade

uv run finresearch --help                            # every group and command has --help
```

Procedures are slash commands in `.claude/skills/` (`/feature`, `/live-check`, `/new-migration`, `/ship`, `/release`) and
subagents in `.claude/agents/` (`implementer`, `invariant-auditor`, `gold-reviewer`). Research sandboxes sit inside the
repo and see both, so every repo skill keeps `disable-model-invocation: true` and no role gets the Agent tool
(`tests/test_claude_config.py`).

Live checks are local only (CI has no Claude login or Ollama) and spend real Claude usage: a full IPO research run
uses most of a 5-hour Max window, so check `uv run finresearch bridge limits` first and live-check one instrument per
asset class (Orient Cables for IPOs). `uv run python scripts/smoke_live.py` covers the bridge, local models, OCR and
CLAUDE.md isolation (no database); `scripts/smoke_mcp_live.py` covers the MCP tools, claim ledger and agents (writes
to the configured database). Report-engine changes also need a gold-set score: `uv run finresearch eval gold <run_id>`
against `evals/gold/` (summary in the PR). `eval replay <fixture>` re-scores an exported run without Claude or the
network but imports it as a new company and run into the configured database: point `FINRESEARCH_DATABASE_URL` at a
scratch database built by Alembic (like `M` above).

CI (`.github/workflows/ci.yml`) runs on every PR and push to `main`, but is not a required check: confirm it is green
before merging. It runs ruff, the Alembic round trip above, pytest, the web typecheck, lint, build and Playwright e2e,
and gitleaks over the full history of every branch (so a fake secret pushed to any branch breaks every PR).

## Architecture

**Processes.** `finresearch serve` is one uvicorn process on 127.0.0.1:8710 whose lifespan also runs the monitor
scheduler (`monitor/scheduler.tick`, every minute) and an auto-resume loop for runs paused by transient errors
(`--no-monitor` turns both off; `finresearch monitor run` runs only the scheduler). The Next.js app only serves pages:
client components call the API from the browser, and `web/src/proxy.ts` (Next 16's renamed middleware) puts the token
from `data/state/api_token` in an httpOnly cookie `finresearch_token_<api port>` (the cookie is host-scoped, so
`proxy.ts` redirects a localhost visit to 127.0.0.1). Non-browser clients send `Authorization: Bearer`
(`api/auth.client_headers()`); the CLI never calls the API, it works on the database directly. Research and Strategy
Lab runs started from the app are detached workers (`python -m finresearch.cli ipo resume <id> --wait`, `... fno lab
run-job <id>`), so the `if __name__ == "__main__": app()` guard must stay the last statement of `cli.py`
(`tests/test_cli_module.py`).

**API** (`api/app.py` `create_app`). No APIRouter: core routes are closures inside `create_app`, and each route module
`api/<x>.py` exposes one `add_*_routes(app, ...)` that `create_app` calls; its keyword arguments (`spawner`, `router`,
`monitor_deps`, `clock`, `api_token`, ...) are the test seams. Every route except `/api/health` and the brokers' OAuth
callback needs the token. Unsafe methods from a browser need an allowed Origin and `X-FinResearch: 1`, which `api()`
adds; it deliberately sends no custom header on GETs (that would preflight every read). `ValueError` → 422, upstream
`NseError`/`SebiError`/`AmfiError`/`httpx.HTTPError` → 502, anything else → JSON 500; bodies go through
`api/errors.public_error`, which callers re-raising exceptions must also use.

**Configuration.** `config.Settings` (pydantic-settings, prefix `FINRESEARCH_`, plus the checkout's `.env`);
`get_settings()` and `db.get_engine()` are lru-cached. The default `FINRESEARCH_DATABASE_URL` is the live
`finresearch` database. `.env` and every `data/` path (gitignored: state, runs, docs, reports, portfolio imports,
caches, backups) resolve against the checkout the code runs from (`config.REPO_ROOT`; logs go wherever `--log-file`
points), so a worktree has its own empty `data/` but still defaults to the live database. All tables are in
`db/models.py`; migrations in `migrations/`.

**Monitor.** `monitor/jobs.Deps` injects the exchange fetchers and a flag per background pass (all off by default;
`Deps.live()` turns them on), so tests stay offline. A scheduled job has `due_slot(now)` in its module, claims an
`alert_eval_slot` row through `monitor/disclosures.claim`/`finish` (so two monitor processes never run it twice; a
failed pass gets at most `MAX_ATTEMPTS` attempts, `RETRY_AFTER` apart), a `Deps` flag, and a `_spawn_<job>` hook in
`tick`. After-close market-data jobs gate on `monitor/market_days.market_day`, which fails closed when the year's NSE
holiday list is unknown; the IPO watch plan, archives, portfolio pass, brief, alert rules and broker syncs
deliberately treat an unknown list as no holidays (skipping them would lose data). Choose the policy on purpose for a
new job.

**Research pipeline.** `orchestrator/` runs one resumable DAG per run, with a subclass per kind (IPO, stock, fund,
bond): deterministic facts → planner → research streams in parallel, each followed by the deterministic gate, an
adversarial verifier and, for high-importance claims it passed, a blind second verifier → cross-stream and
accounting-identity checks → bull ║ bear → synthesis (re-run while the publish gate blocks, `max_revisions`) → critic,
whose gaps send follow-up streams through the same checks (`max_followup_rounds`) → final publish gate. Model steps
are `agent_step` rows unique on `(run_id, key)` and deterministic post-steps run once under `_once` keys, so resumes
never repeat finished work and step keys are a contract. A Claude limit or the 5-hour budget pre-flight pauses a run;
it never fails over to a local model. `agents/roles.py` gives each role its model class, output schema and an explicit
tool allow-list (a new MCP tool is denied to every agent until a role lists it); prompts are `agents/prompts/*.md`,
skills `agents/skills/*/SKILL.md`. `bridge/` runs tasks through the official `claude -p` CLI on the subscription login
(auth env vars stripped; the API-key tier is off, Ollama serves embeddings and OCR). Agents run in sandboxes
`data/runs/<run>/<role>/` and load no CLAUDE.md (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`), so agent behaviour belongs in
`agents/`, never in this file. Their tools are the stdio MCP server (`mcp_server/`: documents, search, tables,
`fincalc`, exchange data, `fetch_page`, the claim ledger) plus WebSearch/WebFetch for web roles (`fetch_guard.py`
keeps them off this machine). `save_claim` checks every citation before writing, `verify/evidence.py` grades it (A–D,
U; see `docs/ARCHITECTURE.md`), and the publish gate writes `report.md` only if, among other checks, every cited
high-importance claim is verified with grade A, B or D (else `report_blocked.md`). A verifier's verdicts apply only to
the claims it was sent, and exchange/AMFI facts the pipeline fetched are never re-judged by a model.

**Markets and signals.** Exchange, regulator, AMFI and FBIL adapters use `adapters/http.PoliteClient`: a per-host rate
limit shared by every process with the same state dir (flock slots; all `*.nseindia.com` hosts share one budget),
retries, a URL check on every request and redirect hop, and opt-in caching of 2xx bodies (pass a `cache_if` validator:
exchanges answer blocks with HTTP 200). Agent-chosen pages go through `fetch_public`, which resolves every hop. When
connected, Groww's paid API is asked first for in-session prices, candles and option chains
(`adapters/groww_market.py` via `ReadOnlyHttp`: a GET allow-list plus one margin-calculator POST; it logs in only from
the monitor tick); a failed Groww answer falls back to NSE/BSE and is never a price (funds always use AMFI). Signals
share one contract (`signals/base.Signal`), and IPO/stock signals, the direction model and gate-passed runs log
forecasts to `signals/ledger.py`; the monitor resolves them and calibration is scored on read. New models are
pre-registered (repo-root `evals/experiments/*/PREREG.md`; for the F&O backtest and direction model `specs.py` is the
registration and `prereg.py` pins its hash).

**Portfolio.** Transactions are the only source of truth: importers (`portfolio/importers.py`, pure), read-only broker
connectors (`portfolio/connectors/`) and the inbox all enter through `service.apply`/`add_txns` (`dedupe.py` matches
rows across sources), and `service.rebuild` replays them into FIFO lots per instrument and account; never write lots
directly. `valuation.py` picks prices (Groww LTP in session when connected; else NSE/BSE, with the exchange's official
close after the session; funds by AMFI NAV, ISIN first). `report.snapshot` is what the API shows (unknown cost or
price stays `None`; `complete` says whether it is whole), `tax.py` applies `fincalc/tax.py`'s dated rules per trade
date, and `series.py` is the canonical value history. `disclosures/store.tracked()` is the one set of instruments the
monitor's feed jobs fetch (open holdings plus watched instruments, minus ended ones).

**F&O** (analysis only). `fnohist/` stores NSE's daily F&O bhavcopy and derived IV series. `backtest/` replays the
pre-registered strategies with real costs and a no-look-ahead clock (`Clocked`); `stratlab/` runs user strategies on
the same engine with an out-of-sample split locked at creation. `direction/` is the walk-forward direction model.
`fincalc/tradeplan.verdict` gives ENTER / WAIT / AVOID (`signals/tradeplan.py` builds the card, using the latest saved
backtest as evidence); `fnobook/` covers Groww positions, the paper book and exit-rule alerts.

**Web.** App-router pages in `web/src/app`; primitives in `components/ui.tsx`, charts in `components/charts.tsx`, nav
in `components/shell.tsx` (`NAV`); UI rules in `web/DESIGN.md`. JSON reads and writes use
`api()`/`useApi()`/`useRetryApi()` from `@/lib/api`; raw `fetch`/`EventSource` only for streams and files, with
credentials (and `X-FinResearch: 1` on unsafe methods). Playwright specs in `web/e2e` run against the built app with
every `/api/` call answered by fixtures.

## Rules that are easy to break

- **The model reads and writes; Python computes and verifies.** Report figures come from `fincalc` (Decimal); agents
  cite a fincalc call and its input claims instead of doing arithmetic. Primary documents beat secondary sources;
  every number carries page and line, or URL and time; interim or unverified figures are labelled so.
- **Unknown never becomes 0 or ok.** A missing price, cost, date, weight or feed is `None` with a reason, and
  aggregates say what is incomplete. Never cache a failure as data or as "no data". Nothing is silently truncated:
  tools paginate with continuation hints and engines refuse over-long input (`CapabilityMismatch`).
- **GETs never change records** (portfolio, snapshots, alert inputs, journal, the forecast ledger): those writes sit
  behind POST/PUT/PATCH/DELETE or run in the monitor; GETs may fill market-data caches (exceptions: `docs/USAGE.md`,
  "Which requests write"). `session_scope()` commits on success, so even setting an ORM attribute in a GET is saved.
- **Personal data never reaches a model** (portfolio, statements, AIS, journal, wealth, the F&O book, broker
  connections). Prompt-building and agent-tool packages must not import `finresearch.portfolio`, `wealth`, `fnobook`
  or a personal table, and the IPO advisor sees only `suggest.profile.ADVISOR_FIELDS` (`tests/test_privacy_llm.py`).
- **Secrets** live in the macOS Keychain through `finresearch.secrets`: rows keep a `secret_ref`, logs are redacted as
  they are written, and a new secret field must be visible to `secrets_migrate`'s scan so `finresearch secrets check`
  keeps passing. Never read or print `.env`, credentials, `~/.pgpass` or Keychain items.
- **Claude only through the official `claude` CLI or Agent SDK** (`finresearch.bridge`): no token extraction, no
  proxies that re-expose the subscription.
- **Brokers are read-only.** Never add code that places, modifies or cancels orders; connector attribute names are
  checked against a forbidden list (`portfolio/connectors/base.py`).
- **Never edit a registered spec in place** (`backtest/specs.py`, `direction/specs.py`; tests pin their hash to
  `prereg.py`) or a `PREREG.md`: add a variant, bump `VARIANTS_TRIED`, record it as post hoc in the experiment's
  `ADDENDUM.md` or `RESULTS.md` (never `PREREG.md`), and commit the new hash before any out-of-sample run. Never edit
  a golden tax expectation (`tests/golden_tax`) to match the engine.
- **Times are IST** (`fincalc.dates.today_ist()`/`to_ist()`, never `date.today()` or naive datetimes); financial years
  are named by their end year. Read canonical document text with `ingest.text.read_lines()`, never `str.splitlines()`
  (pages are separated by `\f`, which shifts line numbers).
- **Tests stay offline and isolated.** Only tests using the `env` fixture are redirected to the test database;
  anything else reaches `FINRESEARCH_DATABASE_URL` (the live database by default), hence both URLs above. One pytest
  session per test database. Use the fake `claude` CLI (`tests/fixtures/fake_claude.py`) and respx or
  `httpx.MockTransport`; build `PoliteClient` with `cache_dir=None` or a tmp dir; build secret-like strings at
  runtime; write async tests as plain `async def`; don't add `tests/__init__.py`.
- **Every bug fix gets a regression test that fails without the fix. Never weaken or delete an assertion** to make a
  change pass; change one only when behaviour changed on purpose, and say so in the PR.
- **Migrations**: every Alembic command that touches a database needs `FINRESEARCH_DATABASE_URL` pointed at a scratch
  `*_test` database (bare, it hits the live one). Keep one head: re-parent your `down_revision` when `main` gains a
  migration first.
- **Next.js 16 is not the Next.js you know**: read `web/node_modules/next/dist/docs/` before writing Next code. Don't
  hand-edit `web/AGENTS.md` or `web/CLAUDE.md` (written by `next dev`). A page's new `/api/` call needs a fixture (or
  an optional entry in `expectClean([...])`) in every e2e spec that visits it; date-filtered fixtures use dates
  relative to now.

## Workflow

Issue first (acceptance criteria, a `vX.Y.Z — …` milestone, `area:*`/`type:*` labels; security problems go to a
private advisory, never a public issue) → branch `feat|fix|chore|docs|ci|perf|spike/<name>` → imperative commit
subjects → PR from the template with `Closes #N` → merge commit (`gh pr merge N --merge`, as `git gh` on the owner's
Mac; the remote branch is auto-deleted, and `--delete-branch` would also remove local worktrees). Never push to
`main`. To update a branch, merge `main` into it; never squash or rebase it onto a newer `main` (that once silently
reverted three merged PRs). Append a dated row to `docs/dev/PRODUCT_REQUIREMENTS.md` when a requirement ships and a
`docs/dev/FUNCTIONAL_TESTING.md` entry for each reproduced bug. Releases: a `chore/release-X.Y.Z` PR bumps the version
in `pyproject.toml` (then `uv lock`, or CI's `uv sync --locked` fails) and `web/package.json` and adds a verification
row; tag `vX.Y.Z` on `main` only once that PR shows MERGED, then publish the release notes.

**On the owner's Mac** the live app runs from the main checkout (API :8710 and web :3100 on the `finresearch`
database, serving `web/.next`). Don't run `pnpm build|dev|typecheck|e2e`, `finresearch serve`, the monitor or research
runs from the main tree or against the live database while developing: use a git worktree per change and your own
`*_test` databases. Never use `git stash` (it is shared across worktrees). Run GitHub CLI commands as `git gh ...` (a
repo-local alias for the project's account). A dev stack in a worktree (after `pnpm install --frozen-lockfile` in its
`web/`): create a third scratch database `finresearch_<you>_dev_test` and build it with `alembic upgrade head`; run
`mktemp -d` once and paste that literal path as `<S>` (an empty or different state dir means no cookie and a 401 on
every call). Start each server with `run_in_background`:
`FINRESEARCH_DATABASE_URL=postgresql+psycopg://localhost/finresearch_<you>_dev_test FINRESEARCH_STATE_DIR=<S> uv run
finresearch serve --port 8711 --no-monitor`, and in `web/`: `node scripts/copy-pdfjs-assets.mjs &&
FINRESEARCH_STATE_DIR=<S> NEXT_PUBLIC_FINRESEARCH_API=http://127.0.0.1:8711 pnpm exec next dev -H 127.0.0.1 -p 3001`
(CORS allows the ports in `DASHBOARD_ORIGINS`; a `_test` database keeps secrets out of the Keychain). Stop both when
done. A run started from a dev stack is a detached worker that spends real Claude usage and outlives `serve`. Local
helpers outside git live in the main checkout's `data/devtools/` (a worktree's `data/` has none): `AGENT_RULES.md` for
subagents and `merge_deploy.sh <pr> <worktree> <branch>`, the only way deploys (including the live database's
migrations) reach the live app.
