"""Report markdown -> self-contained HTML with linked citations and an evidence appendix."""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from markdown_it import MarkdownIt

_CITE = re.compile(r"\[C(\d+)\]")

# Required on every exported report (docs/dev/RESEARCH_ROADMAP.md §D.8). Under the SEBI RA master circular
# (6-Feb-2026) a purely personal tool is outside RA/IA registration, but publishing its calls would not be, so every
# export says what it is and carries a visible watermark. Keep the wording in sync with the web footer.
EXPORT_DISCLAIMER = (
    "Personal research generated with AI assistance; not investment advice; the author is not a SEBI-registered "
    "Research Analyst (RA) or Investment Adviser (IA); do not distribute."
)
EXPORT_WATERMARK = "PERSONAL – NOT FOR DISTRIBUTION"

STATUS_LABEL = {
    "verified": "verified",
    "unverified": "unverified",
    "needs_review": "needs review",
    "contradicted": "contradicted",
    "unsupported": "unsupported",
}

CSS = """
:root{--fg:#1d1d1f;--muted:#5b5b66;--bg:#fff;--line:#e3e3e8;--head:#f4f5f7;--accent:#1f5fbf;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e}
@media (prefers-color-scheme: dark){:root{--fg:#e6e6ea;--muted:#a0a0ab;--bg:#15161a;--line:#2e3036;
--head:#1f2126;--accent:#6ea8ff;--ok:#4ac26b;--warn:#d4a72c;--bad:#ff6b6b}}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--fg);
background:var(--bg);max-width:1060px;margin:0 auto;padding:24px 16px 64px;line-height:1.55;font-size:15px}
h1{font-size:27px;border-bottom:3px solid var(--accent);padding-bottom:8px}
h2{font-size:21px;margin-top:34px;border-bottom:1px solid var(--line);padding-bottom:4px;color:var(--accent)}
h3{font-size:17px}
table{border-collapse:collapse;width:100%;margin:12px 0 18px;font-size:13px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}
th{background:var(--head)}
blockquote{border-left:4px solid var(--warn);margin:14px 0;padding:6px 14px;color:var(--muted)}
code{background:var(--head);padding:1px 4px;border-radius:3px;font-size:12.5px}
a.cite{text-decoration:none;font-size:11px;vertical-align:super;padding:0 2px;border-radius:3px}
a.cite.verified{color:var(--ok)} a.cite.needs_review,a.cite.unverified{color:var(--warn)}
a.cite.contradicted,a.cite.unsupported,a.cite.missing{color:var(--bad);font-weight:600}
.banner{border:2px solid var(--bad);background:rgba(207,34,46,.08);padding:12px 16px;border-radius:6px;margin:16px 0}
.banner.ok{border-color:var(--ok);background:rgba(26,127,55,.07)}
.evidence{font-size:12.5px} .evidence .claim{border-top:1px solid var(--line);padding:8px 0}
.badge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:10px;border:1px solid currentColor}
.badge.verified{color:var(--ok)} .badge.needs_review,.badge.unverified{color:var(--warn)}
.badge.contradicted,.badge.unsupported{color:var(--bad)}
.quote{color:var(--muted);white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.disclaimer{border:1px solid var(--line);background:var(--head);color:var(--muted);font-size:12.5px;padding:8px 12px;
border-radius:6px;margin:12px 0}
.disclaimer b{color:var(--fg)}
.watermark{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;
z-index:0;overflow:hidden}
.watermark span{transform:rotate(-30deg);font-size:clamp(20px,5.2vw,64px);font-weight:700;letter-spacing:.06em;white-space:nowrap;
color:var(--bad);opacity:.07}
@media print{body{max-width:none;font-size:11px}table{display:table;font-size:9.5px}tr{page-break-inside:avoid}
h2{page-break-after:avoid} a.cite{color:#000!important}}
"""


@dataclass
class ClaimView:
    id: int
    status: str
    stream: str
    statement: str
    metric: str | None = None
    value: str | None = None
    unit: str | None = None
    period: str | None = None
    importance: str = "normal"
    note: str | None = None
    corrects: int | None = None
    citations: list[dict[str, Any]] = field(
        default_factory=list
    )  # doc_title, page, lines, quote, url, accessed_at


def _cite_title(c: ClaimView) -> str:
    parts = [f"C{c.id} · {STATUS_LABEL.get(c.status, c.status)} · {c.statement[:160]}"]
    for ct in c.citations[:2]:
        if ct.get("url"):
            parts.append(f"{ct['url']} (accessed {ct.get('accessed_at') or '?'})")
        else:
            parts.append(
                f"{ct.get('doc_title')} p{ct.get('page')} L{ct.get('lines')}: {(ct.get('quote') or '')[:160]}"
            )
    return " | ".join(parts)


def link_citations(body_html: str, claims: dict[int, ClaimView]) -> str:
    def sub(m: re.Match) -> str:
        cid = int(m.group(1))
        c = claims.get(cid)
        cls = c.status if c else "missing"
        title = _cite_title(c) if c else f"C{cid} is not in this run's ledger"
        return f'<a class="cite {cls}" href="#claim-{cid}" title="{_html.escape(title)}">[C{cid}]</a>'

    return _CITE.sub(sub, body_html)


def evidence_appendix(claims: list[ClaimView]) -> str:
    rows = []
    for c in claims:
        cites = []
        for ct in c.citations:
            grade = (f" <small>evidence {ct['grade']}: {_html.escape(ct.get('grade_label') or '')}</small>"
                     if ct.get("grade") else "")  # fmt: skip
            if ct.get("url") and not str(ct["url"]).lower().startswith(("http://", "https://")):
                # a computed figure: only a fincalc call save_claim re-ran is "deterministic" (grade D, #242)
                cites.append(f"<div>🧮 <code>{_html.escape(str(ct['url'])[:200])}</code>{grade}</div>")
            elif ct.get("url"):
                cites.append(f'<div>🌐 <a href="{_html.escape(ct["url"])}">{_html.escape(ct["url"][:90])}</a> '
                             f'<span class="quote">accessed {_html.escape(str(ct.get("accessed_at") or "?"))}</span>'
                             f"{grade}</div>")  # fmt: skip
            else:
                found = {True: "✓ quote found", False: "✗ quote NOT found", None: ""}[ct.get("quote_found")]
                cites.append(f"<div>📄 {_html.escape(ct.get('doc_title') or '?')} — page {ct.get('page')}, lines "
                             f"{ct.get('lines')} <small>{found}</small>"
                             f"<div class='quote'>{_html.escape((ct.get('quote') or '')[:500])}</div></div>")  # fmt: skip
        figure = " ".join(x for x in (c.value, c.unit, f"({c.period})" if c.period else "") if x)
        corr = f" · corrects C{c.corrects}" if c.corrects else ""
        note = f"<div class='quote'>verifier: {_html.escape((c.note or '')[:600])}</div>" if c.note else ""
        rows.append(f"<div class='claim' id='claim-{c.id}'><b>C{c.id}</b> "
                    f"<span class='badge {c.status}'>{STATUS_LABEL.get(c.status, c.status)}</span> "
                    f"<small>{_html.escape(c.stream)} · {c.importance}{corr}</small><br>"
                    f"{_html.escape(c.statement)}{(' — <b>' + _html.escape(figure) + '</b>') if figure else ''}"
                    f"{''.join(cites)}{note}</div>")  # fmt: skip
    return (
        "<section class='evidence'><h2>Evidence: claims cited in this report</h2>"
        + "".join(rows)
        + "</section>"
    )


def render_html(report_md: str, claims: dict[int, ClaimView], *, title: str, generated_at: datetime,
                gate_ok: bool, gate_blocking: list[str], gate_warnings: list[str]) -> str:  # fmt: skip
    md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    body = link_citations(md.render(report_md), claims)
    if gate_ok:
        banner = (f"<div class='banner ok'>✓ Publish gate passed · every cited claim is in the ledger · "
                  f"{len(gate_warnings)} warning(s) · generated {generated_at:%d %b %Y %H:%M %Z}</div>")  # fmt: skip
    else:
        items = "".join(f"<li>{_html.escape(b[:300])}</li>" for b in gate_blocking[:20])
        banner = (f"<div class='banner'><b>NOT PUBLISHED: the publish gate blocked this report.</b> "
                  f"Treat it as a draft.<ul>{items}</ul></div>")  # fmt: skip
    cited = sorted({int(x) for x in _CITE.findall(report_md)})
    appendix = evidence_appendix([claims[i] for i in cited if i in claims])
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><meta name='robots' content='noindex,nofollow'>"
            f"<title>{_html.escape(title)}</title><style>{CSS}</style></head><body>{export_notice()}{banner}{body}"
            f"{appendix}{export_notice(footer=True)}</body></html>")  # fmt: skip


def export_notice(*, footer: bool = False) -> str:
    """The §D.8 export disclaimer; the top copy also carries the watermark (fixed, so it prints on every PDF page)."""
    note = (f"<div class='disclaimer' role='note'><b>{_html.escape(EXPORT_WATERMARK)}.</b> "
            f"{_html.escape(EXPORT_DISCLAIMER)}</div>")  # fmt: skip
    if footer:
        return f"<footer>{note}</footer>"
    return (
        f"<div class='watermark' aria-hidden='true'><span>{_html.escape(EXPORT_WATERMARK)}</span></div>{note}"
    )
