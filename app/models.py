import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(enum.StrEnum):
    admin = "admin"
    staff = "staff"
    student = "student"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'staff', 'student')", name="ck_users_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    entra_oid: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only : le code applicatif ne fait que des INSERT (triggers SQL en garde-fou)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    action: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor: Mapped[str | None] = mapped_column(String(320))
    ip: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    group: Mapped[str | None] = mapped_column(String(100))
    notify_service: Mapped[str | None] = mapped_column(String(255))
    ma_player_id: Mapped[str | None] = mapped_column(String(255))


class Preset(Base):
    __tablename__ = "presets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Priority(enum.IntEnum):
    """Plus petit = plus prioritaire. `alert` est réservé aux incidents (phase 5)."""

    alert = 0
    admin = 1
    staff = 2


class AnnouncementStatus(enum.StrEnum):
    queued = "queued"
    sending = "sending"
    done = "done"
    failed = "failed"


class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (
        CheckConstraint("priority IN (0, 1, 2)", name="ck_announcements_priority"),
        CheckConstraint(
            "status IN ('queued', 'sending', 'done', 'failed')", name="ck_announcements_status"
        ),
        Index("ix_announcements_status_created", "status", "created_at"),
        Index("ix_announcements_author_email", "author_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    author_email: Mapped[str | None] = mapped_column(String(320))
    text: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    results: Mapped[dict[str, Any] | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    voice: Mapped[str | None] = mapped_column(String(32))
    lang: Mapped[str | None] = mapped_column(String(16))
    ssml: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnounceDefaults(Base):
    """Ligne unique (id = 1) : voix/langue par défaut des annonces, modifiable par l'admin."""

    __tablename__ = "announce_defaults"
    __table_args__ = (CheckConstraint("id = 1", name="ck_announce_defaults_single"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    voice: Mapped[str | None] = mapped_column(String(32))
    lang: Mapped[str | None] = mapped_column(String(16))


class IncidentStatus(enum.StrEnum):
    draft = "draft"
    sent = "sent"
    failed = "failed"
    closed = "closed"


class IncidentType(enum.StrEnum):
    evacuation = "evacuation"
    confinement = "confinement"
    intrusion = "intrusion"
    incendie = "incendie"
    meteo = "meteo"
    autre = "autre"


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'sent', 'failed', 'closed')", name="ck_incidents_status"
        ),
        Index("ix_incidents_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_email: Mapped[str | None] = mapped_column(String(320))
    closed_by_email: Mapped[str | None] = mapped_column(String(320))
    announcement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("announcements.id", ondelete="SET NULL")
    )
    delivery: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Relances automatiques : surcharges par incident (NULL = config globale, 0 = désactivé)
    auto_remind_interval: Mapped[int | None] = mapped_column(Integer)
    auto_remind_max: Mapped[int | None] = mapped_column(Integer)
    auto_remind_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    auto_remind_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    auto_remind_last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResponseStatus(enum.StrEnum):
    safe_campus = "safe_campus"
    safe_offsite = "safe_offsite"
    danger = "danger"


class Response(Base):
    """Réponse d'un étudiant à un incident (une seule par incident+utilisateur, mise à jour)."""

    __tablename__ = "responses"
    __table_args__ = (
        UniqueConstraint("incident_id", "user_aad_id", name="uq_responses_incident_user"),
        CheckConstraint(
            "status IN ('safe_campus', 'safe_offsite', 'danger')", name="ck_responses_status"
        ),
        Index("ix_responses_received_at", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE")
    )
    user_aad_id: Mapped[str] = mapped_column(String(64))
    user_email: Mapped[str | None] = mapped_column(String(320))
    user_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AlertTemplate(Base):
    """Modèle d'alerte : un par type d'incident (texte diffusé sur enceintes et Teams)."""

    __tablename__ = "alert_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(7))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


speaker_group_members = Table(
    "speaker_group_members",
    Base.metadata,
    Column(
        "group_id",
        UUID(as_uuid=True),
        ForeignKey("speaker_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "speaker_id",
        UUID(as_uuid=True),
        ForeignKey("speakers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

scene_speakers = Table(
    "scene_speakers",
    Base.metadata,
    Column(
        "scene_id",
        UUID(as_uuid=True),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "speaker_id",
        UUID(as_uuid=True),
        ForeignKey("speakers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class SpeakerGroup(Base):
    __tablename__ = "speaker_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    members: Mapped[list[Speaker]] = relationship(
        secondary=speaker_group_members, lazy="selectin", order_by=Speaker.name
    )


class Scene(Base):
    __tablename__ = "scenes"
    __table_args__ = (
        CheckConstraint(
            "media_type IN ('playlist', 'album', 'track', 'radio')", name="ck_scenes_media_type"
        ),
        CheckConstraint("mode IN ('play', 'replace')", name="ck_scenes_mode"),
        CheckConstraint(
            "volume IS NULL OR (volume >= 0 AND volume <= 100)", name="ck_scenes_volume"
        ),
        CheckConstraint(
            "repeat IS NULL OR repeat IN ('off', 'one', 'all')", name="ck_scenes_repeat"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    icon: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(7))
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speaker_groups.id", ondelete="RESTRICT")
    )
    media_uri: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16), default="play", server_default="play")
    volume: Mapped[int | None] = mapped_column(Integer)
    shuffle: Mapped[bool | None] = mapped_column(Boolean)
    repeat: Mapped[str | None] = mapped_column(String(8))
    preset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("presets.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    speakers: Mapped[list[Speaker]] = relationship(
        secondary=scene_speakers, lazy="selectin", order_by=Speaker.name
    )


class ScheduleAction(enum.StrEnum):
    announcement = "announcement"
    scene = "scene"
    stop = "stop"


class ScheduleRunStatus(enum.StrEnum):
    running = "running"
    ok = "ok"
    skipped_closure = "skipped_closure"
    skipped_alert = "skipped_alert"
    failed = "failed"
    missed = "missed"


class ClosureKind(enum.StrEnum):
    vacances = "vacances"
    fermeture = "fermeture"
    ferie = "ferie"


class Schedule(Base):
    """Planification : récurrence hebdo (jours ISO 1-7 + HH:MM) ou date unique, dans un fuseau."""

    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint("action IN ('announcement', 'scene', 'stop')", name="ck_schedules_action"),
        CheckConstraint(
            "stop_volume IS NULL OR (stop_volume >= 0 AND stop_volume <= 100)",
            name="ck_schedules_stop_volume",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    timezone: Mapped[str] = mapped_column(
        String(64), default="Europe/Paris", server_default="Europe/Paris"
    )
    days: Mapped[list[int]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list)
    time_of_day: Mapped[str] = mapped_column(String(5))
    once_date: Mapped[date | None] = mapped_column(Date)
    action: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    preset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("presets.id", ondelete="SET NULL")
    )
    voice: Mapped[str | None] = mapped_column(String(32))
    lang: Mapped[str | None] = mapped_column(String(16))
    group_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list
    )
    speaker_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list
    )
    scene_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scenes.id", ondelete="SET NULL")
    )
    stop_volume: Mapped[int | None] = mapped_column(Integer)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    respect_closures: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    armed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleRun(Base):
    """Journal d'exécution. `UNIQUE (schedule_id, scheduled_for)` : verrou d'idempotence."""

    __tablename__ = "schedule_runs"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for", name="uq_schedule_runs_slot"),
        CheckConstraint(
            "status IN ('running', 'ok', 'skipped_closure', 'skipped_alert', 'failed', 'missed')",
            name="ck_schedule_runs_status",
        ),
        CheckConstraint("trigger IN ('schedule', 'manual')", name="ck_schedule_runs_trigger"),
        Index("ix_schedule_runs_scheduled_for", "scheduled_for"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="CASCADE")
    )
    schedule_name: Mapped[str] = mapped_column(String(100), default="")
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", server_default="schedule")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    detail: Mapped[str | None] = mapped_column(String(255))


class Closure(Base):
    """Plage de fermeture (dates incluses, calendrier local) saisie par l'admin."""

    __tablename__ = "closures"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_closures_range"),
        CheckConstraint("kind IN ('vacances', 'fermeture', 'ferie')", name="ck_closures_kind"),
        Index("ix_closures_range", "start_date", "end_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class PlanningSettings(Base):
    """Ligne unique (id = 1) : interrupteur « tenir compte des jours fériés »."""

    __tablename__ = "planning_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_planning_settings_single"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    observe_holidays: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class PushSubscription(Base):
    """Abonnement Web Push d'un navigateur (admin/staff) ; l'endpoint est unique."""

    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    endpoint: Mapped[str] = mapped_column(String(2048), unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentNotification(Base):
    """Déclencheur de notification admin déjà traité : un (incident, clé) ne notifie qu'une fois."""

    __tablename__ = "incident_notifications"
    __table_args__ = (UniqueConstraint("incident_id", "key", name="uq_incident_notifications_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Student(Base):
    """Annuaire étudiants (import CSV annuel). Séparé de `users` : ils ne se connectent pas."""

    __tablename__ = "students"
    __table_args__ = (CheckConstraint("email = lower(email)", name="ck_students_email_lower"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    last_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    first_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    promo: Mapped[str] = mapped_column(String(100), default="", server_default="", index=True)
    cursus: Mapped[str] = mapped_column(String(100), default="", server_default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
