"""P3 dataset fixtures for Laya evaluation (docs/laya.md section 12).

Timestamped, labeled fixtures with a versioned evaluation command. Initial
sample planning target: several hundred labeled examples across tasks and
markets — this seed file starts the corpus; extend it with real
operator-supplied financial text and causally captured candidate snapshots.
Labels for event interpretation / evidence alignment are independent from
future trade outcomes.
"""
from __future__ import annotations

FIXTURE_VERSION = "laya-fixtures-v1"

# Each fixture: task, market, language, text/context, labels, timestamps.
# Publication/availability times are part of the label: a document collected
# today without historical availability evidence is valid for current research,
# not proof it was available in an old backtest.
FIXTURES: list[dict] = [
    {
        "fixture_id": "evt-en-001",
        "task_id": "event-review-v1",
        "market": "us",
        "language": "en",
        "symbol": "EXAMPLE",
        "source_title": "EXAMPLE Corp prices secondary offering",
        "publisher": "operator-fixture",
        "publication_time": "2026-01-15T13:00:00+00:00",
        "available_at": "2026-01-15T13:05:00+00:00",
        "source_text": (
            "EXAMPLE Corp announced it priced an underwritten public offering "
            "of 10 million common shares at $20 per share, with proceeds for "
            "general corporate purposes. The offering dilutes existing holders."
        ),
        "labels": {"event_type": "financing", "directional_tone": "negative",
                   "material_risk_mentioned": "no"},
        "split": "dev",
    },
    {
        "fixture_id": "evt-en-002",
        "task_id": "event-review-v1",
        "market": "us",
        "language": "en",
        "symbol": "EXAMPLE",
        "source_title": "EXAMPLE Corp beats earnings, raises guidance",
        "publisher": "operator-fixture",
        "publication_time": "2026-02-10T12:00:00+00:00",
        "available_at": "2026-02-10T12:05:00+00:00",
        "source_text": (
            "EXAMPLE Corp reported quarterly revenue above expectations and "
            "raised full-year guidance, citing strong demand."
        ),
        "labels": {"event_type": "earnings", "directional_tone": "positive",
                   "material_risk_mentioned": "no"},
        "split": "dev",
    },
    {
        "fixture_id": "evt-mixed-003",
        "task_id": "event-review-v1",
        "market": "ph",
        "language": "en",
        "symbol": "EXAMPLE",
        "source_title": "Regulator opens inquiry into EXAMPLE subsidiary",
        "publisher": "operator-fixture",
        "publication_time": "2026-03-05T08:00:00+00:00",
        "available_at": "2026-03-05T08:10:00+00:00",
        "source_text": (
            "The regulator said it opened an inquiry into EXAMPLE's subsidiary "
            "over disclosure practices. The company said it is cooperating and "
            "that operations continue normally."
        ),
        "labels": {"event_type": "regulatory", "directional_tone": "mixed",
                   "material_risk_mentioned": "yes"},
        "split": "test",
    },
    {
        "fixture_id": "cand-001",
        "task_id": "candidate-review-v1",
        "market": "us",
        "language": "en",
        "symbol": "EXAMPLE",
        "context_text": (
            "Task: review supplied candidate evidence; do not infer missing news.\n"
            "Market: US; symbol: EXAMPLE; currency: USD; timeframe: 1d.\n"
            "Evidence cutoff: 2026-01-20T21:00:00+00:00; all bars complete through this cutoff.\n"
            "Candidate: BUY; detector: pattern_003_double_bottom; engine eligibility: passed.\n"
            "Computed facts: close_above_20d_mean=true; volume_ratio_20d=1.40; "
            "reward_risk=2.10; volatility_bucket=medium.\n"
            "External text: unavailable.\n"
        ),
        "labels": {"evidence_alignment": "mixed", "material_conflict": "no"},
        "split": "dev",
    },
]
