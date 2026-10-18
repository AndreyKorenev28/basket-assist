"""Схема БД (SQLAlchemy 2.0). Описание таблиц — docs/architecture.md, раздел 3.

Время хранится в UTC без таймзоны (SQLite), таймзона — у лиги.
Таблицы без tenant_id — общие справочники лиг; остальные изолированы по команде сервиса.
"""

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    # Явные имена ограничений — нужны Alembic в режиме batch для SQLite.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# ---------- Общие справочники лиг ----------


class League(Base):
    __tablename__ = "league"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Moscow")
    min_players: Mapped[int] = mapped_column(Integer, default=5)
    max_players: Mapped[int] = mapped_column(Integer, default=12)
    poll_weekday: Mapped[int] = mapped_column(Integer, default=2)
    poll_time: Mapped[str] = mapped_column(String(5), default="10:00")
    store_birth_date: Mapped[bool] = mapped_column(Boolean, default=False)
    default_venue_address: Mapped[str | None] = mapped_column(String(255))


class Tournament(Base):
    __tablename__ = "tournament"
    __table_args__ = (UniqueConstraint("league_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("league.id"))
    external_id: Mapped[str] = mapped_column(String(50))
    name: Mapped[str | None] = mapped_column(String(200))
    season: Mapped[str | None] = mapped_column(String(50))


class LeagueTeam(Base):
    __tablename__ = "league_team"
    __table_args__ = (UniqueConstraint("league_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("league.id"))
    external_id: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))


class Match(Base):
    __tablename__ = "match"
    __table_args__ = (UniqueConstraint("tournament_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournament.id"))
    external_id: Mapped[str] = mapped_column(String(50))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("league_team.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("league_team.id"))
    match_date: Mapped[date] = mapped_column(Date)
    start_at: Mapped[datetime | None] = mapped_column(DateTime)  # UTC
    time_status: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    venue_name: Mapped[str | None] = mapped_column(String(200))
    venue_address: Mapped[str | None] = mapped_column(String(255))
    score_home: Mapped[int | None] = mapped_column(Integer)
    score_away: Mapped[int | None] = mapped_column(Integer)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SeasonRosterEntry(Base):
    """Строка заявки на сезон, как её отдаёт сайт лиги."""

    __tablename__ = "season_roster_entry"
    __table_args__ = (UniqueConstraint("league_team_id", "tournament_id", "external_person_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_team_id: Mapped[int] = mapped_column(ForeignKey("league_team.id"))
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournament.id"))
    external_person_id: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(100))
    first_name: Mapped[str] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    number: Mapped[str | None] = mapped_column(String(10))
    number_source: Mapped[str | None] = mapped_column(String(20))  # roster | protocol
    position: Mapped[str | None] = mapped_column(String(50))
    birth_date: Mapped[date | None] = mapped_column(Date)  # только если league.store_birth_date
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Standing(Base):
    __tablename__ = "standing"
    __table_args__ = (UniqueConstraint("tournament_id", "league_team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournament.id"))
    league_team_id: Mapped[int] = mapped_column(ForeignKey("league_team.id"))
    place: Mapped[int | None] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    points_for: Mapped[int | None] = mapped_column(Integer)
    points_against: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChangeEvent(Base):
    """Изменение, обнаруженное синхронизацией (перенос, смена времени, дозаявка)."""

    __tablename__ = "change_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("match.id"))
    league_team_id: Mapped[int | None] = mapped_column(ForeignKey("league_team.id"))
    payload: Mapped[str | None] = mapped_column(Text)  # JSON: было/стало
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------- Данные команды сервиса (tenant_id) ----------


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class TeamEntry(Base):
    """Выступление: наша команда в конкретной лиге и турнире."""

    __tablename__ = "team_entry"
    __table_args__ = (UniqueConstraint("tenant_id", "league_team_id", "tournament_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    league_team_id: Mapped[int] = mapped_column(ForeignKey("league_team.id"))
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournament.id"))
    source_url: Mapped[str] = mapped_column(String(255))


class Chat(Base):
    __tablename__ = "chat"
    __table_args__ = (UniqueConstraint("channel", "peer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    channel: Mapped[str] = mapped_column(String(20))
    peer_id: Mapped[str] = mapped_column(String(50))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Player(Base):
    __tablename__ = "player"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="player")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MessengerAccount(Base):
    __tablename__ = "messenger_account"
    __table_args__ = (UniqueConstraint("tenant_id", "channel", "external_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id"))
    channel: Mapped[str] = mapped_column(String(20))
    external_user_id: Mapped[str] = mapped_column(String(50))
    dm_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime)


class RosterLink(Base):
    """Связь игрока с заявкой на сезон выступления. season_roster_entry_id пусто при not_in_roster."""

    __tablename__ = "roster_link"
    __table_args__ = (UniqueConstraint("tenant_id", "team_entry_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    team_entry_id: Mapped[int] = mapped_column(ForeignKey("team_entry.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id"))
    season_roster_entry_id: Mapped[int | None] = mapped_column(ForeignKey("season_roster_entry.id"))
    status: Mapped[str] = mapped_column(String(20))
    confirmed_by_player_id: Mapped[int | None] = mapped_column(ForeignKey("player.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    number_override: Mapped[str | None] = mapped_column(String(10))


class PermissionGrant(Base):
    __tablename__ = "permission_grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id"))
    permission: Mapped[str] = mapped_column(String(30))
    granted_by_player_id: Mapped[int] = mapped_column(ForeignKey("player.id"))
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)  # пусто — до отмены
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    suppress_digests_for_grantor: Mapped[bool] = mapped_column(Boolean, default=False)


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("tenant_id", "match_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    match_id: Mapped[int] = mapped_column(ForeignKey("match.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id"))
    vote: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ApplicationEntry(Base):
    """Правки заявки на игру поверх результатов опроса."""

    __tablename__ = "application_entry"
    __table_args__ = (UniqueConstraint("tenant_id", "match_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    match_id: Mapped[int] = mapped_column(ForeignKey("match.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("player.id"))
    source: Mapped[str] = mapped_column(String(20))
    removed_by_captain: Mapped[bool] = mapped_column(Boolean, default=False)
    changed_by_player_id: Mapped[int | None] = mapped_column(ForeignKey("player.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PollMessage(Base):
    """Анонс-голосование в беседе. superseded_by — если анонс пришлось переопубликовать."""

    __tablename__ = "poll_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    match_id: Mapped[int] = mapped_column(ForeignKey("match.id"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id"))
    message_ref: Mapped[str] = mapped_column(String(50))
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_edited_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("poll_message.id"))


class NotificationLog(Base):
    """Журнал отправленного: уникальный (tenant, kind, dedup_key) делает отправку идемпотентной."""

    __tablename__ = "notification_log"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "dedup_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    kind: Mapped[str] = mapped_column(String(40))
    dedup_key: Mapped[str] = mapped_column(String(200))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("match.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("player.id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
