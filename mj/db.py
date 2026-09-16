from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from sqlalchemy import (JSON, BigInteger, Boolean, Column, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, create_engine, event)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

def uid():
    return uuid.uuid4().hex


class Project(Base):
    __tablename__ = "projects"
    id = Column(String(32), primary_key=True, default=uid)
    owner = Column(String(100), nullable=False, default="admin")
    title = Column(String(120), nullable=False)
    brief = Column(Text, nullable=False)
    style = Column(Text, nullable=False)
    spec = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, default=time.time, nullable=False)


class Revision(Base):
    __tablename__ = "revisions"
    id = Column(String(32), primary_key=True, default=uid)
    project_id = Column(String(32), ForeignKey("projects.id"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    content = Column(JSON, nullable=False)
    content_hash = Column(String(64), nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "number"),)


class SessionToken(Base):
    __tablename__ = "sessions"
    token_hash = Column(String(64), primary_key=True)
    csrf_hash = Column(String(64), nullable=False)
    expires = Column(Float, nullable=False)
    actor = Column(String(100), nullable=False, default="admin")


class Asset(Base):
    __tablename__ = "assets"
    id = Column(String(32), primary_key=True, default=uid)
    project_id = Column(String(32), ForeignKey("projects.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    name = Column(String(200), nullable=False)
    blob_hash = Column(String(64), nullable=False)
    extension = Column(String(10), nullable=False)
    info = Column(JSON, nullable=False)
    mock = Column(Boolean, nullable=False, default=False)
    task_id = Column(String(32))
    review = Column(String(20), nullable=False, default="pending")
    motion = Column(String(30), nullable=False, default="none")
    rights = Column(Text, nullable=False, default="")
    created_at = Column(Float, default=time.time, nullable=False)


class Approval(Base):
    __tablename__ = "approvals"
    id = Column(String(32), primary_key=True, default=uid)
    project_id = Column(String(32), ForeignKey("projects.id"), nullable=False, index=True)
    gate = Column(String(5), nullable=False)
    scope_hash = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    task_id = Column(String(32))
    note = Column(Text, nullable=False)
    actor = Column(String(100), nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class Budget(Base):
    __tablename__ = "budgets"
    id = Column(String(32), primary_key=True, default=uid)
    project_id = Column(String(32), ForeignKey("projects.id"), nullable=False, index=True)
    currency = Column(String(3), nullable=False)
    limit_micros = Column(BigInteger, nullable=False)
    per_task_micros = Column(BigInteger, nullable=False)
    used_micros = Column(BigInteger, nullable=False, default=0)
    policy = Column(JSON, nullable=False)
    expires = Column(Float, nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String(32), primary_key=True, default=uid)
    project_id = Column(String(32), ForeignKey("projects.id"), nullable=False, index=True)
    kind = Column(String(30), nullable=False)
    provider = Column(String(100), nullable=False)
    state = Column(String(30), nullable=False, default="queued", index=True)
    input_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    snapshot = Column(JSON, nullable=False)
    result = Column(JSON)
    remote = Column(JSON)
    error = Column(Text)
    revision = Column(Integer, nullable=False)
    budget_id = Column(String(32), ForeignKey("budgets.id"))
    cap_micros = Column(BigInteger, nullable=False, default=0)
    fee_state = Column(String(20), nullable=False, default="free")
    settled_micros = Column(BigInteger)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    fence = Column(Integer, nullable=False, default=0)
    lease_until = Column(Float, nullable=False, default=0)
    next_run = Column(Float, nullable=False, default=0)
    created_at = Column(Float, default=time.time, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key"),)


class AuditEvent(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(32), index=True)
    action = Column(String(60), nullable=False)
    payload = Column(JSON, nullable=False)
    actor = Column(String(100), nullable=False, default="system")
    created_at = Column(Float, default=time.time, nullable=False)


class Database:
    def __init__(self, url: str):
        kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(url, **kw)
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def pragma(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA journal_mode=WAL")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def init(self):
        Base.metadata.create_all(self.engine)

    @contextmanager
    def tx(self):
        with self.sessions() as session:
            try:
                if self.sqlite:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise


def log(session, project_id, action, payload, actor="system"):
    session.add(AuditEvent(project_id=project_id, action=action, payload=payload, actor=actor))


def public(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
