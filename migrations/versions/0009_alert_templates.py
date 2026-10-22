"""alert_templates (modèles d'alerte, un par type d'incident)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# (type, libellé, titre, message, icône)
DEFAULTS = [
    (
        "evacuation",
        "Évacuation",
        "Évacuation du bâtiment",
        "Évacuation immédiate du bâtiment. Quittez les locaux calmement par la sortie "
        "la plus proche et rejoignez le point de rassemblement.",
        "directions_run",
    ),
    (
        "confinement",
        "Confinement",
        "Mise à l'abri et confinement",
        "Le plan de Mise à l'abri et de confinement est déclenché. Restez dans votre salle, "
        "verrouillez la porte, éloignez-vous des fenêtres et gardez le silence. "
        "Attendez les instructions.",
        "lock",
    ),
    (
        "intrusion",
        "Intrusion",
        "Alerte intrusion",
        "Alerte intrusion. Enfermez-vous, barricadez la porte, éteignez les lumières et "
        "mettez vos téléphones en silencieux. Attendez les consignes des secours.",
        "report",
    ),
    (
        "incendie",
        "Incendie",
        "Alerte incendie",
        "Alerte incendie. Évacuez le bâtiment immédiatement sans reprendre vos affaires "
        "et rejoignez le point de rassemblement.",
        "local_fire_department",
    ),
    (
        "meteo",
        "Météo",
        "Alerte météorologique",
        "Alerte météorologique. Restez à l'intérieur du bâtiment, éloignez-vous des "
        "fenêtres et attendez les instructions.",
        "thunderstorm",
    ),
    (
        "autre",
        "Autre",
        "Alerte de sécurité",
        "Alerte de sécurité. Merci de rester attentifs et de suivre les consignes "
        "communiquées par le personnel.",
        "warning",
    ),
]


def upgrade() -> None:
    t = op.create_table(
        "alert_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("icon", sa.String(64)),
        sa.Column("color", sa.String(7)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.bulk_insert(
        t,
        [
            {
                "id": uuid.uuid4(),
                "type": ty,
                "label": label,
                "title": title,
                "message": msg,
                "icon": icon,
                "is_active": True,
            }
            for ty, label, title, msg, icon in DEFAULTS
        ],
    )


def downgrade() -> None:
    op.drop_table("alert_templates")
