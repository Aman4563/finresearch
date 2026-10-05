"""The AIS PDF (best effort, format unverified): decrypt with the user's password, read the summary lines.

The PDF is locked with the PAN in lower case + the date of birth as ddmmyyyy (AIS app help text) [V]. Its table
layout is not published, so this reads only lines shaped like a Part B summary row:

    <sr no> <information code> <description> <source> [(<TAN>)] <count> <amount>

e.g. "1 SFT-015 Dividend income EXAMPLE LIMITED 1 1,500.00". The description is split from the source by a list of
known descriptions; the security of a sale or purchase is not on the summary line, so those rows are compared in
total for the year (portfolio.ais_recon). Part A lines (PAN, name, address ...) never match the row pattern and are
dropped with the rest of the text, which is not kept. The JSON download carries more detail: prefer it.
"""

from __future__ import annotations

import io
import re

from finresearch.portfolio.ais import AisStatement, _fy_from, statement_from_records
from finresearch.portfolio.importers import StatementError

_ROW = re.compile(r"^\s*\d{1,4}\s+((?:SFT|TDS|TCS)-[0-9A-Z]{2,6}|\d{3}[A-Z]{0,3})\s+(.+?)\s+(\d{1,6})\s+"
                  r"(-?[\d,]+(?:\.\d{1,2})?)\s*$")  # fmt: skip
_TAN_PAREN = re.compile(r"\(\s*([A-Z]{4}\d{5}[A-Z])\s*\)")
DESCRIPTIONS = (  # longest first; matched case-insensitively at the start of the text after the code
    "Interest other than 'Interest on securities'", "Income in respect of units of Mutual Fund",
    "Sale of securities and units of mutual fund", "Purchase of securities and units of mutual funds",
    "Off market debit transactions", "Off market credit transactions", "Interest from savings bank",
    "Interest from deposit", "Interest on securities", "Off market transactions", "Interest on income tax refund",
    "Dividend income", "Interest income", "Dividend", "Interest",
)  # fmt: skip


def _split(text: str) -> tuple[str, str]:
    low = text.lower()
    for d in sorted(DESCRIPTIONS, key=len, reverse=True):
        if low.startswith(d.lower()):
            return text[: len(d)], text[len(d) :].strip(" -:")
    return text, ""


def parse_ais_pdf(content: bytes, password: str) -> AisStatement:
    """Raises StatementError with a safe message (never the password or the file's text)."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(password):
            raise StatementError(
                "wrong password for this AIS PDF (PAN in lower case + date of birth ddmmyyyy)"
            )
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except StatementError:
        raise
    except Exception as e:  # never include the exception text: it could quote the file
        raise StatementError(f"could not read this PDF ({type(e).__name__})") from None
    if "annual information statement" not in text.lower():
        raise StatementError("this PDF is not an Annual Information Statement")
    fy = None
    if m := re.search(r"Financial\s+Year\s*:?\s*(20\d\d\s*-\s*\d{2,4})", text, re.I):
        fy = _fy_from(m.group(1), False)
    elif m := re.search(r"Assessment\s+Year\s*:?\s*(20\d\d\s*-\s*\d{2,4})", text, re.I):
        fy = _fy_from(m.group(1), True)
    records = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        code, rest, _count, amount = m.groups()
        tan = _TAN_PAREN.search(rest)
        rest = _TAN_PAREN.sub(" ", rest).strip()
        desc, source = _split(rest)
        records.append({"code": code, "description": desc, "source": source or None,
                        "tan": tan.group(1) if tan else None, "amount": amount})  # fmt: skip
    del text
    if not records:
        raise StatementError("no AIS summary rows were found in this PDF (format unverified): import the AIS JSON "
                             "instead")  # fmt: skip
    st = statement_from_records(records, fy, "pdf")
    st.warnings.append("Read from the PDF summary: sales and purchases are compared in total for the year, not per "
                       "security. The AIS JSON has the detail.")  # fmt: skip
    return st
