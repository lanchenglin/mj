"""PostgreSQL in deployment; SQLite only for explicit local development/tests."""
import time
import uuid
from sqlalchemy import create_engine, event, ForeignKey, Integer, String, Text, JSON, BigInteger, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

def uid(): return uuid.uuid4().hex

def now(): return int(time.time())

class Base(DeclarativeBase): pass

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner: Mapped[str] = mapped_column(String(80), default="admin", index=True)
    name: Mapped[str] = mapped_column(String(150))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    budget_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    budget_used: Mapped[int] = mapped_column(BigInteger, default=0)
    budget_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[int] = mapped_column(BigInteger, default=now)

class Revision(Base):
    __tablename__ = "revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    plan: Mapped[dict] = mapped_column(JSON)
    hash: Mapped[str] = mapped_column(String(64))
    created: Mapped[int] = mapped_column(BigInteger, default=now)
    __table_args__ = (UniqueConstraint("project_id", "number"),)

class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20))
    hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(80))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[int] = mapped_column(BigInteger, default=now)

class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    blob: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    info: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewed: Mapped[bool] = mapped_column(default=False)
    mock: Mapped[bool] = mapped_column(default=False)
    actual_motion: Mapped[str] = mapped_column(String(30), default="unverified")
    rights: Mapped[str] = mapped_column(Text, default="")
    shot_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created: Mapped[int] = mapped_column(BigInteger, default=now)

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    spec: Mapped[dict] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    remote_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    charge_state: Mapped[str] = mapped_column(String(20), default="released")
    actual_cost: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    created: Mapped[int] = mapped_column(BigInteger, default=now)
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key"),)

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[int] = mapped_column(BigInteger, default=now)

class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[int] = mapped_column(BigInteger)


def database(settings):
    kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, pool_pre_ping=True, **kw)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def pragmas(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
    return engine, sessionmaker(engine, expire_on_commit=False)


def emit(db, project_id, kind, data):
    db.add(Event(project_id=project_id, kind=kind, data=data))
