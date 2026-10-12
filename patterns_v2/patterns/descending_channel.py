from ._triangle_base import detect as _detect_all

PATTERN = "descending_channel"


def detect(h, l, c, a, P, **kwargs):
    return _detect_all(h, l, c, a, P, kinds=(PATTERN,), **kwargs)
