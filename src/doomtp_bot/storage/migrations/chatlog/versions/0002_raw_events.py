"""Keep each event as Twitch sent it, beside the columns that are queried (ADR-0024 item 1).

`raw` holds the source: the EventSub `event` object (`eventsub`), `{"line": ...}` for an IRC line
(`irc`), or, for a row logged before this revision, an EventSub-shaped object rebuilt from its columns
(`legacy`). `enrichment` holds what a backfilled line lacks and was looked up (item 4); `emotes` caches
those lookups; `backfill_runs.provider` says which history service filled a gap (item 5).

Additive: the columns that moved into `raw` stay, and are still written, until one release after the
readers use `raw` (item 8). `raw` stays nullable until then too, for rows written straight into the table.

`chatlog.legacy` rebuilds the same objects for an event that arrives with neither source; the two must
agree (`tests/test_raw_events.py`).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("messages", "chat_notifications", "mod_events")

# A message's columns as a channel.chat.message event. Fragments were kept in our own shape
# (`twitch.mapping._fragment`); they go back to Twitch's. Nulls are left out, as nothing was known there.
LEGACY_MESSAGE = """
UPDATE messages SET raw_format = 'legacy', raw = jsonb_strip_nulls(jsonb_build_object(
    'broadcaster_user_id', channel_id,
    'chatter_user_id', user_id,
    'chatter_user_login', user_login,
    'chatter_user_name', display_name,
    'message_id', message_id,
    'message', jsonb_build_object(
        'text', text,
        'fragments', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'type', f->'type',
                'text', f->'text',
                'cheermote', CASE WHEN f ? 'cheermote' THEN jsonb_build_object(
                    'prefix', f->'cheermote'->'prefix', 'bits', f->'cheermote'->'bits') END,
                'emote', CASE WHEN f ? 'emote_id' THEN jsonb_build_object('id', f->'emote_id') END,
                'mention', CASE WHEN f ? 'mention' THEN jsonb_build_object(
                    'user_id', f->'mention'->'id', 'user_login', f->'mention'->'login') END
            ) ORDER BY n)
            FROM jsonb_array_elements(COALESCE(fragments, '[]')::jsonb) WITH ORDINALITY AS t(f, n)
        ), '[]'::jsonb)),
    'message_type', message_type,
    'badges', COALESCE(badges, '[]')::jsonb,
    'cheer', CASE WHEN bits > 0 THEN jsonb_build_object('bits', bits) END,
    'reply', CASE WHEN reply_parent_id IS NOT NULL THEN jsonb_build_object('parent_message_id', reply_parent_id) END,
    'channel_points_custom_reward_id', reward_id,
    'source_broadcaster_user_id', source_channel_id
)) WHERE raw IS NULL
"""

# A notification's payload was our own summary, different per type: it is kept whole under `legacy`.
LEGACY_NOTIFICATION = """
UPDATE chat_notifications SET raw_format = 'legacy', raw = jsonb_strip_nulls(jsonb_build_object(
    'broadcaster_user_id', channel_id,
    'chatter_user_id', user_id,
    'notice_type', type
)) || jsonb_build_object('legacy', payload::jsonb)
"""

LEGACY_MOD_EVENT = """
UPDATE mod_events SET raw_format = 'legacy', raw = jsonb_strip_nulls(jsonb_build_object(
    'broadcaster_user_id', channel_id,
    'message_id', message_id,
    'target_user_id', target_user_id,
    'moderator_user_id', moderator_user_id,
    'duration_s', duration_s,
    'reason', reason
))
"""


def upgrade() -> None:
    # messages.raw was the backfilled IRC line, as text: it becomes the `irc` source in place.
    op.execute(
        "ALTER TABLE messages ALTER COLUMN raw TYPE jsonb"
        " USING CASE WHEN raw IS NULL THEN NULL ELSE jsonb_build_object('line', raw) END"
    )
    op.execute("ALTER TABLE chat_notifications ADD COLUMN raw jsonb")
    op.execute("ALTER TABLE mod_events ADD COLUMN raw jsonb")
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN raw_format text"
            " CHECK (raw_format IN ('eventsub', 'irc', 'legacy')), ADD COLUMN enrichment jsonb"
        )
    op.execute("UPDATE messages SET raw_format = 'irc' WHERE raw IS NOT NULL")
    op.execute(LEGACY_MESSAGE)
    op.execute(LEGACY_NOTIFICATION)
    op.execute(LEGACY_MOD_EVENT)
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_raw_has_format"
            " CHECK ((raw IS NULL) = (raw_format IS NULL))"
        )

    op.execute("ALTER TABLE backfill_runs ADD COLUMN provider text NOT NULL DEFAULT 'recent-messages'")
    # What an emote id stands for, looked up once (ADR-0024 §3). `gone`: Twitch no longer knows it.
    op.execute(
        """
        CREATE TABLE emotes (
            emote_id     text PRIMARY KEY,
            set_id       text,
            owner_id     text,
            formats      jsonb,                 -- ["static"] or ["static", "animated"]
            source       text NOT NULL CHECK (source IN ('log', 'helix', 'cdn', 'gone')),
            looked_up_at bigint NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE emotes")
    op.execute("ALTER TABLE backfill_runs DROP COLUMN provider")
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT {table}_raw_has_format,"
            " DROP COLUMN raw_format, DROP COLUMN enrichment"
        )
    op.execute("ALTER TABLE mod_events DROP COLUMN raw")
    op.execute("ALTER TABLE chat_notifications DROP COLUMN raw")
    # Only an IRC line has a place in the old column; EventSub and legacy objects were never kept there.
    op.execute("ALTER TABLE messages ALTER COLUMN raw TYPE text USING raw->>'line'")
