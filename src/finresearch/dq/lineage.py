"""Lineage per dataset load (#368): one dataset_load row per file loaded, with the body's sha256, the server's
Last-Modified when known, the parser version and the rows stored. A load of a day whose last load had another hash
records that hash in `prior_sha256`: the exchange re-uploaded the file (4-Jan-2024's F&O bhavcopy was re-uploaded the
next afternoon). The nightly checks (dq.checks) turn that into a `reupload` row; the manifests' resume rules still
skip loaded days, so a re-upload is seen when a day is re-loaded (a newly tracked stock, a manual re-run)."""

from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import DatasetLoad


def record_load(s: Session, dataset: str, day: date, body: bytes, *, url: str | None, parser_version: str,
                rows: int | None, last_modified: str | None = None) -> DatasetLoad:  # fmt: skip
    sha = hashlib.sha256(body).hexdigest()
    last = s.scalars(select(DatasetLoad.sha256).where(DatasetLoad.dataset == dataset, DatasetLoad.day == day)
                     .order_by(DatasetLoad.id.desc()).limit(1)).first()  # fmt: skip
    row = DatasetLoad(dataset=dataset, day=day, url=None if url is None else url[:200], sha256=sha,
                      last_modified=last_modified, parser_version=parser_version, rows=rows,
                      prior_sha256=last if last is not None and last != sha else None)  # fmt: skip
    s.add(row)
    s.flush()
    return row


def reuploads(s: Session, dataset: str, day: date) -> int:
    """Loads of `day` whose body differed from the load before it."""
    return s.scalar(select(func.count()).select_from(DatasetLoad).where(
        DatasetLoad.dataset == dataset, DatasetLoad.day == day, DatasetLoad.prior_sha256.is_not(None))) or 0  # fmt: skip
