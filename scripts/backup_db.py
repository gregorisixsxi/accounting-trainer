"""Consistent copy of the SQLite database (safe while the site is running).

    python scripts/backup_db.py                 # -> backups/data-YYYYmmdd-HHMMSS.db next to the project
    python scripts/backup_db.py D:\\safe\\place   # -> into another folder

Uses SQLite's own backup API, so the copy is never half-written. On Render run it in the Shell tab and download the file
(or point it at a second persistent disk). Keep the last copies; this script prunes to the newest KEEP files.
"""
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

KEEP = 14
ROOT = Path(__file__).resolve().parent.parent
SOURCE = os.environ.get("DB_PATH") or str(ROOT / "data.db")


def main() -> int:
    if not Path(SOURCE).exists():
        print(f"No database at {SOURCE}")
        return 1
    target_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"data-{datetime.now():%Y%m%d-%H%M%S}.db"

    src = sqlite3.connect(SOURCE)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Backup written: {target} ({target.stat().st_size // 1024} KB)")

    for old in sorted(target_dir.glob("data-*.db"))[:-KEEP]:
        old.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
