#!/usr/bin/env python3
"""Audit or source-verify and repair five years of US/PH closed daily bars."""
from __future__ import annotations
import argparse
from datetime import date
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.history_maintenance import audit_database,repair_database,closed_sessions,STATUS_PATH,repair_secondary_us,deduplicate_identical_sessions,apply_authoritative_corrections
from core.kronos_weekly import exclusive_lock,write_json


def main(argv: list[str] | None=None) -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--start',type=date.fromisoformat,default=date(2021,10,31))
    ap.add_argument('--repair',action='store_true')
    ap.add_argument('--resume',action='store_true',help='Keep verified symbols and retry unfinished requests')
    ap.add_argument('--corrections',type=Path,help='Apply reviewed exchange-report bar corrections')
    ap.add_argument('--secondary-us',action='store_true',help='Cross-check unresolved primary histories using configured SIP data')
    ap.add_argument('--deduplicate',action='store_true',help='Remove identical valid daily-session copies')
    ap.add_argument('--only-issues',action='store_true')
    ap.add_argument('--workers',type=int,default=3)
    ap.add_argument('--report',type=Path,default=STATUS_PATH)
    args=ap.parse_args(argv)
    if not 1<=args.workers<=8: ap.error('workers must be between 1 and 8')
    with exclusive_lock(ROOT/'logs/update-db.lock',timeout=3600):
        if args.corrections:
            corrections=apply_authoritative_corrections(json.loads(args.corrections.read_text()),closed_sessions())
            write_json(args.report.with_name('daily-bars-exchange-corrections.json'),corrections)
        if args.secondary_us:
            primary=json.loads(args.report.read_text())
            if not primary.get('finished_at'):
                raise RuntimeError('Primary repair is incomplete; finish it before the secondary pass')
            result=repair_secondary_us(primary,args.report.with_name('daily-bars-secondary.json'))
            print(json.dumps(result['totals']),flush=True)
        ends=closed_sessions()
        print('Auditing through',json.dumps({m:d.isoformat() for m,d in ends.items()}),flush=True)
        audit=audit_database(args.start,ends)
        write_json(args.report.with_name('daily-bars-audit.json'),audit)
        print(json.dumps(audit['summary'],indent=2),flush=True)
        if args.deduplicate:
            result=deduplicate_identical_sessions(audit)
            write_json(args.report.with_name('daily-bars-deduplication.json'),result)
            audit=audit_database(args.start,ends)
            write_json(args.report.with_name('daily-bars-final-audit.json'),audit)
            print(json.dumps({'deduplication':result['removed_rows'],'final_audit':audit['summary']},indent=2),flush=True)
        if args.repair:
            result=repair_database(args.start,ends,only_issues=args.only_issues,workers=args.workers,
                                   report_path=args.report,audit=audit,resume=args.resume)
            print(json.dumps({'totals':result['totals'],'statuses':result['statuses'],'audit_after':result['audit_after']},indent=2),flush=True)
            return int(bool(result['statuses'].get('source_unverified') or result['statuses'].get('provider_has_invalid_bars')
                            or any(m['invalid_rows'] for m in result['audit_after'].values())))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
