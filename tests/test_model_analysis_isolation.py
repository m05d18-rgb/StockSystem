import datetime as dt
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ml_backend import backend
from modules.brain import engine as brain_engine


ROOT = Path(__file__).resolve().parents[1]


def _rule_rows(count=130):
    start = dt.date(2025, 1, 2)
    rows = []
    for index in range(count):
        close = 100 + index * 0.2
        rows.append({
            "date": (start + dt.timedelta(days=index)).isoformat(),
            "open": close - 0.3,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
            "volume": 2_000_000 + index * 1_000,
            "price_source": "TWSE STOCK_DAY_ALL official",
            "foreign_buy_sell": 1000,
            "trust_buy_sell": 200,
            "margin_balance": 10_000,
            "short_balance": 500,
            "chip_source": "TWSE official T86",
            "margin_source": "TWSE official MI_MARGN",
        })
    return rows


class RuleDataQualityTests(unittest.TestCase):
    def test_rule_quality_does_not_require_model_finance_coverage(self):
        rows = _rule_rows(120)
        rule_quality = backend.rule_analysis_data_quality("2330", rows, min_price_rows=120)
        model_quality = backend.model_data_quality("2330", rows)

        self.assertTrue(rule_quality["ok"])
        self.assertFalse(model_quality["ok"])
        self.assertNotIn("financeCoverageOk", rule_quality["missing"])

    def test_monster_scoring_defaults_to_rule_only(self):
        parameter = inspect.signature(backend.monster_score_for_symbol).parameters["use_model"]
        self.assertIs(parameter.default, False)

    def test_stale_rule_rows_force_refresh_before_rejection(self):
        stale_rows = _rule_rows(120)
        fresh_rows = [dict(row) for row in stale_rows]
        fresh_rows[-1]["date"] = "2026-07-13"
        with patch.object(
            backend, "load_price_rows", side_effect=[stale_rows, fresh_rows]
        ), patch.object(
            backend, "rows_with_verified_sources", side_effect=lambda rows: rows
        ), patch.object(
            backend, "latest_complete_price_date", return_value="2026-07-13"
        ), patch.object(
            backend, "update_prices", return_value={"2330": len(fresh_rows)}
        ) as update:
            rows, quality = backend.ensure_rule_analysis_rows(
                "2330", repair=True, min_price_rows=120
            )

        self.assertTrue(quality["ok"])
        self.assertTrue(quality["repairAttempted"])
        self.assertEqual(rows[-1]["date"], "2026-07-13")
        self.assertTrue(update.call_args.kwargs["force_refresh"])


class ProductionBrainIsolationTests(unittest.TestCase):
    def test_default_brain_never_predicts_or_returns_model_payload(self):
        rows = _rule_rows()
        deterministic_inputs = {
            "probability": None,
            "riskPenalty": 0.10,
            "setupScore": 0.72,
            "close": rows[-1]["close"],
            "marketGate": {
                "allowBuy": True,
                "hotMarket": False,
                "stockStrongerThanTaiex": True,
                "taiexAboveMonthLine": True,
            },
            "tradeGate": {
                "marketOk": True,
                "strongerThanMarket": True,
                "riskOk": True,
                "volumeExpanded": True,
            },
        }
        technical = {
            "ok": True,
            "score": 0.70,
            "text": "70.0%",
            "components": {"volume": 0.70, "obv": 0.70},
            "volumeRatio": 1.5,
            "obv": {"ok": True, "score": 0.70, "text": "OBV向上"},
            "kd": {},
            "macd": {},
        }
        short_money = {
            "ok": True,
            "score": 0.70,
            "text": "70.0%",
            "available": 1,
            "passed": 1,
            "details": [],
        }
        kline = {
            "ok": True,
            "score": 0.70,
            "text": "70.0%",
            "components": {"volume": 0.70, "market": 0.70, "ma": 0.70, "model": 0.99},
            "patterns": ["突破前高"],
            "volumeRatio": 1.5,
            "ma5": 120,
            "ma20": 118,
        }
        strategy = {
            "ok": True,
            "score": 0.70,
            "text": "70.0%",
            "available": 1,
            "passed": 1,
            "details": [],
            "decisionFlow": {
                "score": 0.70,
                "entryScore": 0.75,
                "holdScore": 0.70,
                "exitScore": 0.10,
                "riskScore": 0.10,
                "reversalScore": 0.70,
                "signals": {"breakoutHigh": True, "volumeExpanded": True},
            },
        }

        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "brain_decision_inputs", return_value=deterministic_inputs), \
             patch.object(backend, "predict_symbol", side_effect=AssertionError("正式 Brain 不得跑模型")) as predict, \
             patch.object(backend, "get_previous_brain_v2_snapshot", return_value=None), \
             patch.object(brain_engine, "_brain_technical_indicator_score", return_value=technical), \
             patch.object(brain_engine, "_brain_short_money_score", return_value=short_money), \
             patch.object(brain_engine, "_brain_kline_score", return_value=kline), \
             patch.object(brain_engine, "_brain_backtest_strategy_score", return_value=strategy):
            result = brain_engine.build_brain_decision("2330", context="monster")

        predict.assert_not_called()
        self.assertTrue(result["ok"])
        for key in (
            "confidence", "threshold", "modelVersion", "trainedAt",
            "modelBreakdown", "prediction", "buySignal",
        ):
            self.assertNotIn(key, result)
        self.assertNotIn("formalPass", result["brainV2"])
        self.assertNotIn("formalModel", result["strategyProfile"]["weights"])
        self.assertNotIn("model", result["klineScore"]["components"])
        self.assertNotIn("formalModel", [row["key"] for row in result["brainV2"]["components"]])
        self.assertTrue(result["ruleBreakdown"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("正式模型", serialized)
        self.assertNotIn("獨立模型", serialized)


class FrontendIsolationContractTests(unittest.TestCase):
    def test_main_stock_app_does_not_fetch_or_cache_model_predictions(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("/api/ml/predict", source)
        self.assertNotIn("/api/ml/predictions", source)
        self.assertNotIn("backendPredictionMap", source)
        self.assertNotIn("/api/backtest-summary", source)
        self.assertNotIn("/api/radar/track-record", source)
        self.assertNotIn("模型主路徑門檻", source)


if __name__ == "__main__":
    unittest.main()
