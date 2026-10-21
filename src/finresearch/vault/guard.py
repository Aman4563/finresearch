"""The vault-read guard (#364): the only way to open the 2008-2021 F&O vault, and it fails closed.

`vault_read(study_id, study_hash, purpose=...)` refuses (VaultSealed) unless the study is registered in the pack, the
presented hash and the hash recomputed from the committed files both equal the recorded one, the pack hash matches,
every prerequisite study has a logged read, no read was ever made under another pack hash, and (engine-pinned
studies) the backtest engine hashes to the committed pin. It then writes and commits the read-log row (`vault_read`)
BEFORE yielding a Grant: the read is spent even if the study crashes, and a second read of the same study hash is
refused (the data audit alone is repeatable, numbered). Any database error while checking or logging refuses.
`backtest.dbhistory.DbHistory` accepts vault rows only with a live Grant for its own URL.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from finresearch.vault import engine_pin, pack
from finresearch.vault.registry import STUDIES, Study, VaultSealed, engine_hash, pack_hash, study_hash

__all__ = ["Grant", "VaultSealed", "check_grant", "reads", "reads_count", "vault_read", "verify"]


@dataclass
class Grant:
    """A live permission to read the vault at `url` for one logged read. Closed when vault_read's block exits."""

    study_id: str
    study_hash: str
    read_no: int
    url: str
    read_id: int
    open: bool = True


def _root(root: Path | None) -> Path:
    from finresearch.config import REPO_ROOT

    return root or REPO_ROOT


def verify(study_id: str, presented_hash: str, root: Path | None = None) -> Study:
    """The registered study, or VaultSealed with the reason."""
    st = STUDIES.get(study_id)
    if st is None:
        raise VaultSealed(f"{study_id!r} is not a registered vault study (evals/experiments/vault/PREREG.md)")
    recorded = pack.STUDY_HASHES.get(study_id)
    if recorded is None or presented_hash != recorded:
        raise VaultSealed(f"{study_id}: the presented hash is not the one committed in the pack")
    r = _root(root)
    actual = study_hash(st, r)
    if actual != recorded:
        raise VaultSealed(f"{study_id}: its registration changed or is missing since the pack was committed "
                          f"({'missing file' if actual is None else actual[:12] + '…'} vs {recorded[:12]}…)")  # fmt: skip
    if pack_hash(r) != pack.PACK_HASH:
        raise VaultSealed(
            "the vault pack changed since it was committed: no study may read the vault under it"
        )
    if st.library:
        from finresearch.backtest import prereg, specs

        if specs.registration_hash() != prereg.HASH:
            raise VaultSealed(f"{study_id}: backtest/specs.py does not match backtest/prereg.HASH")
    return st


def _git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, capture_output=True,
                             text=True, timeout=5, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


@contextmanager
def vault_read(study_id: str, study_hash_: str, *, purpose: str, vault_url: str | None = None,
               log_url: str | None = None, root: Path | None = None) -> Iterator[Grant]:  # fmt: skip
    """Open one logged read of the vault for a registered study (see the module docstring). `log_url` is the
    database holding the read log (default: the app's)."""
    from finresearch.config import get_settings
    from finresearch.db import session_scope
    from finresearch.db.models import VaultRead

    st = verify(study_id, study_hash_, root)
    url = vault_url or get_settings().vault_database_url
    if not url:
        raise VaultSealed("no vault database is configured (FINRESEARCH_VAULT_DATABASE_URL)")
    eh = engine_hash()
    if st.engine_pin and (engine_pin.ENGINE_HASH is None or eh != engine_pin.ENGINE_HASH):
        raise VaultSealed(f"{study_id} needs the pinned engine: "
                          + ("no pin is committed yet (vault/engine_pin.py)" if engine_pin.ENGINE_HASH is None
                             else f"the engine hashes to {eh[:12]}…, the pin is {engine_pin.ENGINE_HASH[:12]}…"))  # fmt: skip
    try:
        with session_scope(log_url) as s:
            s.execute(select(func.pg_advisory_xact_lock(364)))  # one guard decision at a time
            rows = list(s.scalars(select(VaultRead)))
            if any(r.pack_hash != pack.PACK_HASH for r in rows):
                raise VaultSealed(
                    "the vault was read under another pack: a later registration may use forward data only"
                )
            done = {r.study_id for r in rows}
            missing = [x for x in st.requires if x not in done]
            if missing:
                raise VaultSealed(f"{study_id} may read the vault only after {', '.join(missing)}")
            mine = [r for r in rows if r.study_id == study_id and r.study_hash == study_hash_]
            if mine and not st.repeatable:
                raise VaultSealed(
                    f"{study_id} already read the vault on {mine[0].read_at:%d-%b-%Y} (one read per spec hash)"
                )
            row = VaultRead(study_id=study_id, study_hash=study_hash_, read_no=len(mine) + 1, pack_hash=pack.PACK_HASH,
                            engine_hash=eh, git_sha=_git_sha(), counts_in_n=st.counts_in_n, purpose=purpose[:300])  # fmt: skip
            s.add(row)
            s.flush()
            grant = Grant(study_id, study_hash_, row.read_no, url, row.id)
    except SQLAlchemyError as e:
        raise VaultSealed(f"the vault read log could not be checked or written: {type(e).__name__}") from e
    try:
        yield grant
    finally:
        grant.open = False


def check_grant(grant: Any, url: str | None) -> bool:
    """True only for a live Grant issued for this database URL."""
    return isinstance(grant, Grant) and grant.open and grant.url == url


def reads(log_url: str | None = None) -> list[dict[str, Any]]:
    from finresearch.db import session_scope
    from finresearch.db.models import VaultRead

    with session_scope(log_url) as s:
        return [{"study_id": r.study_id, "study_hash": r.study_hash, "read_no": r.read_no, "engine_hash": r.engine_hash,
                 "git_sha": r.git_sha, "counts_in_n": r.counts_in_n, "purpose": r.purpose, "read_at": r.read_at}
                for r in s.scalars(select(VaultRead).order_by(VaultRead.id))]  # fmt: skip


def reads_count(log_url: str | None = None) -> int:
    """Vault reads that count in the trial count N (every read except the data audit's)."""
    return sum(1 for r in reads(log_url) if r["counts_in_n"])
