import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import StockMLBackend


class LiquidUniverseFreshnessTests(unittest.TestCase):
    def test_stale_symbol_is_not_kept_by_old_liquidity(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = StockMLBackend()
            backend.db_path = Path(tmp) / "stock_system.sqlite3"
            backend.init_db()
            with backend.connect() as conn:
                conn.executemany(
                    "INSERT INTO stock_info (symbol, name, sector, market_type, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("1111", "最新股", "測試", "twse", "2026-07-09 14:00:00"),
                        ("2222", "過期股", "測試", "twse", "2026-07-09 14:00:00"),
                    ],
                )
                rows = []
                for day in range(1, 20):
                    date = f"2026-06-{day:02d}"
                    rows.extend([
                        ("1111", date, 100, 100, 100, 100, 2_000_000, "TWSE OpenAPI STOCK_DAY_ALL", f"{date} 14:00:00"),
                        ("2222", date, 100, 100, 100, 100, 2_000_000, "TWSE OpenAPI STOCK_DAY_ALL", f"{date} 14:00:00"),
                    ])
                rows.extend([
                    ("1111", "2026-07-09", 100, 100, 100, 100, 2_000_000, "TWSE OpenAPI STOCK_DAY_ALL", "2026-07-09 14:00:00"),
                    ("2222", "2026-07-08", 100, 100, 100, 100, 2_000_000, "TWSE OpenAPI STOCK_DAY_ALL", "2026-07-08 14:00:00"),
                ])
                conn.executemany(
                    """
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume, price_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            with patch.object(backend, "latest_complete_price_date", return_value="2026-07-09"):
                symbols = backend.liquid_monster_universe()

        self.assertIn("1111", symbols)
        self.assertNotIn("2222", symbols)


if __name__ == "__main__":
    unittest.main()
