-- Kronos daily forecasts and their graded outcomes.
--
-- The migration runner owns the schema, schema_migrations, and search_path;
-- this file contains DDL only. One row per symbol x model x forecast day.
-- Re-saving the same anchor date upserts the prediction and clears grading so
-- the row is graded again against the settled bars.

CREATE TABLE kronos_predictions (
    symbol           TEXT             NOT NULL,
    anchor_date      DATE             NOT NULL,
    anchor_close     DOUBLE PRECISION NOT NULL,
    model            TEXT             NOT NULL CHECK (model IN ('base', 'finetuned')),
    day_ahead        SMALLINT         NOT NULL CHECK (day_ahead BETWEEN 1 AND 60),
    forecast_date    DATE             NOT NULL,
    predicted_close  DOUBLE PRECISION NOT NULL,
    saved_by         TEXT             NOT NULL,
    saved_at         TIMESTAMPTZ      NOT NULL DEFAULT clock_timestamp(),
    actual_date      DATE,
    actual_close     DOUBLE PRECISION,
    correct          BOOLEAN,
    graded_at        TIMESTAMPTZ,
    PRIMARY KEY (symbol, anchor_date, model, day_ahead)
);

CREATE INDEX idx_kronos_predictions_anchor
    ON kronos_predictions (anchor_date DESC);

CREATE INDEX idx_kronos_predictions_pending
    ON kronos_predictions (symbol, anchor_date) WHERE correct IS NULL;
