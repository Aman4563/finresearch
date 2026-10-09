"""Document acquisition + text extraction with page/line provenance.

Pipeline for one PDF:
  1. store:   copy/download into data/docs/raw/<sha[:2]>/<sha>.pdf (immutable, deduplicated by sha256)
  2. extract: `pdftotext -layout` for the whole file (pages separated by \\f)
  3. OCR:     pages with (almost) no text layer are scanned -> Tesseract (fast, confidence-scored);
              low-confidence pages get a glm-ocr second opinion (loop-safe) and the better result wins
  4. combine: write data/docs/derived/<sha>/text.txt = text layer with OCR text substituted for scanned pages.
              Line numbers of this file are the citation backbone (== `grep -n` / `sed -n`).
  5. record:  Document + DocumentPage rows (page -> line_start/line_end, source, OCR confidence, warnings)
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import shutil
import socket
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Company, Document, DocumentPage

PIPELINE_VERSION = "ingest-1"
SCANNED_CHAR_THRESHOLD = 40  # a page with fewer non-space chars has no usable text layer
TESSERACT_MIN_CONF = 70.0  # below this mean word confidence we ask glm-ocr for a second opinion
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
MAX_REDIRECTS = 5
RESERVED_TLDS = ("example", "invalid", "test")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class DocKind(StrEnum):
    RHP = "RHP"
    DRHP = "DRHP"
    ADDENDUM = "ADDENDUM"
    ABRIDGED = "ABRIDGED_PROSPECTUS"
    PRICE_BAND_AD = "PRICE_BAND_AD"
    ANCHOR = "ANCHOR_ALLOCATION"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    FINANCIALS = "FINANCIAL_STATEMENTS"
    INDUSTRY_REPORT = "INDUSTRY_REPORT"
    OTHER = "OTHER"


@dataclass
class PageText:
    page_no: int
    text: str
    source: str = "pdftotext"
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- storage
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def store_file(src: Path, docs_dir: Path) -> tuple[str, Path]:
    sha = sha256_file(src)
    dest = docs_dir / "raw" / sha[:2] / f"{sha}{src.suffix.lower() or '.pdf'}"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
    return sha, dest


class UnsafeUrl(ValueError):
    pass


def check_public_url(url: str, *, resolve=socket.getaddrinfo) -> None:
    """URLs come from agents and web pages: only http(s) to public addresses (no loopback, private network,
    link-local/cloud metadata). A name that does not resolve is left to fail in the fetch itself."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrl(f"only http(s) URLs can be downloaded: {url!r}")
    host = parts.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrl(f"{host} is a local host name")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host.rsplit(".", 1)[-1] in RESERVED_TLDS:
            return  # never resolves (RFC 2606/6761); skip a slow negative DNS lookup
        try:
            addrs = [ipaddress.ip_address(ai[4][0].split("%")[0]) for ai in resolve(host, parts.port or 443)]
        except (OSError, UnicodeError) as e:
            # fail closed: a name that does not resolve now could resolve to a private address in the fetch itself
            raise UnsafeUrl(
                f"could not resolve {host} to check that it is public ({type(e).__name__})"
            ) from e
    from finresearch.adapters.http import is_public_ip  # one address policy for every agent-chosen URL (#259)

    for a in addrs:
        if not is_public_ip(a):
            raise UnsafeUrl(f"{host} resolves to a non-public address ({a})")


def download(
    url: str,
    dest_dir: Path,
    *,
    referer: str | None = None,
    timeout: float = 300,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> tuple[Path, dict]:
    """Download to a temp file; returns (path, provenance). Validates that PDFs really are PDFs.

    Every hop of a redirect chain is checked with check_public_url, and the body is capped at max_bytes."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA, **({"Referer": referer} if referer else {})}
    with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers) as c:
        target = url
        for _ in range(MAX_REDIRECTS + 1):
            check_public_url(target)
            r = c.send(c.build_request("GET", target), stream=True)
            if not r.is_redirect:
                break
            r.close()
            target = urljoin(target, r.headers["location"])
        else:
            raise ValueError(f"{url}: more than {MAX_REDIRECTS} redirects")
        try:
            r.raise_for_status()
            if int(r.headers.get("content-length") or 0) > max_bytes:
                raise ValueError(f"{url}: file larger than {max_bytes // (1024 * 1024)} MB")
            fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".download")
            size = 0
            try:
                with open(fd, "wb") as f:
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"{url}: file larger than {max_bytes // (1024 * 1024)} MB")
                        f.write(chunk)
            except (
                BaseException
            ):  # too large, or the connection dropped mid-body: no partial file left behind
                Path(tmp).unlink(missing_ok=True)
                raise
        finally:
            r.close()
        prov = {
            "url": str(r.url),
            "requested_url": url,
            "status": r.status_code,
            "content_type": r.headers.get("content-type"),
            "fetched_at": datetime.now(UTC).isoformat(),
        }
    path = Path(tmp)
    if url.lower().endswith(".pdf") and path.read_bytes()[:5] != b"%PDF-":
        path.unlink(missing_ok=True)
        raise ValueError(f"{url} did not return a PDF (content-type {prov['content_type']})")
    return path, prov


# --------------------------------------------------------------------------- extraction
# Downloaded PDFs are untrusted: a malformed or hostile file can make poppler/tesseract spin forever. Each tool run is
# bounded (subprocess.TimeoutExpired kills it); the limits are far above a normal 600-page RHP (seconds).
PDF_INFO_TIMEOUT_S = 60
PDF_TEXT_TIMEOUT_S = 900
PAGE_TOOL_TIMEOUT_S = 300  # one page rendered (pdftoppm) or OCR'd (tesseract)


def pdf_page_count(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True,
                         timeout=PDF_INFO_TIMEOUT_S).stdout  # fmt: skip
    return int(next(line.split()[-1] for line in out.splitlines() if line.startswith("Pages:")))


def extract_layout_pages(pdf: Path) -> list[str]:
    """Text layer per page via pdftotext -layout (pages are \\f-separated)."""
    out = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        check=True,
        timeout=PDF_TEXT_TIMEOUT_S,
    ).stdout.decode("utf-8", errors="replace")
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]  # pdftotext ends with a trailing \f
    n = pdf_page_count(pdf)
    if len(pages) < n:
        pages += [""] * (n - len(pages))
    return pages[:n]


def is_scanned(text: str) -> bool:
    return sum(1 for ch in text if not ch.isspace()) < SCANNED_CHAR_THRESHOLD


def render_page(pdf: Path, page_no: int, out_dir: Path, dpi: int = 200) -> Path:
    prefix = out_dir / f"p{page_no:04d}"
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            str(dpi),
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            "-png",
            "-singlefile",
            str(pdf),
            str(prefix),
        ],
        check=True,
        capture_output=True,
        timeout=PAGE_TOOL_TIMEOUT_S,
    )
    return prefix.with_suffix(".png")


def tesseract_page(image: Path, lang: str = "eng") -> tuple[str, float]:
    """Returns (text, mean word confidence 0-100). Uses one tesseract run producing txt + tsv."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "out"
        subprocess.run(
            ["tesseract", str(image), str(base), "-l", lang, "--psm", "3", "txt", "tsv"],
            check=True,
            capture_output=True,
            timeout=PAGE_TOOL_TIMEOUT_S,
        )
        text = base.with_suffix(".txt").read_text(errors="replace")
        confs = []
        for row in base.with_suffix(".tsv").read_text(errors="replace").splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) >= 12 and cols[11].strip():
                try:
                    c = float(cols[10])
                except ValueError:
                    continue
                if c >= 0:
                    confs.append(c)
        return text, (sum(confs) / len(confs) if confs else 0.0)


def ocr_pages(
    pdf: Path,
    page_numbers: list[int],
    *,
    glm_ocr=None,
    workers: int = 4,
    dpi: int = 200,
) -> dict[int, PageText]:
    """OCR scanned pages. Tesseract in parallel threads (CPU), then glm-ocr for low-confidence pages."""
    results: dict[int, PageText] = {}
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)

        def one(p: int) -> PageText:
            img = render_page(pdf, p, out_dir, dpi)
            text, conf = tesseract_page(img)
            return PageText(p, text, "tesseract", round(conf, 1))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for pt in ex.map(one, page_numbers):
                results[pt.page_no] = pt

        if glm_ocr is not None:
            weak = [p for p, pt in results.items() if (pt.confidence or 0) < TESSERACT_MIN_CONF]
            for p in weak:
                img = out_dir / f"p{p:04d}.png"
                if not img.exists():
                    img = render_page(pdf, p, out_dir, dpi)
                try:
                    r = asyncio.run(glm_ocr.ocr_image(img))
                except Exception as e:
                    results[p].warnings.append(f"glm-ocr failed: {e}")
                    continue
                tess = results[p]
                # prefer glm-ocr when it produced materially more text (tesseract low-conf often drops text)
                if len(r.text) > 0.8 * len(tess.text):
                    results[p] = PageText(
                        p, r.text, "glm-ocr", None, [*r.warnings, f"tesseract conf {tess.confidence}"]
                    )
                else:
                    tess.warnings.append("glm-ocr second opinion shorter; kept tesseract")
    return results


def combine_pages(pages: list[PageText]) -> tuple[str, list[tuple[int, int]]]:
    """Join pages with \\f (like pdftotext) and compute 1-based (line_start, line_end) per page."""
    parts, spans, line = [], [], 1
    for i, pt in enumerate(pages):
        body = pt.text.replace("\f", "")
        if i > 0:
            body = "\f" + body
        n_lines = body.count("\n") + 1
        if body.endswith("\n"):
            n_lines -= 1
        spans.append((line, line + max(n_lines, 1) - 1))
        parts.append(body if body.endswith("\n") or i == len(pages) - 1 else body + "\n")
        line += max(n_lines, 1)
    return "".join(parts), spans


# --------------------------------------------------------------------------- orchestration
def get_or_create_company(session: Session, slug: str, name: str | None = None, **fields) -> Company:
    c = session.scalar(select(Company).where(Company.slug == slug))
    if c is None:
        c = Company(slug=slug, name=name or slug, **fields)
        session.add(c)
        session.flush()
    return c


def ingest_pdf(
    session: Session,
    pdf: Path,
    *,
    company: Company | None,
    kind: DocKind,
    title: str | None = None,
    docs_dir: Path,
    provenance: dict | None = None,
    glm_ocr=None,
    ocr: bool = True,
    ocr_workers: int = 4,
    progress=None,
) -> Document:
    """Idempotent: re-ingesting the same bytes returns the existing Document."""
    sha, stored = store_file(Path(pdf), docs_dir)
    existing = session.scalar(select(Document).where(Document.sha256 == sha))
    if existing is not None:
        return existing

    raw_pages = extract_layout_pages(stored)
    pages = [PageText(i + 1, t) for i, t in enumerate(raw_pages)]
    scanned = [pt.page_no for pt in pages if is_scanned(pt.text)]
    if progress:
        progress(f"{len(pages)} pages, {len(scanned)} without a text layer")
    if scanned and ocr:
        for p, pt in ocr_pages(stored, scanned, glm_ocr=glm_ocr, workers=ocr_workers).items():
            pages[p - 1] = pt

    text, spans = combine_pages(pages)
    derived = docs_dir / "derived" / sha
    derived.mkdir(parents=True, exist_ok=True)
    text_path = derived / "text.txt"
    tmp = text_path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(text_path)

    doc = Document(
        company_id=company.id if company else None,
        kind=kind.value,
        title=title or Path(pdf).stem,
        sha256=sha,
        source_url=(provenance or {}).get("url"),
        local_path=str(stored),
        text_path=str(text_path),
        bytes=stored.stat().st_size,
        pages=len(pages),
        scanned_pages=len(scanned),
        fetched_at=datetime.fromisoformat(provenance["fetched_at"])
        if provenance and provenance.get("fetched_at")
        else None,
        provenance={
            **(provenance or {}),
            "pipeline": PIPELINE_VERSION,
            "original_name": Path(pdf).name,
            "ocr_ran": bool(scanned and ocr),
        },
    )
    session.add(doc)
    session.flush()
    for pt, (ls, le) in zip(pages, spans, strict=True):
        session.add(
            DocumentPage(
                document_id=doc.id,
                page_no=pt.page_no,
                line_start=ls,
                line_end=le,
                text=pt.text.replace("\x00", ""),
                char_count=len(pt.text),
                text_source=pt.source if pt.page_no in scanned else "pdftotext",
                ocr_confidence=pt.confidence,
                warnings=pt.warnings,
            )
        )
    session.flush()
    return doc
