import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sinopac_backend import (
    normalize_shioaji_snapshot_time,
    sinopac_backend,
)


class _Stocks:
    def get(self, code):
        return code

    def __getitem__(self, code):
        return code


class _Contracts:
    Stocks = _Stocks()


class _SnapshotApi:
    Contracts = _Contracts()

    def snapshots(self, contracts):
        return [{
            "code": contracts[0],
            "close": 2330.0,
            "reference": 2300.0,
            "ts": 1783941108520905000,
        }]


class _BatchSnapshotApi:
    Contracts = _Contracts()

    def __init__(self, fail_batch=None):
        self.batch_sizes = []
        self.fail_batch = fail_batch

    def snapshots(self, contracts):
        batch_number = len(self.batch_sizes) + 1
        self.batch_sizes.append(len(contracts))
        if self.fail_batch == batch_number:
            raise RuntimeError("synthetic batch failure")
        return [{
            "code": contract,
            "close": 100.0,
            "reference": 99.0,
            "total_volume": 1000.0,
            "ts": 1783941108520905000,
        } for contract in contracts]


class _ScannerApi:
    def __init__(self):
        self.calls = []

    def scanners(self, scanner_type, ascending=False, count=200):
        name = getattr(scanner_type, "name", str(scanner_type))
        self.calls.append({
            "scannerType": name,
            "ascending": ascending,
            "count": count,
        })
        common = {
            "date": "2026-07-14",
            "code": "2330",
            "name": "台積電",
            "open": 99.0,
            "high": 106.0,
            "low": 98.0,
            "close": 105.0,
            "change_price": 5.0,
            "total_volume": 1000,
            "total_amount": 105_000_000,
            "volume_ratio": 1.8,
            "rank_value": 5.0,
            "ts": 1783941108520905000,
        }
        rows = [dict(common)]
        if "ChangePercentRank" in name:
            rows.append({**common, "code": "0050", "name": "ETF"})
        if "DayRangeRank" in name:
            rows.append({
                **common,
                "code": "2603",
                "name": "長榮",
                "close": 50.0,
                "total_volume": 1_000_000,
                "total_amount": 50_000_000,
            })
        return rows


class ShioajiSnapshotTimestampTests(unittest.TestCase):
    def test_nanosecond_timestamp_uses_taipei_wall_clock_without_extra_offset(self):
        self.assertEqual(
            normalize_shioaji_snapshot_time("1783941108520905000"),
            "2026-07-13T11:11:48.520905+08:00",
        )

    def test_stock_snapshot_exposes_normalized_timestamp(self):
        quotes, error = sinopac_backend.stock_snapshots(_SnapshotApi(), ["2330"])
        self.assertIsNone(error)
        self.assertEqual(
            quotes["2330"]["snapshotAt"],
            "2026-07-13T11:11:48.520905+08:00",
        )
        received_at = dt.datetime.fromisoformat(quotes["2330"]["receivedAt"])
        self.assertEqual(received_at.utcoffset(), dt.timedelta(hours=8))

    def test_large_snapshot_request_is_split_into_broker_safe_batches(self):
        api = _BatchSnapshotApi()
        codes = [str(code) for code in range(1000, 1405)]

        quotes, error = sinopac_backend.stock_snapshots(api, codes)

        self.assertIsNone(error)
        self.assertEqual(api.batch_sizes, [200, 200, 5])
        self.assertEqual(len(quotes), 405)

    def test_partial_batch_failure_returns_successful_quotes_and_explicit_error(self):
        api = _BatchSnapshotApi(fail_batch=2)
        codes = [str(code) for code in range(1000, 1405)]

        quotes, error = sinopac_backend.stock_snapshots(api, codes)

        self.assertEqual(api.batch_sizes, [200, 200, 5])
        self.assertEqual(len(quotes), 205)
        self.assertIn("部分失敗", error)


class ShioajiScannerTests(unittest.TestCase):
    def test_five_rankings_are_merged_and_etf_is_excluded(self):
        api = _ScannerApi()
        payload = sinopac_backend.stock_scanners(api, count=200)

        self.assertTrue(payload["ok"])
        self.assertEqual(set(payload["rankCounts"]), {
            "change_percent", "day_range", "volume", "amount", "tick_count",
        })
        by_symbol = {item["symbol"]: item for item in payload["rows"]}
        self.assertEqual(set(by_symbol), {"2330", "2603"})
        self.assertEqual(len(by_symbol["2330"]["rankTypes"]), 5)
        self.assertEqual(by_symbol["2603"]["totalVolumeLots"], 1000.0)
        self.assertEqual(payload["quotes"]["2330"]["totalVolumeUnit"], "lots")
        self.assertEqual(
            payload["quotes"]["2330"]["source"],
            "sinopac_shioaji_scanner",
        )

    def test_scanner_requests_largest_values_first_for_every_ranking(self):
        api = _ScannerApi()

        sinopac_backend.stock_scanners(api, count=200)

        self.assertEqual(len(api.calls), 5)
        self.assertTrue(all(call["ascending"] is True for call in api.calls))
        self.assertTrue(all(call["count"] == 200 for call in api.calls))
        self.assertTrue(any(
            "TickCountRank" in call["scannerType"] for call in api.calls
        ))


if __name__ == "__main__":
    unittest.main()
