"""Explicit pattern-editor migrations; reads DSN from config, never logs it."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pattern_editor_db import EditError, migrate


def main():
    try:
        applied = migrate()
    except EditError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('Applied editor migrations: ' + (', '.join(map(str, applied)) or 'already up to date'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
