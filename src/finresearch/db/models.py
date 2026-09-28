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
    bse_code: Mapped[str | None] = mapped_column(String(20))
    website: Mapped[str | None] = mapped_column(String(300))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    documents: Mapped[list[Document]] = relationship(back_populates="company")


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
