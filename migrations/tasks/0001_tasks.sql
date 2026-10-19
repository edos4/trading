-- Tasks board schema. Applied only by scripts/setup_tasks_db.py with a
-- schema-qualified search_path; the runner owns schema_migrations.
CREATE TABLE task_lists (
    id         TEXT PRIMARY KEY,           -- uuid4 hex
    title      TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    position   DOUBLE PRECISION NOT NULL,  -- fractional rank, ascending
    archived   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task_cards (
    id          TEXT PRIMARY KEY,          -- uuid4 hex
    list_id     TEXT NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
    title       TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 20000),
    author      TEXT NOT NULL,             -- logged-in username at create time
    position    DOUBLE PRECISION NOT NULL, -- fractional rank within list
    archived    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX idx_task_cards_list ON task_cards(list_id, position);

CREATE TABLE task_subtasks (
    id         TEXT PRIMARY KEY,
    card_id    TEXT NOT NULL REFERENCES task_cards(id) ON DELETE CASCADE,
    text       TEXT NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 500),
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    position   DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX idx_task_subtasks_card ON task_subtasks(card_id, position);

CREATE TABLE task_comments (
    id         TEXT PRIMARY KEY,
    card_id    TEXT NOT NULL REFERENCES task_cards(id) ON DELETE CASCADE,
    parent_id  TEXT REFERENCES task_comments(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,              -- logged-in username at create time
    body       TEXT NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 20000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX idx_task_comments_card   ON task_comments(card_id, created_at);
CREATE INDEX idx_task_comments_parent ON task_comments(parent_id);

-- Replies are one level deep and must share their parent's card.
CREATE FUNCTION task_comment_reply_depth() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM task_comments
                   WHERE id = NEW.parent_id AND parent_id IS NOT NULL) THEN
            RAISE EXCEPTION 'replies may only be one level deep' USING ERRCODE='23514';
        END IF;
        IF (SELECT card_id FROM task_comments WHERE id = NEW.parent_id) <> NEW.card_id THEN
            RAISE EXCEPTION 'reply parent belongs to another card' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER task_comment_reply_depth BEFORE INSERT OR UPDATE ON task_comments
    FOR EACH ROW EXECUTE FUNCTION task_comment_reply_depth();
