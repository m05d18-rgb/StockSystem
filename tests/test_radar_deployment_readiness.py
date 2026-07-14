import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import ml_backend
from ml_backend import StockMLBackend


def build_backend(tmp_dir):
    backend = StockMLBackend()
    backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
    backend.init_db()
    return backend


def outcome_summary(avg_net_return, profit_factor, settled):
    return {
        "signals": settled,
        "settled": settled,
        "targetHitRate": 0.48 if settled else None,
        "avgNetReturn": avg_net_return if settled else None,
        "profitFactor": profit_factor if settled else None,
    }


def track_record(
    avg_net_return, profit_factor, settled=80, *,
    proxy_settled=120, proxy_avg_net_return=None, proxy_profit_factor=None,
):
    confirmed = outcome_summary(avg_net_return, profit_factor, settled)
    proxy = outcome_summary(
        avg_net_return if proxy_avg_net_return is None else proxy_avg_net_return,
        profit_factor if proxy_profit_factor is None else proxy_profit_factor,
        proxy_settled,
    )
    return {
        "ok": True,
        "eligible": outcome_summary(avg_net_return, profit_factor, settled + proxy_settled),
        "entryModePerformance": {
            "intradayConfirmed": {"eligible": confirmed},
            "nextOpenProxy": {"eligible": proxy},
        },
    }


def walk_forward(avg_net_return, profit_factor, precision_lift):
    return {
        "source": "walk_forward_observation",
        "walkForward": {
            "precisionLift": precision_lift,
            "calibrated": {
                "trades": 500,
                "avgNetReturn": avg_net_return,
                "profitFactor": profit_factor,
            },
        },
    }


def test_negative_real_and_oos_results_are_persisted_as_observation_only():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with patch.object(
            backend,
            "compute_radar_score_track_record",
            return_value=track_record(-0.01, 0.8),
        ), patch.object(
            ml_backend,
            "RADAR_RULE_CONFIG",
            walk_forward(-0.005, 0.9, 0.01),
        ):
            result = backend.refresh_radar_deployment_readiness("2026-07-13")
        with backend.connect() as conn:
            stored = conn.execute("""
                SELECT enforced, formal_ready, live_pass, walk_forward_pass
                FROM radar_deployment_readiness
                WHERE readiness_date = '2026-07-13'
            """).fetchone()

    assert result["enforced"] is True
    assert result["formalReady"] is False
    assert result["observationOnly"] is True
    assert tuple(stored) == (1, 0, 0, 0)
    assert any("平均成本後報酬" in reason for reason in result["reasons"])


def test_formal_gate_requires_five_consecutive_pass_records():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        passing_config = walk_forward(0.01, 1.2, 0.03)
        with patch.object(
            backend,
            "compute_radar_score_track_record",
            return_value=track_record(0.015, 1.3),
        ), patch.object(ml_backend, "RADAR_RULE_CONFIG", passing_config):
            results = [
                backend.refresh_radar_deployment_readiness(date)
                for date in (
                    "2026-07-06", "2026-07-07", "2026-07-08",
                    "2026-07-09", "2026-07-10",
                )
            ]

    assert [item["consecutivePassDays"] for item in results] == [1, 2, 3, 4, 5]
    assert all(item["formalReady"] is False for item in results[:4])
    assert results[-1]["formalReady"] is True
    assert results[-1]["independentModelUsed"] is False


def test_small_sample_is_recorded_but_not_enforced():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with patch.object(
            backend,
            "compute_radar_score_track_record",
            return_value=track_record(0.02, 1.4, settled=12),
        ), patch.object(
            ml_backend,
            "RADAR_RULE_CONFIG",
            walk_forward(0.01, 1.2, 0.03),
        ):
            result = backend.refresh_radar_deployment_readiness("2026-07-13")

    assert result["enforced"] is False
    assert result["formalReady"] is False
    assert "樣本不足" in result["reasons"][0]


def test_positive_next_open_proxy_cannot_unlock_without_confirmed_entries():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        proxy_only = track_record(
            None,
            None,
            settled=0,
            proxy_settled=500,
            proxy_avg_net_return=0.03,
            proxy_profit_factor=1.5,
        )
        with patch.object(
            backend,
            "compute_radar_score_track_record",
            return_value=proxy_only,
        ), patch.object(
            ml_backend,
            "RADAR_RULE_CONFIG",
            walk_forward(0.01, 1.2, 0.03),
        ):
            result = backend.refresh_radar_deployment_readiness("2026-07-13")

    assert result["performanceBasis"] == "intraday_confirmed_only"
    assert result["live"]["settled"] == 0
    assert result["proxy"]["settled"] == 500
    assert result["enforced"] is False
    assert result["formalReady"] is False
    assert "盤中可成交報價確認" in result["reasons"][0]


def test_legacy_pass_days_do_not_count_toward_new_confirmed_basis():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with backend.connect() as conn:
            for date in ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"):
                legacy = {"ok": True, "readinessDate": date, "formalReady": False}
                conn.execute("""
                    INSERT INTO radar_deployment_readiness (
                        readiness_date, generated_at, eligible_settled,
                        target_hit_rate, avg_net_return, profit_factor,
                        live_pass, walk_forward_pass, consecutive_pass_days,
                        enforced, formal_ready, reasons_json, payload_json
                    ) VALUES (?, ?, 80, 0.5, 0.02, 1.3, 1, 1, 4, 1, 0, '[]', ?)
                """, (date, f"{date} 18:00:00", json.dumps(legacy)))
        with patch.object(
            backend,
            "compute_radar_score_track_record",
            return_value=track_record(0.02, 1.3),
        ), patch.object(
            ml_backend,
            "RADAR_RULE_CONFIG",
            walk_forward(0.01, 1.2, 0.03),
        ):
            result = backend.refresh_radar_deployment_readiness("2026-07-10")

    assert result["live"]["pass"] is True
    assert result["walkForward"]["pass"] is True
    assert result["consecutivePassDays"] == 1
    assert result["formalReady"] is False


def test_stale_readiness_is_not_refreshed_on_a_closed_market_day():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        stored_payload = {
            "ok": True,
            "readinessDate": "2026-07-09",
            "performanceBasisVersion": 2,
            "performanceBasis": "intraday_confirmed_only",
            "enforced": True,
            "formalReady": False,
            "reasons": ["觀察中"],
        }
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO radar_deployment_readiness (
                    readiness_date, generated_at, eligible_settled,
                    target_hit_rate, avg_net_return, profit_factor,
                    live_pass, walk_forward_pass, consecutive_pass_days,
                    enforced, formal_ready, reasons_json, payload_json
                ) VALUES ('2026-07-09', '2026-07-09 18:00:00', 80,
                          0.4, -0.01, 0.8, 0, 0, 0, 1, 0, '[]', ?)
            """, (json.dumps(stored_payload),))
        with patch.object(ml_backend, "today_key", return_value="2026-07-11"), \
             patch.object(backend, "_cached_market_day_status", return_value={
                 "known": True, "isTradingDay": False, "reason": "週末",
             }), \
             patch.object(backend, "refresh_radar_deployment_readiness") as refresh:
            result = backend.current_radar_deployment_readiness(refresh_if_stale=True)

    assert result["readinessDate"] == "2026-07-09"
    refresh.assert_not_called()


def test_legacy_readiness_is_recomputed_to_confirmed_basis_even_on_closed_day():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        legacy_payload = {
            "ok": True,
            "readinessDate": "2026-07-09",
            "enforced": True,
            "formalReady": True,
            "live": {"settled": 80, "avgNetReturn": 0.02, "profitFactor": 1.3},
        }
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO radar_deployment_readiness (
                    readiness_date, generated_at, eligible_settled,
                    target_hit_rate, avg_net_return, profit_factor,
                    live_pass, walk_forward_pass, consecutive_pass_days,
                    enforced, formal_ready, reasons_json, payload_json
                ) VALUES ('2026-07-09', '2026-07-09 18:00:00', 80,
                          0.5, 0.02, 1.3, 1, 1, 5, 1, 1, '[]', ?)
            """, (json.dumps(legacy_payload),))
        with patch.object(
            backend,
            "compute_radar_score_track_record",
            return_value=track_record(None, None, settled=0),
        ), patch.object(
            ml_backend,
            "RADAR_RULE_CONFIG",
            walk_forward(0.01, 1.2, 0.03),
        ), patch.object(ml_backend, "today_key", return_value="2026-07-11"):
            result = backend.current_radar_deployment_readiness(refresh_if_stale=True)

    assert result["performanceBasisVersion"] == 2
    assert result["live"]["settled"] == 0
    assert result["formalReady"] is False
    assert result["consecutivePassDays"] == 0
