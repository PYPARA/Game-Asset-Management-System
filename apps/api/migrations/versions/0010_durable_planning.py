"""Durable planning turns, attempts, commands and immutable conversation batches."""
from alembic import op
from game_assets_api.models import PlanningTurn, PlanningAttempt, PlanningCommand, ConversationBatch
revision = "0010_durable_planning"
down_revision = "0009_generation_conversation_lifecycle"
branch_labels = None
depends_on = None

def upgrade():
    for model in (PlanningTurn, PlanningAttempt, PlanningCommand, ConversationBatch):
        model.__table__.create(op.get_bind(), checkfirst=True)

def downgrade():
    for model in (ConversationBatch, PlanningCommand, PlanningAttempt, PlanningTurn):
        model.__table__.drop(op.get_bind(), checkfirst=True)
