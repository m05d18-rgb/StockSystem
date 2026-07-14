"""Preview or apply a verified SinoPac execution-evidence manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ml_backend import StockMLBackend  # noqa: E402


def sqlite_backup(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--db", type=Path, default=ROOT / "stock_system.sqlite3")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    backend = StockMLBackend()
    backend.db_path = args.db.resolve()
    backend.init_db()

    preview = backend.import_sinopac_execution_evidence(payload, apply=False)
    print(json.dumps(preview, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = ROOT / "backups" / f"stock_system_pre_execution_evidence_{stamp}.sqlite3"
    sqlite_backup(backend.db_path, backup_path)
    result = backend.import_sinopac_execution_evidence(payload, apply=True)
    result["backupPath"] = str(backup_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
