"""Strict JSON values for browser payloads and exported trade records."""

import math


def json_safe(value):
    """Represent undefined metrics, including infinite profit factor, as null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
