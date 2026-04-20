"""SQLAlchemy models for vibebot persistent state."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Network(Base):
    __tablename__ = "networks"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(default=6697)
    tls: Mapped[bool] = mapped_column(default=True)
    nick: Mapped[str] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    realname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sasl_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sasl_password: Mapped[str | None] = mapped_column(String(255), nullable=True)

    channels: Mapped[list[Channel]] = relationship(back_populates="network", cascade="all, delete-orphan")


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("network_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    network_id: Mapped[int] = mapped_column(ForeignKey("networks.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    autojoin: Mapped[bool] = mapped_column(default=True)

    network: Mapped[Network] = relationship(back_populates="channels")


class AclRule(Base):
    __tablename__ = "acl_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Glob over nick!ident@host, e.g. "admin!*@*.example.com"
    mask: Mapped[str] = mapped_column(String(255))
    # Permission token; the bot checks require(perm) before privileged actions.
    permission: Mapped[str] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Repo(Base):
    __tablename__ = "repos"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(String(128), default="main")
    enabled: Mapped[bool] = mapped_column(default=True)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModuleState(Base):
    __tablename__ = "module_state"
    __table_args__ = (UniqueConstraint("repo_name", "module_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    repo_name: Mapped[str] = mapped_column(String(128))
    module_name: Mapped[str] = mapped_column(String(128))
    loaded: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=False)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Schedule(Base):
    """A persisted schedule entry; the authoritative store for user/module schedules.

    APScheduler is the firing engine but its jobstore is ephemeral for these
    rows — the `ScheduleService` re-registers APScheduler jobs from this table
    on startup, so closures and handler references do not need to be picklable.
    """

    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_nick: Mapped[str] = mapped_column(String(64))
    owner_mask: Mapped[str] = mapped_column(String(255))
    owner_network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repo_name: Mapped[str] = mapped_column(String(128))
    module_name: Mapped[str] = mapped_column(String(128))
    handler_name: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    trigger_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    misfire_grace_seconds: Mapped[int] = mapped_column(default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
