import datetime as dt

from portfolio_exit import build_position_exit, evaluate_exit_lot


def price_rows(count=140, close_fn=None, fundamental_latest=None):
    start = dt.date(2026, 1, 1)
    rows = []
    for index in range(count):
        close = float(close_fn(index) if close_fn else 100 + index * 0.1)
        row = {
            "date": (start + dt.timedelta(days=index)).isoformat(),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000,
            "price_source": "TWSE official",
            "revenue_growth": 10.0,
            "gross_margin": 30.0,
            "operating_margin": 15.0,
            "roe": 12.0,
            "debt_ratio": 35.0,
            "operating_cashflow_ratio": 15.0,
            "financial_statement_source": "MOPS",
        }
        rows.append(row)
    if fundamental_latest:
        rows[-1].update(fundamental_latest)
    return rows


def quote(rows, current, *, final=True):
    return {
        "currentPrice": current,
        "openPrice": current,
        "highPrice": current,
        "lowPrice": current,
        "totalVolume": 1_000_000,
        "totalVolumeUnit": "shares",
        "snapshotAt": f"{rows[-1]['date']} 13:30:00",
        "quoteFresh": True,
        "sessionFinal": final,
        "priceSource": "Shioaji",
    }


def lot(horizon, rows, buy_index, price=100, shares=1000):
    return {
        "tradeId": 1,
        "strategyHorizon": horizon,
        "strategyHorizonSource": "order_entry",
        "buyDate": rows[buy_index]["date"] if buy_index is not None else None,
        "price": price,
        "shares": shares,
    }


def short_profit_reversal_rows():
    values = (
        [100.0] * 11
        + [110.0, 120.0, 130.0, 140.0]
        + [138.0 - index * 0.55 for index in range(25)]
    )
    return price_rows(len(values), lambda index: values[index])


def test_short_target_plus_ten_with_intact_trend_stays_hold_after_ten_days():
    rows = price_rows(30, lambda _: 100)
    result = evaluate_exit_lot(lot("short_trade", rows, -16), rows, quote(rows, 110), rows[-1]["date"])
    assert result["tradingDaysHeld"] == 15
    assert result["type"] == "hold"
    assert result["decisionVerified"] is False
    assert result["targetEverReached"] is True
    assert result["profitContinuationIntact"] is True
    assert result["takeProfit"] == 110
    assert result["trailingStop"] == 105
    assert result["stopLoss"] == 93
    assert "續漲結構仍在" in result["status"]


def test_stale_target_quote_never_creates_a_sell_decision_or_notification():
    rows = price_rows(30, lambda _: 100)
    stale_quote = quote(rows, 110)
    stale_quote["quoteFresh"] = False
    result = evaluate_exit_lot(lot("short_trade", rows, -6), rows, stale_quote, rows[-1]["date"])
    assert result["type"] == "hold"
    assert result["dataReady"] is False
    assert result["decisionVerified"] is False
    assert result["canNotify"] is False


def test_short_profit_exit_requires_peak_trail_and_weak_trend_confirmation():
    rows = short_profit_reversal_rows()
    result = evaluate_exit_lot(
        lot("short_trade", rows, 10, price=100),
        rows,
        quote(rows, 125),
        rows[-1]["date"],
    )

    assert result["targetEverReached"] is True
    assert result["profitTrailBreached"] is True
    assert result["shortReboundAssessment"]["trendBreak"] is True
    assert result["type"] == "phase1"
    assert result["status"] == "達標後續漲結構轉弱"
    assert result["decisionVerified"] is True
    assert result["confirmSellPrice"] == 125
    assert len(result["evidence"]) >= 4


def test_target_reversal_after_profit_is_gone_is_not_labeled_take_profit():
    rows = short_profit_reversal_rows()
    result = build_position_exit(
        "2330",
        "台積電",
        {"code": "2330", "shares": 1000, "price": 100, **quote(rows, 90)},
        [lot("short_trade", rows, 10, price=100, shares=1000)],
        rows,
        rows[-1]["date"],
    )
    sell = next(alert for alert in result["alerts"] if alert["typeClass"] == "sell")
    confirm = next(alert for alert in result["alerts"] if alert["typeClass"] == "confirm")

    assert result["targetEverReached"] is True
    assert result["type"] == "phase3"
    assert result["status"] == "達標後漲幅回吐且趨勢轉弱"
    assert result["estimatedNetPnl"] < 0
    assert sell["canNotify"] is False
    assert confirm["canNotify"] is True
    assert confirm["status"] == "短期走勢轉弱，建議賣出"


def test_weekend_undated_cached_quote_does_not_create_a_synthetic_daily_bar():
    rows = price_rows(30, lambda _: 100)
    stale_quote = {
        "currentPrice": 108,
        "quoteFresh": False,
        "priceSource": "Shioaji cached portfolio",
    }
    result = evaluate_exit_lot(
        lot("short_trade", rows, -6),
        rows,
        stale_quote,
        dt.date(2026, 7, 11),
    )
    assert result["dataDate"] == rows[-1]["date"]
    assert result["historyRows"] == len(rows)
    assert result["quoteAt"] is None
    assert result["dataReady"] is False
    assert result["decisionVerified"] is False


def test_nanosecond_snapshot_is_normalized_without_becoming_a_fresh_bar():
    rows = price_rows(30, lambda _: 100)
    stale_quote = {
        "currentPrice": 108,
        "snapshotAt": "1783607400000000000",
        "priceSource": "Shioaji cached portfolio",
    }
    result = evaluate_exit_lot(
        lot("short_trade", rows, -6),
        rows,
        stale_quote,
        dt.date(2026, 7, 11),
    )
    assert result["quoteAt"] == "2026-07-09 22:30:00"
    assert result["quoteTimestampRaw"] is None
    assert result["dataDate"] == rows[-1]["date"]
    assert result["historyRows"] == len(rows)
    assert result["quoteFresh"] is False


def test_short_ten_sessions_without_weak_rebound_evidence_stays_hold():
    rows = price_rows(30, lambda _: 103)
    result = evaluate_exit_lot(lot("short_trade", rows, -11), rows, quote(rows, 103), rows[-1]["date"])
    assert result["tradingDaysHeld"] == 10
    assert result["type"] == "hold"
    assert result["decisionVerified"] is False
    assert result["shortReboundRiskScore"] == 0


def test_short_minus_seven_alone_is_warning_not_sell_when_trend_structure_is_intact():
    rows = price_rows(30, lambda index: 80 + index)
    result = evaluate_exit_lot(
        lot("short_trade", rows, -6, price=100),
        rows,
        quote(rows, 90),
        rows[-1]["date"],
    )

    assert result["riskLineBreached"] is True
    assert result["type"] == "hold"
    assert result["decisionVerified"] is False
    assert result["shortReboundAssessment"]["trendBreak"] is False
    assert "不單獨通知賣出" in result["note"]


def test_short_failed_rebound_and_broken_twenty_day_trend_is_verified_exit():
    tail = [108, 106, 107, 104, 102, 100]
    rows = price_rows(
        30,
        lambda index: 120 - index * 0.5 if index < 24 else tail[index - 24],
    )
    result = evaluate_exit_lot(
        lot("short_trade", rows, -8, price=110),
        rows,
        quote(rows, 100),
        rows[-1]["date"],
    )

    assert result["type"] == "phase3"
    assert result["status"] == "短期反彈結構失敗"
    assert result["decisionVerified"] is True
    assert result["shortReboundRiskScore"] >= 65
    assert result["shortReboundCalibratedProbability"] is False
    assert result["shortReboundAssessment"]["trendBreak"] is True
    assert result["shortReboundAssessment"]["failedRebound"] is True
    assert all("-7%" not in evidence for evidence in result["evidence"])


def test_unknown_buy_date_never_triggers_time_exit():
    rows = price_rows(40, lambda _: 103)
    result = evaluate_exit_lot(lot("short_trade", rows, None), rows, quote(rows, 103), rows[-1]["date"])
    assert result["buyDateKnown"] is False
    assert result["type"] != "time_stop"
    assert result["decisionVerified"] is False


def test_intraday_partial_volume_does_not_trigger_volume_exit():
    rows = price_rows(30, lambda _: 100)
    live_quote = quote(rows, 99, final=False)
    live_quote["totalVolume"] = 100_000
    result = evaluate_exit_lot(lot("short_trade", rows, -3), rows, live_quote, rows[-1]["date"])
    assert result["type"] == "hold"


def test_same_day_broker_lot_volume_does_not_replace_official_close_volume():
    rows = price_rows(30, lambda _: 100)
    rows[-1]["volume"] = 28_776_375
    rows[-1]["price_source"] = "TWSE official MI_INDEX afterTrading"
    final_quote = quote(rows, 99)
    final_quote["totalVolume"] = 28_350
    final_quote["totalVolumeUnit"] = "lots"

    result = evaluate_exit_lot(lot("short_trade", rows, -3), rows, final_quote, rows[-1]["date"])

    assert result["type"] == "hold"
    assert result["volumeRatio"] > 1


def test_shioaji_lot_volume_is_converted_for_synthetic_close_bar():
    rows = price_rows(29, lambda _: 100)
    evaluation_date = dt.date.fromisoformat(rows[-1]["date"]) + dt.timedelta(days=1)
    final_quote = {
        "currentPrice": 99,
        "openPrice": 100,
        "highPrice": 100,
        "lowPrice": 99,
        "totalVolume": 1000,
        "totalVolumeUnit": "lots",
        "snapshotAt": f"{evaluation_date.isoformat()} 13:30:00",
        "quoteFresh": True,
        "sessionFinal": True,
        "priceSource": "Shioaji",
    }

    result = evaluate_exit_lot(lot("short_trade", rows, -3), rows, final_quote, evaluation_date)

    assert result["type"] == "hold"
    assert result["volumeRatio"] == 1.0


def test_mid_horizon_never_uses_short_ten_day_or_fixed_minus_seven_rules():
    rows = price_rows(80, lambda index: 100 + index * 0.1)
    result = evaluate_exit_lot(lot("mid_swing", rows, -31, price=110), rows, quote(rows, 105), rows[-1]["date"])
    assert result["tradingDaysHeld"] == 30
    assert result["type"] != "time_stop"
    assert result["stopLoss"] is None


def test_mid_horizon_uses_ma60_reversal():
    rows = price_rows(90, lambda index: 125 if index < 55 else 125 - (index - 54) * 1.5)
    result = evaluate_exit_lot(lot("mid_swing", rows, -40, price=120), rows, quote(rows, 72.5), rows[-1]["date"])
    assert result["type"] == "phase3"
    assert result["decisionVerified"] is True
    assert any("MA60" in reason for reason in result["evidence"])


def test_mid_unknown_buy_date_never_uses_pre_entry_history_for_trailing_stop():
    rows = price_rows(80, lambda index: 200 if index < 20 else 100)
    result = evaluate_exit_lot(lot("mid_swing", rows, None, price=100), rows, quote(rows, 100), rows[-1]["date"])
    assert result["buyDateKnown"] is False
    assert result["swingHigh"] is None
    assert result["trailingStop"] is None
    assert result["type"] == "hold"
    assert any(condition["value"] == "買進日未知，停用" for condition in result["conditions"])


def test_mid_unknown_buy_date_can_only_exit_from_ma_trend_rules():
    rows = price_rows(90, lambda index: 125 if index < 55 else 125 - (index - 54) * 1.5)
    result = evaluate_exit_lot(lot("mid_swing", rows, None, price=120), rows, quote(rows, 72.5), rows[-1]["date"])
    assert result["swingHigh"] is None
    assert result["trailingStop"] is None
    assert result["type"] == "phase3"
    assert result["decisionVerified"] is True
    assert all("波段高點" not in reason for reason in result["evidence"])


def test_long_horizon_does_not_sell_for_ten_day_underperformance():
    rows = price_rows(140, lambda index: 100 + index * 0.05)
    result = evaluate_exit_lot(lot("long_trend", rows, -91, price=104), rows, quote(rows, 106.95), rows[-1]["date"])
    assert result["tradingDaysHeld"] == 90
    assert result["type"] != "time_stop"
    assert result["stopLoss"] is None


def test_long_exit_requires_trend_reversal_and_fundamental_deterioration():
    rows = price_rows(
        140,
        lambda index: 130 if index < 75 else 130 - (index - 74) * 0.9,
        fundamental_latest={
            "revenue_growth": -15.0,
            "gross_margin": 24.0,
            "operating_margin": 8.0,
            "financial_statement_source": "MOPS",
        },
    )
    result = evaluate_exit_lot(lot("long_trend", rows, -101, price=125), rows, quote(rows, 71.5), rows[-1]["date"])
    assert result["type"] == "phase3"
    assert result["decisionVerified"] is True
    assert result["fundamentalDeterioration"]


def test_long_trend_reversal_without_fundamental_damage_stays_observation():
    rows = price_rows(140, lambda index: 130 if index < 75 else 130 - (index - 74) * 0.9)
    result = evaluate_exit_lot(lot("long_trend", rows, -101, price=125), rows, quote(rows, 71.5), rows[-1]["date"])
    assert result["type"] == "hold"
    assert result["decisionVerified"] is False
    assert "等待基本面確認" in result["status"]


def test_position_selects_only_actionable_fifo_lot_and_preserves_horizon():
    rows = price_rows(40, lambda _: 103)
    holding = {
        "code": "2330",
        "name": "台積電",
        "shares": 1500,
        "price": 101,
        **quote(rows, 110),
    }
    lots = [
        lot("short_trade", rows, -11, price=100, shares=1000),
        {
            "tradeId": None,
            "strategyHorizon": "unknown",
            "strategyHorizonSource": "uncovered",
            "buyDate": None,
            "price": 103,
            "shares": 500,
        },
    ]
    result = build_position_exit("2330", "台積電", holding, lots, rows, rows[-1]["date"])
    assert result["strategyHorizon"] == "short_trade"
    assert result["type"] == "hold"
    assert result["targetEverReached"] is True
    assert result["sellShares"] == 0
    assert result["mixedHorizons"] is True
    assert result["positionBuyDateKnown"] is False


def test_position_alert_uses_plain_status_but_keeps_technical_status_for_audit():
    tail = [108, 106, 107, 104, 102, 100]
    rows = price_rows(
        30,
        lambda index: 120 - index * 0.5 if index < 24 else tail[index - 24],
    )
    holding = {"code": "2330", "shares": 1000, "price": 110, **quote(rows, 100)}

    result = build_position_exit(
        "2330", "台積電", holding,
        [lot("short_trade", rows, -8, price=110, shares=1000)],
        rows, rows[-1]["date"],
    )
    confirm = next(alert for alert in result["alerts"] if alert["typeClass"] == "confirm")

    assert result["status"] == "短期反彈結構失敗"
    assert confirm["status"] == "短期走勢轉弱，建議賣出"
    assert confirm["technicalStatus"] == "短期反彈結構失敗"


def test_position_alert_plain_status_says_continue_holding_when_no_exit_signal():
    rows = price_rows(30, lambda _: 103)
    holding = {"code": "2330", "shares": 1000, "price": 100, **quote(rows, 103)}

    result = build_position_exit(
        "2330", "台積電", holding,
        [lot("short_trade", rows, -6, price=100, shares=1000)],
        rows, rows[-1]["date"],
    )
    confirm = next(alert for alert in result["alerts"] if alert["typeClass"] == "confirm")

    assert result["type"] == "hold"
    assert confirm["status"] == "目前沒有賣出訊號，繼續持有"


def test_position_alert_says_target_is_a_milestone_while_trend_is_intact():
    rows = price_rows(30, lambda _: 100)
    holding = {"code": "2330", "shares": 1000, "price": 100, **quote(rows, 110)}
    result = build_position_exit(
        "2330", "台積電", holding,
        [lot("short_trade", rows, -16, price=100, shares=1000)],
        rows, rows[-1]["date"],
    )
    sell = next(alert for alert in result["alerts"] if alert["typeClass"] == "sell")

    assert result["type"] == "hold"
    assert sell["status"] == "已達 +10%，趨勢未轉弱就續抱"
    assert sell["canNotify"] is False


def test_position_exit_rounds_sell_quantity_down_to_whole_board_lots():
    rows = short_profit_reversal_rows()
    holding = {"code": "2330", "shares": 1500, "price": 100, **quote(rows, 125)}
    result = build_position_exit(
        "2330", "台積電", holding,
        [lot("short_trade", rows, 10, price=100, shares=1500)],
        rows, rows[-1]["date"],
    )

    assert result["type"] == "phase1"
    assert result["decisionVerified"] is True
    assert result["sellShares"] == 1000
    assert result["wholeLotSellOnly"] is True
    assert result["oddLotSharesExcluded"] == 500
    assert all(alert["sellShares"] == 1000 for alert in result["alerts"])


def test_odd_lot_only_exit_condition_stays_observation_without_notification():
    rows = short_profit_reversal_rows()
    holding = {"code": "2330", "shares": 500, "price": 100, **quote(rows, 125)}
    result = build_position_exit(
        "2330", "台積電", holding,
        [lot("short_trade", rows, 10, price=100, shares=500)],
        rows, rows[-1]["date"],
    )

    assert result["type"] == "phase1"
    assert result["decisionVerified"] is False
    assert result["sellShares"] == 0
    assert result["oddLotSharesExcluded"] == 500
    assert "不足整張" in result["status"]
    assert all(alert["canNotify"] is False for alert in result["alerts"])
