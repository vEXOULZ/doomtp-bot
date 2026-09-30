"""`raw` is the record: the columns that moved into it are dropped, and it is required (ADR-0024 item 8).

Every reader goes through `chatlog/events.py` since item 3, so nothing reads these columns any more.
They are dropped in the same release, not one later: ADR-0024 §1 sets that rule aside while the log
holds nothing that must be kept. The old bot's writes during the update fail and are lost.

Downgrade puts the columns back and fills them from `raw` where it is EventSub-shaped (`eventsub`,
`legacy`). A backfilled IRC line is left with what its columns can hold without parsing it: its text
stays, and the rest is empty.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

MOVED = {
    "messages": (
        "display_name",
        "message_type",
        "badges",
        "fragments",
        "bits",
        "reply_parent_id",
        "reward_id",
        "source_channel_id",
    ),
    "chat_notifications": ("payload",),
    "mod_events": ("duration_s", "reason"),
}


def upgrade() -> None:
    for table, columns in MOVED.items():
        drops = ", ".join(f"DROP COLUMN {column}" for column in columns)
        op.execute(
            f"ALTER TABLE {table} {drops}, ALTER COLUMN raw SET NOT NULL, ALTER COLUMN raw_format SET NOT NULL"
        )


def downgrade() -> None:
    for table in MOVED:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN raw DROP NOT NULL, ALTER COLUMN raw_format DROP NOT NULL"
        )
    op.execute(
        "ALTER TABLE messages ADD COLUMN display_name text, ADD COLUMN message_type text,"
        " ADD COLUMN badges text, ADD COLUMN fragments text, ADD COLUMN bits bigint NOT NULL DEFAULT 0,"
        " ADD COLUMN reply_parent_id text, ADD COLUMN reward_id text, ADD COLUMN source_channel_id text"
    )
    # Fragments go back as Twitch's, not the old own shape: close enough for a revision being left.
    op.execute(
        """
        UPDATE messages SET
            display_name = raw->>'chatter_user_name',
            message_type = raw->>'message_type',
            badges = (raw->'badges')::text,
            fragments = (raw->'message'->'fragments')::text,
            bits = COALESCE((raw->'cheer'->>'bits')::bigint, 0),
            reply_parent_id = raw->'reply'->>'parent_message_id',
            reward_id = raw->>'channel_points_custom_reward_id',
            source_channel_id = raw->>'source_broadcaster_user_id'
        WHERE raw_format <> 'irc'
        """
    )
    op.execute("ALTER TABLE chat_notifications ADD COLUMN payload text")
    op.execute("UPDATE chat_notifications SET payload = COALESCE(raw->'legacy', raw)::text")
    op.execute("ALTER TABLE chat_notifications ALTER COLUMN payload SET NOT NULL")
    op.execute("ALTER TABLE mod_events ADD COLUMN duration_s integer, ADD COLUMN reason text")
    op.execute(
        "UPDATE mod_events SET duration_s = (raw->>'duration_s')::integer, reason = raw->>'reason'"
        " WHERE raw_format = 'legacy'"
    )
