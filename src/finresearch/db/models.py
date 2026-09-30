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

    claim: Mapped[Claim] = relationship(back_populates="citations")


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
