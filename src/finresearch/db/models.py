"""Postgres schema (SQLAlchemy 2.0).

The claim ledger is the heart of the product: every number that reaches a report is a `Claim` with at least one
`Citation` pointing at a document page + line span (or a URL + access time), and a verification status.
Documents are immutable and keyed by sha256; line numbers match `grep -n` on the full pdftotext extract.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 1024  # qwen3-embedding:0.6b


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {dict[str, Any]: JSONB, list[Any]: JSONB}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- entities & documents
class Company(TimestampMixin, Base):
    __tablename__ = "company"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    cin: Mapped[str | None] = mapped_column(String(30))
    nse_symbol: Mapped[str | None] = mapped_column(String(30), index=True)
    bse_code: Mapped[str | None] = mapped_column(String(20), index=True)  # BSE scrip code, e.g. "526433"
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    website: Mapped[str | None] = mapped_column(String(300))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    documents: Mapped[list[Document]] = relationship(back_populates="company")

    @property
    def stock_key(self) -> str | None:
        """The listed stock's key on the stock pages and APIs: the NSE symbol, else "BSE:<scrip code>" for a
        BSE-only stock, else None (an unlisted IPO, a fund, a bond)."""
        return self.nse_symbol or (f"BSE:{self.bse_code}" if self.bse_code else None)

    @property
    def exchange(self) -> str | None:
        return "NSE" if self.nse_symbol else ("BSE" if self.bse_code else None)


class Document(TimestampMixin, Base):
    """An immutable source file (PDF/HTML) identified by sha256."""

    __tablename__ = "document"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # see ingest.documents.DocKind
    title: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str] = mapped_column(Text)
    text_path: Mapped[str | None] = mapped_column(
        Text
    )  # full pdftotext -layout extract (with \f page breaks)
    bytes: Mapped[int] = mapped_column(BigInteger)
    pages: Mapped[int | None] = mapped_column(Integer)
    scanned_pages: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # http status, headers, pipeline versions

    company: Mapped[Company | None] = relationship(back_populates="documents")
    page_rows: Mapped[list[DocumentPage]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    sections: Mapped[list[Section]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentPage(Base):
    __tablename__ = "document_page"
    __table_args__ = (UniqueConstraint("document_id", "page_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)  # 1-based PDF page index
    line_start: Mapped[int] = mapped_column(Integer)  # 1-based line in text_path (grep -n compatible)
    line_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    text_source: Mapped[str] = mapped_column(String(20))  # pdftotext | tesseract | glm-ocr
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[list[Any]] = mapped_column(default=list)

    document: Mapped[Document] = relationship(back_populates="page_rows")


class Section(Base):
    __tablename__ = "section"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    canonical: Mapped[str] = mapped_column(String(60))  # e.g. RISK_FACTORS, RESTATED_FINANCIALS
    heading: Mapped[str] = mapped_column(String(300))
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    document: Mapped[Document] = relationship(back_populates="sections")


class Chunk(Base):
    """Retrieval unit with line anchors; hybrid search = full-text (tsv) + vector (embedding)."""

    __tablename__ = "chunk"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[int | None] = mapped_column(ForeignKey("section.id", ondelete="SET NULL"))
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    embed_model: Mapped[str | None] = mapped_column(String(80))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))

    __table_args__ = (
        Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# --------------------------------------------------------------------------- research runs & claim ledger
class ResearchRun(TimestampMixin, Base):
    __tablename__ = "research_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # ipo_report | stock_report | ...
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | paused | done | failed
    resume_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manifest: Mapped[dict[str, Any]] = mapped_column(default=dict)  # models, prompt hashes, parser versions

    claims: Mapped[list[Claim]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Claim(TimestampMixin, Base):
    __tablename__ = "claim"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id", ondelete="CASCADE"), index=True)
    stream: Mapped[str] = mapped_column(String(60))  # financials | valuation | news | ...
    statement: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(20))  # numeric | factual | opinion
    metric: Mapped[str | None] = mapped_column(String(120))
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    unit: Mapped[str | None] = mapped_column(String(40))
    period: Mapped[str | None] = mapped_column(String(60))
    importance: Mapped[str] = mapped_column(String(10), default="normal")  # high | normal | low
    status: Mapped[str] = mapped_column(String(20), default="unverified", index=True)
    # unverified | verified | contradicted | unsupported | needs_review
    verifier_note: Mapped[str | None] = mapped_column(Text)
    # deterministic gate results, e.g. {"value_in_source": true, "conflict_with": [12], "stale": false}
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)
    corrects_claim_id: Mapped[int | None] = mapped_column(ForeignKey("claim.id", ondelete="SET NULL"))

    run: Mapped[ResearchRun] = relationship(back_populates="claims")
    citations: Mapped[list[Citation]] = relationship(back_populates="claim", cascade="all, delete-orphan")


class Citation(Base):
    __tablename__ = "citation"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"))
    page_no: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[str | None] = mapped_column(Text)
    quote_found: Mapped[bool | None] = mapped_column()  # deterministic check: quote present in cited lines
    url: Mapped[str | None] = mapped_column(Text)
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # a web quote checked against the page text the `fetch_page` MCP tool stored (WebSnapshot.sha256); quote_found
    # then says whether the quote is on that page (#242). None: no snapshot, the quote is unchecked (grade C).
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    # a figure computed by fincalc: {"function", "args", "inputs": [claim ids], "result", "matches", "inputs_ok",
    # "detail"}, re-executed by save_claim (#242). quote_found = matches and inputs_ok.
    computation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    claim: Mapped[Claim] = relationship(back_populates="citations")


class WebSnapshot(Base):
    """The text of a web page as the `fetch_page` MCP tool returned it to an agent (#242): web quotes are checked
    against it, and its sha256 is stored on the citation, so a B-grade web citation names the exact text it was
    checked against. Exchange tools store their JSON responses the same way (run_id None)."""

    __tablename__ = "web_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_run.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str | None] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentStep(Base):
    """One orchestrated step of a research run (idempotent by (run_id, key)); enables resume after a crash or limit."""

    __tablename__ = "agent_step"
    __table_args__ = (UniqueConstraint("run_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(
        String(120)
    )  # e.g. planner, stream:financials, verify:financials, round2:...
    stage: Mapped[str] = mapped_column(
        String(40)
    )  # facts | plan | stream | verify | case | synthesis | critic
    role: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending | running | done | failed | deferred (limit / budget; resumable)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    tier: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(60))
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd_est: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    num_turns: Mapped[int | None] = mapped_column(Integer)
    five_hour_before: Mapped[float | None] = mapped_column(Float)
    five_hour_after: Mapped[float | None] = mapped_column(Float)
    transcript_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- report conversations
class Conversation(TimestampMixin, Base):
    """A follow-up chat about one research run, continued with Claude Code session resume."""

    __tablename__ = "conversation"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    session_id: Mapped[str | None] = mapped_column(String(80))  # Claude Code session to --resume
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="ConversationMessage.id"
    )


class ConversationMessage(TimestampMixin, Base):
    __tablename__ = "conversation_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # deterministic checks on assistant answers: cited claims, unknown/contradicted citations, uncited figures
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)
    tier: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(60))
    num_turns: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# --------------------------------------------------------------------------- personal suggestions
class InvestorProfile(Base):
    """The investor's profile and personal rules (one row per profile name; the app uses "default")."""

    __tablename__ = "investor_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)  # finresearch.suggest.profile.Profile
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Decision(TimestampMixin, Base):
    """Decision journal: one personal suggestion for a run, the investor's action and the outcome."""

    __tablename__ = "decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_run.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    action: Mapped[str] = mapped_column(
        String(30)
    )  # APPLY | APPLY-CONDITIONAL | SKIP (after rule enforcement)
    lots: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str | None] = mapped_column(String(20))  # retail | shni | bhni
    suggestion: Mapped[dict[str, Any]] = mapped_column(default=dict)  # agent output + enforcement notes
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)  # profile, live metrics, rule results
    # what actually happened (entered later)
    user_action: Mapped[str | None] = mapped_column(String(20))  # applied | skipped
    applied_lots: Mapped[int | None] = mapped_column(Integer)
    allotted_lots: Mapped[int | None] = mapped_column(Integer)
    issue_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    listing_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    exit_date: Mapped[date | None] = mapped_column(Date)
    outcome: Mapped[dict[str, Any]] = mapped_column(default=dict)  # computed returns (fincalc)
    notes: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- monitoring after the report
class Watch(TimestampMixin, Base):
    """A company being monitored.

    kind "ipo": subscription to the close, allotment, listing and anchor lock-ins (the IPO dates are set).
    kind "stock": a daily after-close check for results filings, corporate actions, holdings and large moves.

    `exchange` says where a stock watch reads its data: "NSE" (every watch before BSE-only stocks, keyed by
    `nse_symbol`) or "BSE" (a BSE-only stock, keyed by `bse_code`; `nse_symbol` is then empty). `key` is the
    instrument key used in slot names, alerts and the stock pages: the NSE symbol, or "BSE:<scrip code>".
    """

    __tablename__ = "watch"
    __table_args__ = (
        CheckConstraint("nse_symbol IS NOT NULL OR bse_code IS NOT NULL", name="ck_watch_has_instrument"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="ipo", server_default="ipo")
    nse_symbol: Mapped[str | None] = mapped_column(String(30))
    exchange: Mapped[str] = mapped_column(String(10), default="NSE", server_default="NSE")
    bse_code: Mapped[str | None] = mapped_column(String(20))
    open_date: Mapped[date | None] = mapped_column(Date)
    close_date: Mapped[date | None] = mapped_column(Date)
    # expected T+1 (exchange days); confirm with the registrar
    allotment_date: Mapped[date | None] = mapped_column(Date)
    # expected T+3; replaced by NSE's listing date once known
    listing_date: Mapped[date | None] = mapped_column(Date)
    anchor_shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    active: Mapped[bool] = mapped_column(default=True)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    @property
    def key(self) -> str:
        """The instrument key: the NSE symbol for an NSE watch (unchanged slot names), "BSE:<code>" for a BSE one."""
        if self.exchange == "BSE" and self.bse_code:
            return f"BSE:{self.bse_code}"
        return self.nse_symbol or f"BSE:{self.bse_code}"

    @property
    def label(self) -> str:
        """How alerts name the stock: "INFY" on NSE, "BSE 526433" on BSE."""
        return f"BSE {self.bse_code}" if self.exchange == "BSE" and self.bse_code else (self.nse_symbol or "")


class MonitorJob(Base):
    """One scheduled check. `slot` is unique, so a check can never run twice for the same slot."""

    __tablename__ = "monitor_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watch.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # subscription | allotment | listing | lockin
    slot: Mapped[str] = mapped_column(String(120), unique=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )  # pending|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Alert(TimestampMixin, Base):
    __tablename__ = "alert"
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int | None] = mapped_column(ForeignKey("watch.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warn | action
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- market data
class IpoOffer(TimestampMixin, Base):
    __tablename__ = "ipo_offer"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(30), index=True)
    open_date: Mapped[date | None] = mapped_column(Date)
    close_date: Mapped[date | None] = mapped_column(Date)
    listing_date: Mapped[date | None] = mapped_column(Date)
    price_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    fresh_issue_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    ofs_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SubscriptionSnapshotRow(Base):
    __tablename__ = "subscription_snapshot"
    __table_args__ = (UniqueConstraint("nse_symbol", "as_of", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    nse_symbol: Mapped[str] = mapped_column(String(30), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(30))  # nse_combined | nse_only | chittorgarh ...
    total_times: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    categories: Mapped[list[Any]] = mapped_column(default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)


class IvHistory(Base):
    """One day's at-the-money implied volatility for an F&O underlying, recorded after the close by the monitor
    (roadmap item 18). `expiry` is the contract the IV was read from: the nearest expiry at least a week away, so
    same-week expiries do not add noise. IV values are annual % as NSE publishes them."""

    __tablename__ = "iv_history"
    __table_args__ = (UniqueConstraint("symbol", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    day: Mapped[date] = mapped_column(Date)
    expiry: Mapped[date] = mapped_column(Date)
    underlying: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    atm_strike: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    atm_iv: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    call_iv: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    put_iv: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    skew_25d: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))  # 25-delta put IV − call IV, vol points
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NSE's chain timestamp
    source: Mapped[str] = mapped_column(String(200), default="https://www.nseindia.com/option-chain")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- forecast ledger (calibration)
class Forecast(Base):
    """One probability forecast of a defined, checkable event, logged before the outcome is known and scored after.

    Written by `finresearch.signals.ledger.record` (signal providers) and `record_run` (research-run verdicts);
    resolved by the monitor with the resolver registered for (asset, event_kind). `probability` is P(event) and is
    None for a "no call" (the verdict took no side), which is kept for coverage but never scored. At most one open
    forecast per asset + instrument + event kind + IST day (`dedupe_key`).
    """

    __tablename__ = "forecast"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    asset: Mapped[str] = mapped_column(String(20), index=True)  # ipo | stock | fund | bond | fno
    instrument: Mapped[str] = mapped_column(String(60), index=True)  # NSE symbol, ISIN, AMFI code ...
    name: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(80))  # "run:<id>" | "signal:<provider>"
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_run.id", ondelete="SET NULL"), index=True)
    event_kind: Mapped[str] = mapped_column(String(60))  # resolver key, e.g. listing_gain | excess_return_12m
    event: Mapped[str] = mapped_column(Text)  # the event in words, exactly as it will be checked
    horizon: Mapped[str] = mapped_column(String(60))
    resolve_on: Mapped[date] = mapped_column(Date, index=True)  # first day the outcome can be checked
    probability: Mapped[float | None] = mapped_column(Float)  # P(event); None = no call
    interval_low: Mapped[float | None] = mapped_column(Float)
    interval_high: Mapped[float | None] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(40))
    score: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(200))
    validation_status: Mapped[str] = mapped_column(String(20))
    inputs: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # snapshot of what the resolver and review need
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | resolved | void
    outcome: Mapped[int | None] = mapped_column(Integer)  # 1 = the event happened, 0 = it did not
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_value: Mapped[float | None] = mapped_column(Float)  # e.g. the listing gain in %
    resolution_note: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)


class IpoHistory(Base):
    """One past issue's final subscription book and listing-day prices: the training set for the IPO base rates and
    listing model (docs/dev/RESEARCH_ROADMAP.md §D.1, item 2). Filled by `finresearch ipo harvest`
    (finresearch.evals.ipo_history).

    Subscription scope: NSE `ipo-detail` `activeCat` = the COMBINED NSE+BSE book, ex-anchor, with shares offered on
    the LOWER price band (NSE's convention). Times are recomputed as bid / offered. These are FINAL numbers (after the
    close), which a retail applicant cannot know at the 5 pm UPI cut-off.
    """

    __tablename__ = "ipo_history"
    __table_args__ = (UniqueConstraint("symbol", "series", "ipo_start"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    series: Mapped[str] = mapped_column(String(10))  # EQ (mainboard) | SME
    company: Mapped[str | None] = mapped_column(String(300))
    ipo_start: Mapped[date | None] = mapped_column(Date)
    ipo_end: Mapped[date | None] = mapped_column(Date)
    listing_date: Mapped[date | None] = mapped_column(Date, index=True)
    issue_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    issue_size_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # from NSE's issue-size text
    public_book_cr: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2)
    )  # ex-anchor shares offered x issue price
    fresh_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    ofs_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    ofs_share: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    qib_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    nii_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    bnii_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    snii_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    retail_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    employee_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    total_times: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    subscription_scope: Mapped[str | None] = mapped_column(String(40))  # nse_combined (activeCat)
    subscription_updated: Mapped[str | None] = mapped_column(String(80))  # NSE's own "Updated as on ..." text
    list_open: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    list_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    list_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    list_close: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    list_vwap: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    list_prev_close: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # NSE sets it to the issue price
    return_open: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))  # open / issue - 1
    return_close: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    nifty_ret20_close: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6)
    )  # 20 sessions to the issue close
    nifty_ret20_listing: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))  # 20 sessions before listing
    ipo_count_90d: Mapped[int | None] = mapped_column(
        Integer
    )  # same-board listings in the 90 days to the close
    post_2022: Mapped[bool | None] = mapped_column()  # opened on/after 4-Apr-2022 (SEBI NII allotment reform)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # complete | partial | error
    missing: Mapped[list[Any]] = mapped_column(default=list)  # fields that could not be read, with reasons
    source: Mapped[dict[str, Any]] = mapped_column(default=dict)  # URLs and fetch times
    raw: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # activeCat rows and issue-info subset, for audit
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionArchiveSlot(Base):
    """One archive pass of every open issue's subscription book (item 15). The unique slot key means two monitor
    processes never archive the same slot twice."""

    __tablename__ = "subscription_archive_slot"
    slot: Mapped[str] = mapped_column(String(40), primary_key=True)  # e.g. "2026-09-30T13:00"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --------------------------------------------------------------------------- intraday archive (5D charts)
class IntradaySeriesRow(Base):
    """One session's 1-minute price series for an NSE equity or index (NSE's quote-page 1D chart), kept so that
    multi-day intraday charts (5D) work: NSE serves only the current session. Written by the intraday API whenever a
    page fetches a series (viewed symbols archive themselves) and completed after the close by the monitor for
    watched stocks and the main indices (monitor.intraday). `ticks` = [[NSE ts ms (IST wall clock), price, phase]]."""

    __tablename__ = "intraday_series"
    __table_args__ = (UniqueConstraint("kind", "symbol", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # equity | index
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    ticks: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    tick_count: Mapped[int] = mapped_column(Integer, default=0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    complete: Mapped[bool] = mapped_column(Boolean, default=False)  # recorded after the 15:30 close
    source: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- rules and alerts for every asset (item 10)
class AlertRuleState(Base):
    """Where one alert rule stands for one instrument, so a rule fires once when its condition becomes true and not
    again until it has cleared (and its cooldown has passed). `rule_sig` is a digest of the rule's definition: editing
    the rule (metric, comparison, value, params) starts it afresh. `baseline` holds what change metrics compare
    against (the last signal action, rating, TER)."""

    __tablename__ = "alert_rule_state"
    __table_args__ = (UniqueConstraint("rule_id", "instrument"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(40), index=True)
    instrument: Mapped[str] = mapped_column(String(40))
    rule_sig: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(10), default="new")  # new | clear | fired | unknown
    value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    source: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)  # why the value is unknown, or a suppressed re-fire
    baseline: Mapped[dict[str, Any]] = mapped_column(default=dict)
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # when the status last changed
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fired_count: Mapped[int] = mapped_column(Integer, default=0)
    suppressed_count: Mapped[int] = mapped_column(Integer, default=0)  # re-fires held back by the cooldown
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertEvalSlot(Base):
    """One evaluation pass of the alert rules ("intraday:2026-09-30T10:15", "daily:2026-09-30"): the unique key means
    two monitor processes never evaluate the same pass twice."""

    __tablename__ = "alert_eval_slot"
    slot: Mapped[str] = mapped_column(String(60), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)


class NotificationSetting(Base):
    """Delivery settings, one row per key ("ntfy", "telegram", "macos", "general"). The secrets (ntfy topic and
    token, Telegram bot token) are in the macOS Keychain; `value` holds `{"secret_ref": ...}` references for them
    (finresearch.secrets). They are masked by the API and never put in the profile (which reaches the model)."""

    __tablename__ = "notification_setting"
    key: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AlertDelivery(Base):
    """One alert sent (or to be sent) to one channel, with its attempts: the delivery log. A test send has no alert."""

    __tablename__ = "alert_delivery"
    __table_args__ = (UniqueConstraint("alert_id", "channel"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alert.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # ntfy | telegram | macos
    priority: Mapped[str] = mapped_column(
        String(10), default="default"
    )  # min | low | default | high | urgent
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    click: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)  # secrets redacted
    held: Mapped[str | None] = mapped_column(String(100))  # e.g. "quiet hours until 07:00"
    test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- household finances (WEALTH, /wealth)
# Personal financial data entered by hand: bank deposits, retirement accounts, gold, property, loans, goals and
# insurance. Local database only; never sent to an LLM (finresearch.wealth). Holds no identity data: a bank or
# lender label ("HDFC Bank"), never an account number.
class WealthAsset(Base):
    """An asset outside the demat/MF portfolio. Its value on a day comes from finresearch.wealth.calc: an FD/RD from
    its principal, booked rate and compounding; EPF/PPF from the last entered balance plus contributions and
    interest at the stated rate; everything else from the last dated valuation (wealth_valuation)."""

    __tablename__ = "wealth_asset"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(12)
    )  # fd | rd | epf | ppf | nps | gold | sgb | real_estate | cash | other
    name: Mapped[str] = mapped_column(String(120))
    institution: Mapped[str | None] = mapped_column(String(120))  # bank / post office / NPS CRA label
    asset_class: Mapped[str | None] = mapped_column(
        String(20)
    )  # override for "other": Equity | Debt | Gold | ...
    principal: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))  # FD principal
    rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))  # booked / declared rate, % a year
    compounding: Mapped[int] = mapped_column(
        Integer, default=4
    )  # FD compounding per year; 0 = interest paid out
    monthly_contribution: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))  # RD instalment, EPF/PPF/NPS
    start_date: Mapped[date | None] = mapped_column(Date)
    maturity_date: Mapped[date | None] = mapped_column(Date)
    liquid: Mapped[bool] = mapped_column(Boolean, default=False)  # counts towards the emergency fund
    equity_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # NPS: the equity share (E); rest debt
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WealthValuation(Base):
    """A dated value of a manual asset (a balance, an NPS statement value, a property estimate). The latest row on or
    before a day values the asset that day; nothing is marked to market."""

    __tablename__ = "wealth_valuation"
    __table_args__ = (UniqueConstraint("asset_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("wealth_asset.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 2))


class WealthLoan(Base):
    """A loan. Outstanding principal follows the amortisation schedule from `start_date` (first EMI one month
    later) unless a dated statement balance (`outstanding`, `outstanding_as_of`) is entered."""

    __tablename__ = "wealth_loan"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))  # home | car | personal | education | other
    name: Mapped[str] = mapped_column(String(120))
    lender: Mapped[str | None] = mapped_column(String(120))
    principal: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    rate_pct: Mapped[Decimal] = mapped_column(Numeric(7, 3))
    tenure_months: Mapped[int] = mapped_column(Integer)
    emi: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2)
    )  # None = computed from principal, rate, tenure
    start_date: Mapped[date] = mapped_column(Date)
    outstanding: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    outstanding_as_of: Mapped[date | None] = mapped_column(Date)
    floating: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WealthGoal(Base):
    """A goal: a target in today's rupees on a date, funded by linked assets (and a share of the portfolio) plus a
    monthly SIP that steps up once a year. Planned by a seeded Monte Carlo (finresearch.wealth.goals)."""

    __tablename__ = "wealth_goal"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    target_inr: Mapped[Decimal] = mapped_column(Numeric(20, 2))  # in today's rupees
    target_date: Mapped[date] = mapped_column(Date)
    priority: Mapped[str] = mapped_column(String(8), default="medium")  # high | medium | low
    inflation_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(6))
    current_inr: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), default=Decimal(0)
    )  # saved outside linked assets
    monthly_sip: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    step_up_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(0))
    linked_asset_ids: Mapped[list[Any]] = mapped_column(default=list)
    portfolio_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal(0)
    )  # share of the portfolio
    equity_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # None = the time-to-goal glide
    gold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(0))
    in_cover: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # counted in the term-cover need (e.g. education)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WealthPolicy(Base):
    """An insurance policy: cover only (arithmetic on sum assured; no product view)."""

    __tablename__ = "wealth_policy"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # term | health | other
    name: Mapped[str] = mapped_column(String(120))
    cover_inr: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    premium_inr: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))  # a year
    end_date: Mapped[date | None] = mapped_column(Date)
    employer: Mapped[bool] = mapped_column(Boolean, default=False)  # group cover through an employer
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- personal portfolio (roadmap items 8-9)
# Personal financial data: it lives only in this local database (and the uploaded files under data/portfolio/), is
# never sent to an LLM, and holds no identity data (no PAN, name, email, address or phone from a CAS; the folio or
# broker account label is enough). Transactions are the source of truth; lots and disposals are derived from them by
# finresearch.portfolio.lots (FIFO per holding, i.e. per instrument and account/folio) and rebuilt after every change.
class PortfolioImport(Base):
    """One imported file (CAS PDF or broker CSV) or manual batch. `sha256` refuses the identical file twice. The CAS
    password is never stored here or anywhere else."""

    __tablename__ = "portfolio_import"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # cas | tradebook
    source: Mapped[str] = mapped_column(String(20))  # CAMS | KFINTECH | zerodha | groww | upstox
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    saved_path: Mapped[str | None] = mapped_column(String(600))  # under settings.portfolio_dir
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)  # counts, period, reconciliation, warnings
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioHolding(Base):
    """One instrument in one account (a demat/broker account or an MF folio). FIFO runs inside a holding, as Indian
    tax requires (CBDT Circular 768: first-in-first-out per demat account; per folio for MF units)."""

    __tablename__ = "portfolio_holding"
    __table_args__ = (UniqueConstraint("ikey", "account"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ikey: Mapped[str] = mapped_column(
        String(60)
    )  # "ISIN:INE009A01021", "NSE:INFY", "MF:120503", "NAME:<slug>"
    account: Mapped[str] = mapped_column(String(80))  # "Zerodha", "CAMS folio 1234567890 / 12", "Manual"
    asset_type: Mapped[str] = mapped_column(String(10))  # stock | mf | other
    name: Mapped[str] = mapped_column(String(300))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(30))
    bse_code: Mapped[str | None] = mapped_column(String(20))
    scheme_code: Mapped[str | None] = mapped_column(String(20))  # AMFI scheme code
    category: Mapped[str | None] = mapped_column(String(120))  # AMFI category (funds)
    sector: Mapped[str | None] = mapped_column(String(120))  # user override; else the NSE industry
    tax_class: Mapped[str | None] = mapped_column(String(20))  # user override: equity | debt_mf | other | sgb
    fmv_2018: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6)
    )  # FMV per unit on 31-Jan-2018 (grandfathering)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)  # CAS valuation, casparser type, SGB flags ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioTxn(Base):
    """A dated event on a holding: buy, sell, dividend, bonus, split or opening balance (a CAS opening balance whose
    cost is unknown). `dedupe_key` makes re-importing the same or an overlapping statement idempotent."""

    __tablename__ = "portfolio_txn"
    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_holding.id", ondelete="CASCADE"), index=True
    )
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio_import.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(12))  # buy | sell | dividend | bonus | split | opening
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))  # units (positive)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))  # per unit
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))  # gross value (dividend: the payout)
    charges: Mapped[Decimal] = mapped_column(Numeric(16, 4), default=Decimal(0))  # brokerage, STT, stamp, ...
    stt_paid: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20))  # cas | zerodha | groww | upstox | manual | nse_actions
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # split/bonus ratio, CAS description, trade id ...
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioLot(Base):
    """An open or closed FIFO lot (derived). `cost_per_unit` is None when the cost is unknown (a CAS opening
    balance); tax on such a lot cannot be computed until the cost is entered."""

    __tablename__ = "portfolio_lot"
    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_holding.id", ondelete="CASCADE"), index=True
    )
    txn_id: Mapped[int | None] = mapped_column(ForeignKey("portfolio_txn.id", ondelete="CASCADE"))
    acquired: Mapped[date | None] = mapped_column(Date)
    origin: Mapped[str] = mapped_column(String(12))  # buy | bonus | opening
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))  # after splits
    open_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    cost_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))  # incl. buy charges, after splits
    stt_paid: Mapped[bool] = mapped_column(Boolean, default=True)


class PortfolioDisposal(Base):
    """A sale matched against one lot (derived, FIFO): the unit of capital-gains tax."""

    __tablename__ = "portfolio_disposal"
    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_holding.id", ondelete="CASCADE"), index=True
    )
    txn_id: Mapped[int] = mapped_column(ForeignKey("portfolio_txn.id", ondelete="CASCADE"), index=True)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("portfolio_lot.id", ondelete="CASCADE"))
    acquired: Mapped[date | None] = mapped_column(Date)
    sold: Mapped[date] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))  # None when the lot's cost is unknown
    proceeds: Mapped[Decimal] = mapped_column(Numeric(20, 4))  # net of the sale's charges (apportioned)
    stt_paid: Mapped[bool] = mapped_column(Boolean, default=True)
    origin: Mapped[str] = mapped_column(String(12), default="buy")


class PortfolioSnapshot(Base):
    """The portfolio's value on a day "as shown": written whenever it is valued (the /portfolio page, the API, the
    daily pass); the day's latest valuation wins and `updated_at` says when. An audit record, never recomputed: past
    values are read from the canonical reconstructed history (portfolio.series, #239), which reconciles against these
    rows. Still the source for "now" (the current allocation for the drift alert, the dashboard strip, today's net
    worth). `complete` = every open holding had a current (not stale) price and a known cost, and every sale a cost."""

    __tablename__ = "portfolio_snapshot"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    invested: Mapped[Decimal] = mapped_column(
        Numeric(20, 2)
    )  # cumulative net invested (buys - sales) that day
    by_asset: Mapped[dict[str, Any]] = mapped_column(default=dict)  # asset-class label -> value
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioAis(Base):
    """The user's AIS for one financial year (named by its end year), reduced to the rows the AIS check compares
    (portfolio.ais): category, code, description, source name/TAN, security name/ISIN, date, amount, TDS, quantity,
    STT. No PAN, name, address, Aadhaar, phone, e-mail or account number is stored, and the file itself is not kept
    (only its sha256, to recognise it again). A new import for the same year replaces the old one."""

    __tablename__ = "portfolio_ais"
    fy: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(8))  # json | pdf
    items: Mapped[list[Any]] = mapped_column(default=list)
    ignored: Mapped[int] = mapped_column(Integer, default=0)  # rows of categories the check does not use
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioSetting(Base):
    """Small portfolio settings by key (e.g. "targets": target allocation % by asset class)."""

    __tablename__ = "portfolio_setting"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --------------------------------------------------------------------------- broker connections (read-only sync)
# Credentials for the user's own broker accounts live in the macOS Keychain; this table keeps references to them
# (finresearch.secrets, like notification_setting). The API masks them, errors are redacted before they are stored, and nothing here is ever put in the investor profile
# or sent to an LLM. The connectors are read-only by construction (finresearch.portfolio.connectors).
class BrokerConnection(Base):
    """One broker (or the CAS inbox) the user has set up: "groww", "zerodha", "upstox", "dhan", "angel", "cas_inbox".
    `config` holds the user's settings, with each secret (API key/secret, TOTP secret, CAS password if opted in) as a
    `{"secret_ref": ...}` Keychain reference; `token` a `secret_ref:` reference to the current access token and `token_expires_at` when the broker ends it (most expire daily). `state` keeps the
    latest positions/funds snapshot and the last reconciliation, for display only (never turned into transactions)."""

    __tablename__ = "broker_connection"
    key: Mapped[str] = mapped_column(String(20), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=True)  # daily after the close, by the monitor
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)  # secrets as Keychain references
    token: Mapped[str | None] = mapped_column(Text)  # a Keychain reference; never returned by the API
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), default="not_connected"
    )  # connected | reconnect | error | ...
    last_error: Mapped[str | None] = mapped_column(Text)  # secrets redacted
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_day: Mapped[date | None] = mapped_column(Date)  # IST day of the last scheduled sync
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrokerSyncLog(Base):
    """One sync (or inbox scan) and what it did: rows added, duplicates skipped, baselines, reconciliation differences
    and errors (redacted). The portfolio_import rows it created are listed in `summary.import_ids`."""

    __tablename__ = "broker_sync_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(20), index=True)
    trigger: Mapped[str] = mapped_column(String(12))  # manual | scheduled | inbox
    status: Mapped[str] = mapped_column(String(12))  # ok | partial | error | reconnect
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)  # secrets redacted


# --------------------------------------------------------------------------- exchange disclosures (finresearch.disclosures)
# Public market data (no personal data): NSE surveillance lists, the F&O ban list, promoter pledge, insider (PIT) and
# SAST disclosures, bulk/block deals, credit ratings, and SEBI orders that name a tracked company (PAN-scrubbed).
class DisclosureFeed(Base):
    """Where one feed stands: the last good read (payload, publisher as-of, source URL) and the last failure. A feed
    is per market ("asm", "gsm", "fno_ban", "credit_ratings", "sebi_orders"; key "*") or per stock ("pledge", "pit",
    "sast", "deals"; key = NSE symbol). A failure never replaces the last good payload; the views say "unavailable"
    when there is no good read recent enough (DATA-004)."""

    __tablename__ = "disclosure_feed"
    __table_args__ = (UniqueConstraint("dataset", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(40), default="*")
    payload: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # parsed rows / summary of the last good read
    source_url: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[str | None] = mapped_column(
        String(40)
    )  # the publisher's date (e.g. the ban list's trade date)
    ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last good read
    error: Mapped[str | None] = mapped_column(Text)
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DisclosureRecord(Base):
    """One disclosed fact kept as history: an insider trade ("pit"), a SAST disclosure ("sast"), a bulk or block deal
    ("deal"), a quarter's promoter pledge ("pledge"), a credit-rating filing ("rating") or a SEBI order matched to a
    tracked company ("sebi_order"). `dedupe_key` makes re-fetching the same rows idempotent."""

    __tablename__ = "disclosure_record"
    __table_args__ = (Index("ix_disclosure_record_lookup", "dataset", "symbol", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str | None] = mapped_column(String(40))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    issuer: Mapped[str | None] = mapped_column(String(7), index=True)  # ISIN[:7], for ratings
    day: Mapped[date | None] = mapped_column(Date)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    source_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- decision journal for every trade (#5)
# Personal data: the investor's own reasons for buying and selling. Local database only, never sent to an LLM (no
# agent, advisor prompt or MCP tool reads this table) and never put in a forwarded alert's text.
class TradeNote(Base):
    """One journal entry for a buy or a sell of any asset: the thesis, how long it should take, what would prove it
    wrong, how confident the investor is, when to look again, and (later) what happened against the thesis.

    `status`: draft (auto-created for a newly imported trade, the thesis still to write) | planned (written before the
    trade, from the pre-trade checklist) | active (thesis written, trade done) | reviewed (outcome recorded) |
    cancelled (a planned trade not taken). `txn_ids` lists the portfolio transactions it covers (one entry per
    holding, side and day, so a day of partial fills is one entry); ids of deleted transactions are ignored on read.
    """

    __tablename__ = "trade_note"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    source: Mapped[str] = mapped_column(String(12), default="manual")  # auto | manual | pretrade
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    asset_type: Mapped[str] = mapped_column(String(10), default="stock")  # stock | mf | other
    instrument: Mapped[str | None] = mapped_column(String(60))  # "NSE:INFY", "MF:120503", "ISIN:..."
    name: Mapped[str] = mapped_column(String(300))
    # no foreign key on purpose: the portfolio tables are truncated and rebuilt by tests and imports without knowing
    # about the journal; a holding that no longer exists reads as None (portfolio.journal.note_json)
    holding_id: Mapped[int | None] = mapped_column(Integer, index=True)
    txn_ids: Mapped[list[Any]] = mapped_column(default=list)
    trade_day: Mapped[date | None] = mapped_column(Date)  # the executed trade's day (or the planned day)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))  # per unit, gross
    # the thesis
    thesis: Mapped[str | None] = mapped_column(Text)
    expected_holding_days: Mapped[int | None] = mapped_column(Integer)
    invalidation: Mapped[str | None] = mapped_column(
        Text
    )  # what would prove the thesis wrong / the exit rule
    confidence_pct: Mapped[int | None] = mapped_column(Integer)  # 0-100: how likely the thesis plays out
    review_on: Mapped[date | None] = mapped_column(Date, index=True)
    review_alerted_on: Mapped[date | None] = mapped_column(Date)  # the review date a reminder was raised for
    checklist: Mapped[dict[str, Any]] = mapped_column(default=dict)  # the pre-trade checklist as shown
    # the outcome review
    outcome_verdict: Mapped[str | None] = mapped_column(String(12))  # right | wrong | mixed | too_early
    outcome_notes: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[dict[str, Any]] = mapped_column(default=dict)  # figures at review time (price, return)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
