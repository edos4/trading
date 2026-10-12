from ._pole_consolidation import detect as _detect


def detect(h, l, c, a, P, **kwargs):
    return _detect(h, l, c, a, P, "bull_pennant", **kwargs)
