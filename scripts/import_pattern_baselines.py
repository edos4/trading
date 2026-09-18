"""Explicit byte-exact bootstrap; requires separately applied PostgreSQL migrations."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from core.pattern_bootstrap import Bootstrap
    from core.pattern_edit_store import EditStore
    report = Bootstrap(EditStore()).run()
    print(json.dumps({k: report[k] for k in ('batch_id', 'report_id', 'snapshot_sha256', 'status')}, indent=2))


if __name__ == '__main__':
    main()
