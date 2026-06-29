#!/usr/bin/env python3
"""
migrate_json_to_sqlite.py — One-time migration of legacy maintenance_log.json
into the EPM SQLite database.

Usage:
    python migrate_json_to_sqlite.py                      # uses default paths
    python migrate_json_to_sqlite.py --db logs/epm.db --json logs/maintenance_log.json

Run once after deploying the updated gateway.  The JSON file is NOT deleted
automatically — verify the DB looks correct first, then remove it manually.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from storage import Storage


def migrate(db_path: str, json_path: str, dry_run: bool = False) -> int:
    if not os.path.exists(json_path):
        print(f"JSON file not found: {json_path}")
        return 0

    with open(json_path) as f:
        data = json.load(f)

    if not isinstance(data, dict):
        print(f"ERROR: expected a dict in {json_path}, got {type(data).__name__}")
        return 1

    print(f"Found {len(data)} maintenance record(s) in {json_path}")

    if dry_run:
        for mac, record in data.items():
            print(f"  DRY-RUN  mac={mac}  tech={record.get('technician','')}  "
                  f"type={record.get('maint_type', record.get('work_type', ''))}")
        return 0

    db = Storage(db_path)
    count = 0
    for mac, record in data.items():
        technician = record.get('technician', '')
        work_type  = record.get('maint_type', record.get('work_type', 'Routine Inspection'))
        notes_json = json.dumps(record)
        try:
            db.log_maintenance(mac, technician, work_type, notes_json)
            count += 1
            print(f"  Migrated mac={mac}  tech={technician}  type={work_type}")
        except Exception as e:
            print(f"  ERROR migrating mac={mac}: {e}")

    print(f"\nMigrated {count}/{len(data)} record(s) into {db_path}")
    print("Verify the DB looks correct, then delete the JSON file:")
    print(f"  del {json_path}" if sys.platform == 'win32' else f"  rm {json_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    base = os.path.join(os.path.dirname(__file__), 'logs')
    parser.add_argument('--db',       default=os.path.join(base, 'epm.db'),
                        help='SQLite DB path (default: logs/epm.db)')
    parser.add_argument('--json',     default=os.path.join(base, 'maintenance_log.json'),
                        help='Source JSON path (default: logs/maintenance_log.json)')
    parser.add_argument('--dry-run',  action='store_true',
                        help='Print what would be migrated without writing')
    args = parser.parse_args()
    sys.exit(migrate(args.db, args.json, dry_run=args.dry_run))


if __name__ == '__main__':
    main()
