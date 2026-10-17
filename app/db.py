"""Database models. SQLite for local dev; the schema is portable to Postgres.

Traceability: originals (JobFile), every stage output per run (StageRun), and
an append-only review trail (ReviewEvent) live together under a Job. Approval
freezes all of it into a Package snapshot.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Shop(Base):
    __tablename__ = "shops"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String)
    rate_card: Mapped[dict] = mapped_column(JSON)
    rate_card_is_example: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    # needs_info | in_review | approved | rejected | error
    status: Mapped[str] = mapped_column(String, default="needs_info")
    spec: Mapped[dict] = mapped_column(JSON)
    current_run: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    files: Mapped[list[JobFile]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="JobFile.uploaded_at")
    runs: Mapped[list[StageRun]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="StageRun.id")
    events: Mapped[list[ReviewEvent]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="ReviewEvent.id")
    packages: Mapped[list[Package]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobFile(Base):
    __tablename__ = "job_files"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    original_name: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    job: Mapped[Job] = relationship(back_populates="files")


class StageRun(Base):
    __tablename__ = "stage_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    run_no: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String)  # intake | geometry | drawing | dfm | quote
    status: Mapped[str] = mapped_column(String)  # ok | error | skipped
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    job: Mapped[Job] = relationship(back_populates="runs")


class ReviewEvent(Base):
    """Append-only. The latest event per (run_no, target) is the current decision."""

    __tablename__ = "review_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    run_no: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String)  # quote line id, "job", or "spec"
    action: Mapped[str] = mapped_column(String)  # approve | edit | reject | approve_job | reject_job | update_spec | rerun
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    job: Mapped[Job] = relationship(back_populates="events")


class Package(Base):
    """Frozen, approved record: originals' hashes, stage outputs, decisions, totals."""

    __tablename__ = "packages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    run_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    approved_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    job: Mapped[Job] = relationship(back_populates="packages")


def make_engine(url: str | None = None):
    url = url or os.environ.get("PARTS_CAD_DB", "sqlite:///data/parts_cad.db")
    if url.startswith("sqlite:///"):
        os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
