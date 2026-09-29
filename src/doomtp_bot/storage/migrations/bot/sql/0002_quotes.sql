-- Quotes (architecture §12, the `quotes` module). Numbered per channel and never renumbered: a deleted
-- quote keeps its row and its number, so "quote 12" means the same thing in a clip from last year.
CREATE TABLE quotes (
    channel_id  text NOT NULL,
    number      integer NOT NULL CHECK (number > 0),
    text        text NOT NULL,
    game        text,                        -- what the channel was streaming when it was added, if live
    added_by    text,                        -- user id
    added_at    bigint NOT NULL,
    deleted_by  text,
    deleted_at  bigint,
    PRIMARY KEY (channel_id, number)
);
