"""HTML -> PDF with headless Chrome, written atomically and validated.

Regression guard for the manual Moneyview report: re-rendering over an existing PDF produced a file with two
concatenated documents that viewers refused to open, and Chrome sometimes printed but never exited. Here:
* Chrome prints to a unique temp file in its own profile directory and its process group is killed when done;
* the result is validated with pypdf in strict mode and re-written cleanly (single document, clean xref);
* only then is it atomically renamed over the destination.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from pypdf import PdfReader, PdfWriter

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]


class PdfRenderError(RuntimeError):
    pass


def find_chrome() -> str | None:
    env = os.environ.get("CHROME_PATH")
    for c in ([env] if env else []) + CHROME_CANDIDATES:
        if c and (Path(c).exists() or shutil.which(c)):
            return c if Path(c).exists() else shutil.which(c)
    return None


def _complete(p: Path) -> bool:
    try:
        return p.stat().st_size > 1000 and p.read_bytes()[-64:].rstrip().endswith(b"%%EOF")
    except OSError:
        return False


def html_to_pdf(html: Path, pdf: Path, *, title: str | None = None, timeout_s: float = 120) -> int:
    """Render `html` to `pdf`. Returns the page count. Raises PdfRenderError on any failure."""
    chrome = find_chrome()
    if chrome is None:
        raise PdfRenderError("Chrome/Chromium not found (set CHROME_PATH)")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fr-pdf-") as td:
        raw = Path(td) / "raw.pdf"
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
               f"--user-data-dir={Path(td) / 'profile'}", f"--print-to-pdf={raw}", html.resolve().as_uri()]  # fmt: skip
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if _complete(raw):
                    time.sleep(0.5)  # let Chrome finish flushing
                    break
                if proc.poll() is not None and not raw.exists():
                    raise PdfRenderError(f"Chrome exited ({proc.returncode}) without writing a PDF")
                time.sleep(0.25)
            else:
                raise PdfRenderError(f"Chrome did not produce a PDF within {timeout_s:.0f}s")
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    os.killpg(proc.pid, signal.SIGKILL)
        try:
            reader = PdfReader(raw, strict=True)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            if title:
                writer.add_metadata({"/Title": title})
            clean = Path(td) / "clean.pdf"
            with open(clean, "wb") as f:
                writer.write(f)
            pages = len(PdfReader(clean, strict=True).pages)
        except Exception as e:
            raise PdfRenderError(f"rendered PDF failed validation: {e}") from e
        if pages == 0:
            raise PdfRenderError("rendered PDF has no pages")
        tmp_dest = pdf.with_suffix(".pdf.tmp")
        shutil.copyfile(clean, tmp_dest)
        tmp_dest.replace(pdf)
    return pages
