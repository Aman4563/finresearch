"""NSE's participant-wise open interest in equity derivatives (#317): how many contracts Clients, DIIs, FIIs and Pros
hold long and short in index/stock futures and options at the end of a day. Read-only public file; nothing here places
or routes orders.

URL and format (verified by direct fetch on 13-Oct-2026 for 09-Oct-2026, HTTP 200, text/csv, 982 bytes):
`https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_<DDMMYYYY>.csv`. Line 1 is a title,
`""Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 09, 2026"",,,...`; line 2 the
header `Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,Option Index
Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put
Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts      ,Total Short Contracts` (some names are
padded with spaces); then the rows Client, DII, FII, Pro and TOTAL. Units: contracts (not lots of shares).

Checks: every count must be a whole number; TOTAL must equal the sum of the four participants in every column within
TOLERANCE, and long must equal short in TOTAL for each futures/options bucket (every contract has two sides). NSE's own
file is not always exact: on 09-Oct-2026 the participants' index-call longs sum to 5,404,256 against a TOTAL of
5,404,257 (every other column exact), so a difference within the tolerance is kept with a note, and a larger one is
refused (ParticipantFileError), never stored in part. A 404 is a holiday or a day not yet published (the file appears
in the evening with the bhavcopy), so `fetch_day` returns None for it.

Participant-wise trading volume (#371): `content/nsccl/fao_participant_vol_<DDMMYYYY>.csv` has the same layout and
checks (verified by direct fetch on 22-Oct-2026 for 09-Oct-2026, HTTP 200, 980 bytes, title `""Participant wise Trading
Volume (no. of contracts) in Equity Derivatives as on Oct 09, 2026""`; TOTAL long = short in every bucket: every trade
has a buyer and a seller). Units: contracts traded that day. `Kind` names the file; the title phrase is checked, so a
volume file is never stored as open interest or the other way round.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseError

URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{day:%d%m%Y}.csv"
PARSER_VERSION = "2"  # 2: the title phrase is checked per kind (#371)


@dataclass(frozen=True)
class Kind:
    dataset: str  # the table and the lineage / data-rights name
    url: str
    phrase: str  # in the title line: "Participant wise <phrase> (no. of contracts) ..."

    def url_for(self, day: date) -> str:
        return self.url.format(day=day)


OI = Kind("fno_participant_oi", URL, "Open Interest")
VOL = Kind(
    "fno_participant_vol", URL.replace("fao_participant_oi_", "fao_participant_vol_"), "Trading Volume"
)
KINDS = (OI, VOL)
PARTICIPANTS = ("Client", "DII", "FII", "Pro")
# header name (whitespace-normalised) -> column key
COLUMNS = {
    "Future Index Long": "fut_idx_long", "Future Index Short": "fut_idx_short",
    "Future Stock Long": "fut_stk_long", "Future Stock Short": "fut_stk_short",
    "Option Index Call Long": "opt_idx_call_long", "Option Index Put Long": "opt_idx_put_long",
    "Option Index Call Short": "opt_idx_call_short", "Option Index Put Short": "opt_idx_put_short",
    "Option Stock Call Long": "opt_stk_call_long", "Option Stock Put Long": "opt_stk_put_long",
    "Option Stock Call Short": "opt_stk_call_short", "Option Stock Put Short": "opt_stk_put_short",
    "Total Long Contracts": "total_long", "Total Short Contracts": "total_short",
}  # fmt: skip
KEYS = tuple(COLUMNS.values())
# (long, short) pairs that must balance in the TOTAL row
BALANCED = (("fut_idx_long", "fut_idx_short"), ("fut_stk_long", "fut_stk_short"),
            ("opt_idx_call_long", "opt_idx_call_short"), ("opt_idx_put_long", "opt_idx_put_short"),
            ("opt_stk_call_long", "opt_stk_call_short"), ("opt_stk_put_long", "opt_stk_put_short"),
            ("total_long", "total_short"))  # fmt: skip


TOLERANCE = (10, 1e-4)  # contracts, share of the column total: the larger is allowed


class ParticipantFileError(ValueError):
    """The file is not the participant-wise OI file, or its totals do not add up."""


@dataclass(frozen=True)
class ParticipantDay:
    day: date | None  # from the title line; None when the title has no date
    rows: dict[
        str, dict[str, int]
    ]  # participant -> column key -> contracts (the four participants, not TOTAL)
    notes: tuple[str, ...] = ()  # differences within the tolerance


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.replace('"', "")).strip()


def parse(text: str, kind: Kind = OI) -> ParticipantDay:
    """The four participants' counts from the CSV text; raises ParticipantFileError when a check fails."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ParticipantFileError("too few lines for a participant-wise file")
    title = _norm(lines[0])
    if f"Participant wise {kind.phrase}" not in title:
        raise ParticipantFileError(f"not the participant-wise {kind.phrase} file: {title[:80]!r}")
    # 2022 files print "Jan 03,2022" (no space before the year): without the optional space the date check was skipped
    m = re.search(r"as on ([A-Za-z]{3}) (\d{1,2}), ?(\d{4})", title)
    day = datetime.strptime(" ".join(m.groups()), "%b %d %Y").date() if m else None
    reader = csv.reader(io.StringIO("\n".join(lines[1:])))
    header = [_norm(h) for h in next(reader)]
    if not header or header[0] != "Client Type" or any(c not in header for c in COLUMNS):
        raise ParticipantFileError(f"unexpected header: {header[:4]}…")
    idx = {COLUMNS[h]: i for i, h in enumerate(header) if h in COLUMNS}
    rows: dict[str, dict[str, int]] = {}
    for raw in reader:
        if not raw or not raw[0].strip():
            continue
        who = _norm(raw[0])
        try:
            rows[who] = {k: int(raw[i].strip()) for k, i in idx.items()}
        except (ValueError, IndexError) as e:
            raise ParticipantFileError(f"row {who!r}: a count is not a whole number") from e
    missing = [p for p in (*PARTICIPANTS, "TOTAL") if p not in rows]
    if missing:
        raise ParticipantFileError(f"missing rows: {missing}")
    total = rows.pop("TOTAL")
    notes = []
    for k in KEYS:
        diff = total[k] - sum(rows[p][k] for p in PARTICIPANTS)
        if abs(diff) > max(TOLERANCE[0], TOLERANCE[1] * abs(total[k])):
            raise ParticipantFileError(f"the participants' {k} do not add up to TOTAL (off by {diff:,})")
        if diff:
            notes.append(f"NSE's TOTAL {k} differs from the participants' sum by {diff:+,} contracts")
    for lo, sh in BALANCED:
        if total[lo] != total[sh]:
            raise ParticipantFileError(f"TOTAL {lo} ≠ {sh}: every contract has a long and a short side")
    return ParticipantDay(day, {p: rows[p] for p in PARTICIPANTS}, tuple(notes))


async def fetch_day(http: PoliteClient, day: date, kind: Kind = OI) -> tuple[str, bytes] | None:
    """(url, body as served) of the day's file; None on HTTP 404 (holiday, weekend or not yet published). A refusal,
    a 5xx or a body that is not this CSV raises NseError: never read as "no file". GET only, through the shared NSE
    budget."""
    url = kind.url_for(day)
    resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "text/csv,*/*"})
    if resp.status == 404:
        return None
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {url}")
    if f"Participant wise {kind.phrase}".encode() not in resp.content[:300]:
        raise NseError(f"NSE served something else (HTTP {resp.status}) for {url}: a block page?")
    return url, resp.content
