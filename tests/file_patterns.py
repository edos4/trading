"""Explicit legacy file fixture loader, including intentionally skipped detectors."""
import importlib
from patterns.base_pattern import BasePattern


def file_pattern(name):
    module = importlib.import_module('patterns.' + name.removeprefix('pattern_'))
    return next(cls() for cls in vars(module).values() if isinstance(cls, type)
                and cls is not BasePattern and issubclass(cls, BasePattern)
                and cls.__module__ == module.__name__)
