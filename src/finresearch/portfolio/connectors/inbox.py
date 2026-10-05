"""The statement inbox: drop CAS PDFs, tradebooks and holdings statements into data/portfolio/inbox and they import.

For people (and brokers) without an API. Every file goes through the same importers and the same idempotent pipeline
as the Portfolio page's Import tab:

* CAMS/KFintech detailed CAS (PDF) → transactions (password: the one saved in Connections → Statement inbox, only if
  the user chose to save it; otherwise the file waits and the page says why).
* NSDL/CDSL e-CAS (PDF, demat) → holdings only: reconciled against the lots, recorded in the sync log, not imported.
* Broker tradebooks (Zerodha/Groww/Upstox CSV/XLSX) → transactions.
* Broker holdings statements (Zerodha/Groww/Upstox holdings XLSX/CSV) → the broker-baseline merge rules
  (connectors.merge): a baseline only for holdings the account has no history for, reconciliation for the rest.
* The income-tax AIS as JSON → stored for the Tax → AIS check (portfolio.ais_store; identifiers stripped; the file
  moves to processed/ like any other but no copy is saved). An AIS PDF (a file named *ais*.pdf) waits: its
  password (PAN + date of birth) is not kept.

A processed file moves to inbox/processed/ (and a copy is kept like any upload); a file that cannot be imported moves
to inbox/failed/ with the reason in the sync log. A PDF waiting for a password stays put and is retried when the
saved password changes. Files still being written (modified in the last few seconds) are left for the next scan.
Nothing is sent anywhere; statements are personal data and are never logged.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import BrokerConnection, BrokerSyncLog
from finresearch.portfolio.ais import AIS_FILENAME, scrub_filename
from finresearch.portfolio.connectors import INBOX_KEY

SETTLE_S = 5.0
EXTS = (".pdf", ".csv", ".xlsx", ".xls", ".json")
MAX_BYTES = 15 * 1024 * 1024


def inbox_dir() -> Path:
    return get_settings().portfolio_dir / "inbox"


def ensure_dirs() -> Path:
    root = inbox_dir()
    for p in (root, root / "processed", root / "failed"):
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)
    return root


def _move(path: Path, sub: str) -> None:
    dest = path.parent / sub / path.name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-{datetime.now(UTC):%Y%m%d%H%M%S}{dest.suffix}")
    shutil.move(str(path), dest)


def pending_files() -> list[dict[str, Any]]:
    root = inbox_dir()
    if not root.is_dir():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(root.iterdir())
            if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith(".")]  # fmt: skip


def scan(*, now: datetime | None = None, password: str | None = None) -> dict[str, Any]:
    """Import every settled file in the inbox. Returns counts and one entry per file (also written to the log)."""
    from finresearch.portfolio.importers import (
        StatementError,
        parse_cas,
        parse_holdings_statement,
        parse_tradebook,
    )
    from finresearch.portfolio.service import apply, preview, save_upload

    now = now or datetime.now(UTC)
    root = ensure_dirs()
    with session_scope() as s:
        row = s.get(BrokerConnection, INBOX_KEY)
        saved_pw = str((row.config or {}).get("password") or "") if row else ""
        waiting = dict((row.state or {}).get("waiting") or {}) if row else {}
        before = dict(waiting)
        pw_fp = hashlib.sha256(saved_pw.encode()).hexdigest()[:12] if saved_pw else ""
    pw = password or saved_pw
    results: list[dict[str, Any]] = []
    imported = 0
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in EXTS:
            continue
        st = path.stat()
        if now.timestamp() - st.st_mtime < SETTLE_S:
            continue
        if st.st_size > MAX_BYTES:
            results.append(_fail(path, "file larger than 15 MB"))
            continue
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        entry: dict[str, Any] = {"file": path.name}
        try:
            with session_scope() as s:
                from sqlalchemy import select

                from finresearch.db.models import PortfolioImport

                prev = s.scalar(select(PortfolioImport.id).where(PortfolioImport.sha256 == sha))
                if prev is not None:
                    _move(path, "processed")
                    results.append(
                        {**entry, "status": "duplicate", "note": f"already imported (import #{prev})"}
                    )
                    continue
                if path.suffix.lower() == ".json" or (
                    content.startswith(b"%PDF") and AIS_FILENAME.search(path.name)
                ):
                    safe = scrub_filename(path.name)  # an AIS download may carry the PAN in its name
                    try:
                        results.append({"file": safe, **_ais(s, path, content, sha, before.get(safe))})
                    except StatementError as e:
                        _move(path, "failed")
                        results.append({"file": safe, "status": "failed", "note": str(e)})
                    if results[-1]["status"] == "waiting":
                        waiting[safe] = "ais pdf"
                    continue
                if content.startswith(b"%PDF"):
                    if not pw:
                        waiting[path.name] = "no password saved"
                        results.append({**entry, "status": "waiting", "logged": before.get(path.name) == "no password saved", "note": "no CAS password saved: save it in "
                                        "Connections → Statement inbox, or import the file on the Portfolio page"})  # fmt: skip
                        continue
                    if waiting.get(path.name) == f"wrong:{pw_fp}" and not password:
                        continue  # the same password already failed: wait for a new one
                    try:
                        res = parse_cas(content, pw)
                    except StatementError as e:
                        if "password" in str(e):
                            waiting[path.name] = f"wrong:{pw_fp}"
                            results.append(
                                {
                                    **entry,
                                    "status": "waiting",
                                    "note": str(e),
                                    "logged": before.get(path.name) == f"wrong:{pw_fp}",
                                }
                            )
                            continue
                        raise
                    if res.holdings_only:  # NSDL/CDSL demat CAS: reconcile only
                        out = preview(s, res)
                        _move(path, "processed")
                        results.append({**entry, "status": "reconciled", "kind": f"{res.source} demat CAS",
                                        "reconciliation": out["reconciliation"], "reconciled": out["reconciled"],
                                        "note": "holdings only (no transactions): compared with your lots"})  # fmt: skip
                        waiting.pop(path.name, None)
                        continue
                    out = apply(
                        s, res, filename=path.name, sha256=sha, saved_path=save_upload(content, sha, ".pdf")
                    )
                    kind = f"{res.source} CAS"
                else:
                    hs = None
                    try:
                        res = parse_tradebook(content, path.name)
                    except StatementError:
                        hs = parse_holdings_statement(content, path.name)  # raises StatementError if neither
                    if hs is not None:
                        from finresearch.portfolio.connectors.merge import (
                            merge_sync,
                            remember_statement_prices,
                            statement_day,
                        )

                        broker, holdings = hs
                        acc = {"zerodha": "Zerodha", "groww": "Groww", "upstox": "Upstox"}[broker]
                        as_of = statement_day(path.name, datetime.fromtimestamp(st.st_mtime, UTC))
                        mr = merge_sync(s, account=acc, source=f"{broker}_holdings", label=f"{acc} holdings file",
                                        holdings=holdings, trades=[], today=as_of, now=now)  # fmt: skip
                        remember_statement_prices(s, account=acc, holdings=holdings, day=as_of, label=acc)
                        _record_sha(s, mr.import_ids, sha, path.name, f"{broker}_holdings")
                        _move(path, "processed")
                        imported += 1
                        results.append({**entry, "status": "imported", "kind": f"{acc} holdings statement",
                                        **mr.as_dict()})  # fmt: skip
                        waiting.pop(path.name, None)
                        continue
                    ext = path.suffix.lower() if path.suffix.lower() in (".csv", ".xlsx") else ".csv"
                    out = apply(
                        s, res, filename=path.name, sha256=sha, saved_path=save_upload(content, sha, ext)
                    )
                    kind = f"{res.source} tradebook"
                _move(path, "processed")
                imported += 1
                waiting.pop(path.name, None)
                results.append({**entry, "status": "imported", "kind": kind, **out})
        except StatementError as e:
            results.append(_fail(path, str(e)))
            waiting.pop(path.name, None)
        except Exception as e:  # never include exception text: it could quote file contents
            results.append(_fail(path, f"could not import ({type(e).__name__})"))
            waiting.pop(path.name, None)
    with session_scope() as s:
        row = s.get(BrokerConnection, INBOX_KEY)
        if row is not None:
            row.state = {**(row.state or {}), "waiting": waiting, "last_scan": now.isoformat()}
            if results:
                row.last_sync_at = now
        for r in results:
            if r["status"] == "waiting" and r.get("logged"):
                continue
            status = {"imported": "ok", "reconciled": "ok", "duplicate": "ok", "waiting": "partial"}.get(r["status"],
                                                                                                       "error")  # fmt: skip
            s.add(BrokerSyncLog(key=INBOX_KEY, trigger="inbox", status=status, started_at=now,
                                finished_at=datetime.now(UTC), summary=_slim(r),
                                error=r.get("note") if status != "ok" else None))  # fmt: skip
    return {"imported": imported, "files": [_slim(r) for r in results], "inbox": str(root)}


def _ais(s, path: Path, content: bytes, sha: str, before: str | None) -> dict[str, Any]:
    """An AIS: JSON imports (identifiers stripped, the file is not kept); a PDF needs its own password (PAN + date
    of birth), which the inbox does not keep, so it waits for the Portfolio page's import."""
    from finresearch.portfolio.ais import looks_like_ais_json, parse_ais_json
    from finresearch.portfolio.ais_store import known_sha, save
    from finresearch.portfolio.importers import StatementError

    if content.startswith(b"%PDF"):
        return {"status": "waiting", "logged": before == "ais pdf",
                "note": "an AIS PDF needs its password (PAN + date of birth): import it on Portfolio → Import, or drop "
                        "the AIS JSON here instead"}  # fmt: skip
    if (fy := known_sha(s, sha)) is not None:
        _move(path, "processed")
        return {"status": "duplicate", "note": f"this AIS is already imported (FY {fy - 1}-{fy % 100:02d})"}
    if not looks_like_ais_json(content):
        raise StatementError("not an AIS JSON (the inbox reads JSON files only as an AIS)")
    st = parse_ais_json(content)
    if st.fy is None:
        raise StatementError(
            "the AIS year could not be read: import it on Portfolio → Import and choose the year"
        )
    save(s, st, st.fy, sha)
    _move(path, "processed")
    return {"status": "imported", "kind": f"AIS FY {st.fy - 1}-{st.fy % 100:02d}", "rows": len(st.items)}


def _record_sha(s, import_ids: list[int], sha: str, name: str, source: str = "inbox") -> None:
    """A holdings file that produced a baseline import is refused the second time by its sha (like any upload)."""
    from finresearch.db.models import PortfolioImport

    if import_ids:
        imp = s.get(PortfolioImport, import_ids[0])
        imp.sha256, imp.filename = sha, name[:300]
    else:  # nothing to import: remember the file anyway so an unchanged copy is recognised
        s.add(
            PortfolioImport(
                kind="holdings", source=source[:20], filename=name[:300], sha256=sha, summary={"rows": 0}
            )
        )
    s.flush()


def _fail(path: Path, why: str) -> dict[str, Any]:
    _move(path, "failed")
    return {"file": path.name, "status": "failed", "note": why}


def _slim(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("file", "status", "kind", "note", "added", "duplicates", "reconciled", "differences", "import_id",
            "import_ids", "baselines", "reconciliation", "rows")  # fmt: skip
    out = {k: r[k] for k in keep if k in r}
    if isinstance(out.get("reconciliation"), list):
        out["reconciliation"] = out["reconciliation"][:100]
    return out
