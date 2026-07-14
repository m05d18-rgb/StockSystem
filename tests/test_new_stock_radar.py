"""新上市股票獨立雷達回歸測試。"""
import unittest

from ml_backend import backend


def make_rows(count=60):
    rows = []
    for index in range(count):
        close = 100 + index * 0.3
        rows.append({
            "symbol": "9998",
            "date": f"2026-{5 + index // 28:02d}-{1 + index % 28:02d}",
            "open": close - 0.1,
            "high": close,
            "low": close - 0.3,
            "close": close,
            "volume": 2_000_000 if index == count - 1 else 1_200_000,
            "price_source": "TWSE OpenAPI STOCK_DAY_ALL",
        })
    return rows


class NewStockRadarTests(unittest.TestCase):
    def test_30_to_119_day_strong_stock_is_separate_observe_only_candidate(self):
        rows = make_rows(60)
        item = backend.new_stock_radar_item(
            "9998", rows, reference_date=rows[-1]["date"],
            stock_info={"9998": {"name": "測試新股", "sector": "測試產業"}},
        )
        self.assertIsNotNone(item)
        self.assertTrue(item["strictEligible"])
        self.assertTrue(item["watchOnly"])
        self.assertFalse(item["buyAllowed"])
        self.assertEqual(item["historyDays"], 60)
        self.assertEqual(item["name"], "測試新股")

    def test_120_day_stock_stays_in_main_radar_not_new_stock_radar(self):
        self.assertIsNone(backend.new_stock_radar_item("9998", make_rows(120)))

    def test_stale_new_stock_data_is_excluded(self):
        rows = make_rows(60)
        self.assertIsNone(
            backend.new_stock_radar_item("9998", rows, reference_date="2026-12-31")
        )


if __name__ == "__main__":
    unittest.main()
