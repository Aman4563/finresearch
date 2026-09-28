# Security

FinResearch is a private, single-user project that runs on the owner's Mac.

## What must never happen

- A secret (GitHub token, API key, `.env`, Keychain item, anything under `~/Desktop/credential folders`) reaching the repository, a prompt, a transcript, a log or a report. Two checks guard against this: gitleaks runs in pre-commit and CI, and agent sandboxes block these paths.
- The Claude subscription being used outside the official Claude Code CLI / Agent SDK, or being exposed to other people or tools (no token extraction, no proxies).
- A network service binding to anything other than `127.0.0.1`.
- Web content (news pages, broker notes, GMP sites) being treated as instructions. Fetched content is untrusted data. Agents that read it get only the tools they need, and they never get unrestricted shell access.

## Reporting

Open a private GitHub security advisory on this repository, or note it directly to the owner. Don't describe an exploitable issue in a public place.
