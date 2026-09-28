# Security policy

FinResearch is a single-user research engine that runs on the owner's own machine. It has no hosted service.

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub security advisories](https://github.com/Aman4563/finresearch/security/advisories/new). Please don't open a public issue. You will get an acknowledgement, and fixes are released as a new tagged version.

Only the latest release on `main` is supported.

## Security model

- **Secrets never enter the repository, prompts, transcripts, logs or reports.**
  - Credentials live in a gitignored `.env` or the OS keychain.
  - gitleaks runs in pre-commit and CI, and GitHub secret scanning with push protection is enabled.
  - Agent sandboxes block credential locations.
- **Claude is used only through the official Claude Code CLI or Agent SDK**, under the operator's own login and for their own use. The project never extracts OAuth tokens and never re-exposes a subscription as an API.
- **Local services bind to `127.0.0.1` only.** That covers the database, the local model server and the MCP server, which uses stdio.
- **Fetched web content is untrusted data, never instructions.** Agents that read news, broker notes or grey-market sites get only the tools their role needs, never unrestricted shell access. Every figure in a report must trace to a cited primary source that is checked deterministically.
- **Downloaded documents and research data stay out of the repository.** They are kept under the gitignored `data/` directory.
