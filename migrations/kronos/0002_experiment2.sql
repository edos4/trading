-- Experiment 2: one row per symbol x anchor x forecast horizon (D1 and D5).
--
-- Both models share a row so the tab can show base and fine-tuned closes side
-- by side, exactly like the experiment spreadsheet. Accuracy is stored once the
-- corresponding bar settles:
--   accuracy_pct = 100 * (1 - abs(predicted_close - actual_close) / actual_close)

CREATE TABLE kronos_experiment2 (
    symbol                 TEXT             NOT NULL,
    anchor_date            DATE             NOT NULL,
    anchor_close           DOUBLE PRECISION NOT NULL,
    day_ahead              SMALLINT         NOT NULL CHECK (day_ahead BETWEEN 1 AND 60),
    forecast_date          DATE             NOT NULL,
    base_close             DOUBLE PRECISION,
    finetuned_close        DOUBLE PRECISION,
    actual_date            DATE,
    actual_close           DOUBLE PRECISION,
    base_accuracy_pct      DOUBLE PRECISION,
    finetuned_accuracy_pct DOUBLE PRECISION,
    saved_by               TEXT             NOT NULL,
    saved_at               TIMESTAMPTZ      NOT NULL DEFAULT clock_timestamp(),
    graded_at              TIMESTAMPTZ,
    PRIMARY KEY (symbol, anchor_date, day_ahead),
    CHECK (base_close IS NOT NULL OR finetuned_close IS NOT NULL)
);

CREATE INDEX idx_kronos_experiment2_anchor
    ON kronos_experiment2 (anchor_date DESC);

CREATE INDEX idx_kronos_experiment2_pending
    ON kronos_experiment2 (symbol, anchor_date) WHERE actual_close IS NULL;
