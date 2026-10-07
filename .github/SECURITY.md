# Security policy

FinResearch is a single-user research engine that runs on the owner's own machine. It has no hosted service.

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub security advisories](https://github.com/Aman4563/finresearch/security/advisories/new). Please don't open a public issue. You will get an acknowledgement, and fixes are released as a new tagged version.

Only the latest release on `main` is supported.

## Security model

- **Secrets never enter the repository, prompts, transcripts, logs or reports.**
  - App secrets live in the macOS Keychain: broker API keys and secrets, TOTP seeds, PINs, access tokens, the
    optional saved CAS password, ntfy and Telegram tokens, and the local API token. The database keeps only a
    reference to each (`finresearch.secrets`), so backups hold no secrets; `scripts/backup.sh` refuses to run if
    `finresearch secrets check` finds one stored as plain text.
  - The Postgres password is in `~/.pgpass` (mode 0600); local Postgres uses scram-sha-256, not trust. Other
    settings live in a gitignored `.env`.
  - gitleaks runs in pre-commit and CI, and GitHub secret scanning with push protection is enabled.
  - Agent sandboxes block credential locations.
- **Claude is used only through the official Claude Code CLI or Agent SDK**, under the operator's own login and for their own use. The project never extracts OAuth tokens and never re-exposes a subscription as an API.
- **Local services bind to `127.0.0.1` only.** That covers the API, the web app, the database, the local model server and the MCP server, which uses stdio.
- **The local API needs a per-install token** on every route except `/api/health`: a bearer header for the CLI and scripts, an httpOnly SameSite=Strict cookie that the web app's server sets for 127.0.0.1. `finresearch serve` creates it on first start (Keychain, plus `data/state/api_token` with mode 0600 for the web server). Same-user malware can still read that file; the token keeps out local programs that can only make HTTP requests.
- **Agents cannot fetch this machine or the local network.** WebFetch deny rules and a hook that resolves each URL block loopback, private, link-local and reserved addresses in every agent sandbox. The MCP tools fetch only official hosts and refuse private addresses, including after a redirect.
- **Fetched web content is untrusted data, never instructions.** Agents that read news, broker notes or grey-market sites get only the tools their role needs, never unrestricted shell access. Every figure in a report must trace to a cited primary source that is checked deterministically.
- **Personal financial data stays on the machine.** Portfolio, statements, AIS, journal, wealth and household data stay in the local database and `data/`, are never sent to a model, and statement passwords are used once and not stored. The one exception is the statement inbox's CAS password: it is saved, in the Keychain, only if you type it into the inbox settings. Broker connections are read-only and never place orders. The one personal input a model does see is the IPO profile the advisor uses (capital per IPO, risk appetite, horizon, tax slab, category, max position, typed holdings and notes, rules); the profile setting "Keep the personal IPO suggestion local" switches even that off.
- **Downloaded documents and research data stay out of the repository.** They are kept under the gitignored `data/` directory.
