import warnings
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

# SQLite stores Numeric as float internally. Amounts are whole-rupee scale and quantized on the way
# out, and Postgres (the real target) keeps exact NUMERIC, so the warning is noise in dev only.
warnings.filterwarnings("ignore", message=r".*does \*not\* support Decimal objects natively.*")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_number: Mapped[str | None] = mapped_column(String(16), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    lines: Mapped[list] = mapped_column(JSON, default=list)
    address_text: Mapped[str | None] = mapped_column(String(500))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Address: raw words, then candidates the server found, then the one the customer confirmed.
    address_candidates: Mapped[list | None] = mapped_column(JSON)
    address_label: Mapped[str | None] = mapped_column(String(500))
    address_lat: Mapped[float | None] = mapped_column(Float)
    address_lon: Mapped[float | None] = mapped_column(Float)
    address_precision: Mapped[str | None] = mapped_column(String(16))
    address_place_id: Mapped[str | None] = mapped_column(String(160))
    address_confidence: Mapped[float | None] = mapped_column(Float)
    route_distance_m: Mapped[int | None] = mapped_column(Integer)
    delivery_allowed: Mapped[bool | None] = mapped_column(Boolean)
    branch_id: Mapped[str | None] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(String(120))
    branch_radius_m: Mapped[int | None] = mapped_column(Integer)
    # What the customer was quoted at validation, frozen so later menu edits cannot change it.
    snapshot: Mapped[list | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class OrderEventRow(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallTranscriptRow(Base):
    """What a caller said, per voice call.

    Kept in the database rather than in memory because the evidence guards read the caller's own
    words before they let a chosen option or a confirmation through. A restart mid-call used to
    erase that evidence. Rows are deleted when the call ends.
    """
    __tablename__ = "call_transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(String(2000))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_session_factory(url: str) -> sessionmaker:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(engine) -> None:
    """create_all never alters an existing table, so add columns introduced after the first deploy."""
    existing = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in Base.metadata.tables}
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if column.name not in existing[table.name]:
                    ddl = column.type.compile(engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}'))
