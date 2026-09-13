"""Presentation geometry derived from the detector's saved anchors."""

from copy import deepcopy

import numpy as np

from patterns.base_pattern import ANN_PATTERN, BasePattern, ann_marker, ann_path, ann_segment


PART_LABELS = {
    "H1": "First top (H1)", "H2": "Second top (H2)",
    "L1": "First bottom (L1)", "L2": "Second bottom (L2)",
    "LS": "Left shoulder", "HEAD": "Head", "RS": "Right shoulder",
    "LN": "Left neckline trough", "RN": "Right neckline trough",
    "SH1": "First swing high", "SH2": "Second swing high",
    "SL1": "First swing low", "SL2": "Second swing low",
    "valley": "Valley", "peak": "Peak", "neckline": "Neckline",
    "bottom": "Bottom", "top": "Top", "start": "Channel start",
    "pole start": "Pole start", "pole high": "Pole end", "pole": "Pole end",
    "flag low": "Flag low", "flag high": "Flag high",
    "breakout": "Breakout", "retest": "Retest", "entry": "Entry",
    "support": "Support", "resistance": "Resistance",
}


def pattern_annotations(annotations: list[dict], df=None, pattern: str | None = None) -> list[dict]:
    """Upgrade saved marker-only reversals without re-running detection.

    Historical ledgers retain the exact pivot dates/prices even when they
    predate outlines. Never search price history for a replacement setup.
    """
    result = deepcopy(annotations)
    markers = {a.get("label"): a for a in result if a.get("type") == "marker"}
    if df is not None and pattern:
        result.extend(_historical_geometry(df, pattern, result, markers))
    outlines = (
        ("Double top", ("H1", "valley", "H2", "entry")),
        ("Double bottom", ("L1", "neckline", "L2", "entry")),
        ("Head and shoulders", ("LS", "LN", "HEAD", "RN", "RS", "entry")),
        ("Breakout and retest", ("breakout", "retest", "entry")),
    )
    for name, parts in outlines:
        if all(key in markers for key in parts) and not any(a.get("outline") == name for a in result):
            # Same-bar events have one price per date; retain the latest event.
            points = {markers[key]["date"]: markers[key]["price"] for key in parts}
            path = ann_path(sorted(points.items()))
            path["outline"] = name
            result.append(path)
    for ann in result:
        label = ann.get("label", "")
        if label not in {"stop", "target"}:
            ann["color"] = ANN_PATTERN
        if ann.get("type") == "segment" and not label:
            if "SH1" in markers or "SL1" in markers:
                first = markers.get("SH1") or markers["SL1"]
                main_rail = ann.get("start_date") == first["date"]
                upper = main_rail == ("SH1" in markers)
                label = "Upper channel" if upper else "Lower channel"
            elif "LS" in markers:
                # The detector uses the mean neck close, a horizontal threshold.
                ann["start_price"] = ann["end_price"]
                label = "Neckline"
            elif "pole start" in markers:
                label = "Pole"
        ann["label"] = PART_LABELS.get(label, label)
    return result


def _historical_geometry(df, pattern, annotations, markers):
    """Use saved formation windows to fill geometry absent from old exports."""
    dates = {BasePattern.bar_date(df, i): i for i in range(len(df))}
    close = df["close" if "close" in df else "Close"]
    for direction in ("bottom", "top"):
        if pattern.endswith(f"rounding_{direction}") and direction in markers:
            if any(a.get("type") == "path" for a in annotations):
                return []
            center = dates.get(markers[direction].get("date"))
            if center is not None and center >= 60 and center + 60 < len(df):
                return [_rounding_curve(df, close, center, direction, 60)]
    if pattern.endswith("flag_pattern") and "pole high" in markers and "flag low" in markers:
        if any(a.get("label") == "Flag resistance" for a in annotations):
            return []
        pole_end = dates.get(markers["pole high"].get("date"))
        # Older flag annotations stored the flag's end date on its low marker.
        end = dates.get(markers["flag low"].get("date"))
        resistance = next((a.get("price") for a in annotations if a.get("label") == "flag high"), None)
        if pole_end is not None and end is not None and end > pole_end + 1 and resistance is not None:
            low = df["low" if "low" in df else "Low"].iloc[pole_end + 1:end + 1]
            low_bar = pole_end + 1 + int(np.argmin(low.to_numpy(dtype=float)))
            markers["flag low"]["date"] = BasePattern.bar_date(df, low_bar)
            return [
                ann_segment(BasePattern.bar_date(df, pole_end + 1), BasePattern.bar_date(df, end),
                            price, price, ANN_PATTERN, label=label)
                for price, label in ((resistance, "Flag resistance"), (markers["flag low"]["price"], "Flag support"))
            ]
    if not pattern.endswith("pennant") or "pole" not in markers or "entry" not in markers:
        return []
    if any("pennant" in a.get("label", "").lower() for a in annotations):
        return []
    pole_end, entry = dates.get(markers["pole"].get("date")), dates.get(markers["entry"].get("date"))
    if pole_end is None or entry is None or entry - pole_end < 3:
        return []
    result = []
    start, end = pole_end + 1, entry - 1
    for column, label in (("high", "Upper pennant fit"), ("low", "Lower pennant fit")):
        values = df[column if column in df else column.title()].iloc[start:end + 1].to_numpy(dtype=float)
        slope, intercept = np.polyfit(np.arange(len(values)), values, 1)
        result.append(ann_segment(BasePattern.bar_date(df, start), BasePattern.bar_date(df, end),
                                  float(intercept), float(intercept + slope * (end - start)), ANN_PATTERN, label=label))
    return result


def _rounding_curve(df, close, center, direction, radius):
    start, end = center - radius, center + radius
    x = np.arange(-radius, radius + 1, dtype=float)
    coefficients = np.polyfit(x, close.iloc[start:end + 1].to_numpy(dtype=float), 2)
    fitted = np.polyval(coefficients, x)
    return ann_path([
        (BasePattern.bar_date(df, start + i), price)
        for i, price in enumerate(fitted)
    ], f"Rounding {direction} fit")


def rounding_annotations(df, setup, direction: str, radius: int = 60) -> list[dict]:
    """Sample the same quadratic/window validated by parabolic_fit()."""
    return [
        _rounding_curve(df, df["close"], setup.center, direction, radius),
        ann_marker(BasePattern.bar_date(df, setup.start), setup.neckline,
                   "Left rim", ANN_PATTERN, "o", "above" if direction == "bottom" else "below"),
    ]
