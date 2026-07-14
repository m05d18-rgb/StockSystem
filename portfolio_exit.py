"""Canonical backend exit policy for real portfolio positions.

The module is deliberately independent from the web UI.  A strategy horizon is
locked on the BUY trade, real fills provide the FIFO buy date, and every caller
(dashboard, notifications, and trade review) consumes the same result payload.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable


POLICY_VERSION = "portfolio-exit-v2"
CALCULATION_REVISION = "profit-continuation-v4"

BUY_COMMISSION_RATE = 0.001425
SELL_COMMISSION_RATE = 0.001425
SELL_TAX_RATE = 0.003
EXIT_SLIPPAGE_RATE = 0.001
BOARD_LOT_SHARES = 1000
SHORT_REBOUND_WEAK_SCORE = 45
SHORT_REBOUND_EXIT_SCORE = 65
SHORT_REBOUND_MIN_EVIDENCE = 3
SHORT_PROFIT_TRAIL_PCT = 0.08
SHORT_PROFIT_FLOOR_PCT = 0.05

HORIZON_CONFIG = {
    "short_trade": {
        "label": "短期",
        "holdingDays": "10 個交易日",
        "minimumRows": 20,
    },
    "mid_swing": {
        "label": "中期",
        "holdingDays": "20～60 個交易日",
        "minimumRows": 60,
    },
    "long_trend": {
        "label": "長期",
        "holdingDays": "60 個交易日以上",
        "minimumRows": 120,
    },
    "unknown": {
        "label": "週期未知",
        "holdingDays": "不啟用時間出場",
        "minimumRows": 0,
    },
}

HORIZON_ALIASES = {
    "short": "short_trade",
    "short_term": "short_trade",
    "short-trade": "short_trade",
    "短期": "short_trade",
    "短線": "short_trade",
    "mid": "mid_swing",
    "medium": "mid_swing",
    "medium_term": "mid_swing",
    "mid-swing": "mid_swing",
    "中期": "mid_swing",
    "波段": "mid_swing",
    "long": "long_trend",
    "long_term": "long_trend",
    "long-trend": "long_trend",
    "長期": "long_trend",
    "長線": "long_trend",
}

ACTIONABLE_TYPES = {"stop", "phase1", "phase2", "phase3", "time_stop"}
DECISION_PRIORITY = {
    "stop": 100,
    "phase3": 90,
    "phase2": 80,
    "time_stop": 70,
    "phase1": 60,
    "hold": 10,
    "unknown": 0,
}

FUNDAMENTAL_FIELDS = (
    "revenue_growth",
    "gross_margin",
    "operating_margin",
    "roe",
    "debt_ratio",
    "operating_cashflow_ratio",
)

OFFICIAL_FINANCE_SOURCE_WORDS = ("mops", "finmind", "twse", "tpex", "證交所", "櫃買")
NON_OFFICIAL_SOURCE_WORDS = ("yahoo", "fallback", "estimate", "derived", "simulate", "推估")


def normalize_strategy_horizon(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in HORIZON_CONFIG:
        return text
    return HORIZON_ALIASES.get(text, "unknown")


def strategy_horizon_info(value: Any) -> dict[str, Any]:
    key = normalize_strategy_horizon(value)
    return {"key": key, **HORIZON_CONFIG[key]}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _whole_lot_sell_shares(value: Any) -> int:
    shares = max(0, int(_number(value) or 0))
    return (shares // BOARD_LOT_SHARES) * BOARD_LOT_SHARES


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        stamp = _datetime(value)
        return stamp.date() if stamp else None


def _datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
        if parsed.tzinfo is not None:
            taipei = dt.timezone(dt.timedelta(hours=8))
            parsed = parsed.astimezone(taipei).replace(tzinfo=None)
        return parsed
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    try:
        epoch = float(text)
        if epoch == epoch and abs(epoch) >= 1_000_000_000:
            while abs(epoch) > 10_000_000_000:
                epoch /= 1000
            taipei = dt.timezone(dt.timedelta(hours=8))
            return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone(
                taipei
            ).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            taipei = dt.timezone(dt.timedelta(hours=8))
            parsed = parsed.astimezone(taipei).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _moving_average(rows: list[dict[str, Any]], days: int, offset: int = 0) -> float | None:
    end = len(rows) - max(0, offset)
    start = end - days
    if start < 0 or end <= 0:
        return None
    return _mean(row.get("close") for row in rows[start:end])


def _position_economics(
    entry_price: float,
    current_price: float,
    shares: int,
    entry_cost_includes_buy_fee: bool = False,
    entry_cost_amount: float | None = None,
) -> dict[str, float | bool | str]:
    buy_amount = entry_price * shares
    market_value = current_price * shares
    broker_cost_known = entry_cost_amount is not None and entry_cost_amount > 0
    buy_commission = (
        0.0
        if broker_cost_known or entry_cost_includes_buy_fee
        else buy_amount * BUY_COMMISSION_RATE
    )
    sell_commission = market_value * SELL_COMMISSION_RATE
    sell_tax = market_value * SELL_TAX_RATE
    exit_slippage = market_value * EXIT_SLIPPAGE_RATE
    entry_cost = float(entry_cost_amount) if broker_cost_known else buy_amount + buy_commission
    entry_cost_adjustment = entry_cost - buy_amount
    exit_costs = sell_commission + sell_tax + exit_slippage
    net_proceeds = market_value - exit_costs
    net_pnl = net_proceeds - entry_cost
    return {
        "buyAmount": round(buy_amount, 2),
        "marketValue": round(market_value, 2),
        "grossPnl": round(market_value - buy_amount, 2),
        "grossReturnPct": round((current_price / entry_price - 1) * 100, 4),
        "buyCommission": round(buy_commission, 2),
        "sellCommission": round(sell_commission, 2),
        "sellTax": round(sell_tax, 2),
        "exitSlippage": round(exit_slippage, 2),
        "estimatedExitCosts": round(exit_costs, 2),
        "estimatedNetPnl": round(net_pnl, 2),
        "netPnlRate": round((net_pnl / entry_cost) * 100, 4) if entry_cost else 0.0,
        "entryCostIncludesBuyFee": bool(entry_cost_includes_buy_fee),
        "entryCostAmount": round(entry_cost, 2),
        "entryCostPrice": round(entry_cost / shares, 6) if shares else 0.0,
        "entryCostAdjustment": round(entry_cost_adjustment, 2),
        "entryCostSource": "broker_reported" if broker_cost_known else "execution_estimate",
    }


def _normalized_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        row_date = _date(raw.get("date"))
        close = _number(raw.get("close"))
        if row_date is None or close is None or close <= 0:
            continue
        row = dict(raw)
        row["date"] = row_date.isoformat()
        row["close"] = close
        by_date[row["date"]] = row
    return [by_date[key] for key in sorted(by_date)]


def _quote_volume_shares(quote: dict[str, Any]) -> float | None:
    volume = _number(quote.get("totalVolume") or quote.get("volume"))
    if volume is None or volume <= 0:
        return None
    unit = str(
        quote.get("totalVolumeUnit") or quote.get("volumeUnit") or ""
    ).strip().lower()
    if unit in {"lot", "lots", "board_lot", "board_lots", "張"}:
        return volume * 1000
    if unit in {"share", "shares", "股"}:
        return volume
    source = " ".join(
        str(quote.get(key) or "").strip().lower()
        for key in ("priceSource", "quoteSource", "marketDataSource")
    )
    if any(name in source for name in ("shioaji", "永豐", "capital strategy king", "群益")):
        return volume * 1000
    return volume


def _has_official_daily_volume(row: dict[str, Any]) -> bool:
    volume = _number(row.get("volume"))
    source = str(row.get("price_source") or row.get("priceSource") or "").lower()
    return bool(
        volume is not None
        and volume > 0
        and any(marker in source for marker in (
            "twse official", "tpex openapi", "mi_index", "stock_day_all", "aftertrading"
        ))
    )


def _rows_with_quote(
    rows: Iterable[dict[str, Any]], quote: dict[str, Any], evaluation_date: dt.date
) -> list[dict[str, Any]]:
    prepared = _normalized_rows(rows)
    current = _number(quote.get("currentPrice") or quote.get("price"))
    if current is None or current <= 0:
        return prepared
    quote_fresh = _quote_is_fresh(quote, evaluation_date)
    quote_date = _date(quote.get("quoteDate") or quote.get("snapshotAt"))
    if quote_date is None and quote.get("quoteFresh") is True:
        quote_date = evaluation_date
    # A cached or undated quote may still be used for a non-actionable P/L
    # estimate, but it must never become a synthetic daily bar.
    if not quote_fresh or quote_date is None:
        return prepared
    quote_source = (
        quote.get("priceSource") or quote.get("quoteSource")
        or quote.get("marketDataSource") or "Shioaji portfolio quote"
    )
    synthetic = {
        "date": quote_date.isoformat(),
        "open": _number(quote.get("openPrice")) or current,
        "high": _number(quote.get("highPrice")) or current,
        "low": _number(quote.get("lowPrice")) or current,
        "close": current,
        "volume": _quote_volume_shares(quote),
        "price_source": quote_source,
    }
    if prepared and prepared[-1]["date"] == synthetic["date"]:
        merged = dict(prepared[-1])
        if _has_official_daily_volume(merged):
            synthetic["volume"] = None
        merged.update({key: value for key, value in synthetic.items() if value is not None})
        prepared[-1] = merged
    elif not prepared or prepared[-1]["date"] < synthetic["date"]:
        prepared.append(synthetic)
    return prepared


def _quote_is_fresh(quote: dict[str, Any], evaluation_date: dt.date) -> bool:
    explicit = quote.get("quoteFresh")
    if explicit is not None:
        return explicit is True
    stamp = _datetime(quote.get("snapshotAt"))
    quote_date = _date(quote.get("quoteDate")) or (stamp.date() if stamp else None)
    if quote_date != evaluation_date or stamp is None:
        return False
    now = dt.datetime.now()
    if evaluation_date != now.date():
        return (stamp.hour, stamp.minute) >= (13, 25)
    if now.time() >= dt.time(13, 30):
        return (stamp.hour, stamp.minute) >= (13, 25)
    if dt.time(8, 30) <= now.time() < dt.time(13, 30):
        age = (now - stamp).total_seconds()
        return -60 <= age <= 300
    return False


def _volume_is_final(quote: dict[str, Any], evaluation_date: dt.date) -> bool:
    explicit = quote.get("sessionFinal")
    if explicit is not None:
        return explicit is True
    stamp = _datetime(quote.get("snapshotAt"))
    if stamp is None:
        return False
    if stamp.date() < evaluation_date:
        return True
    return stamp.date() == evaluation_date and (stamp.hour, stamp.minute) >= (13, 25)


def _short_rebound_assessment(
    rows: list[dict[str, Any]],
    current: float,
    evaluation_date: dt.date,
    volume_final: bool,
) -> dict[str, Any]:
    """Score evidence that a short-term rebound structure has failed.

    This is a transparent rule score, not a calibrated probability. Intraday
    price may confirm a break, while multi-day structure and volume only use
    completed sessions.
    """

    completed = [
        row for row in rows
        if volume_final or (_date(row.get("date")) or evaluation_date) < evaluation_date
    ]
    latest_is_final = bool(
        volume_final
        and completed
        and _date(completed[-1].get("date")) == evaluation_date
    )
    prior_rows = completed[:-1] if latest_is_final else completed
    ma5 = _moving_average(completed, 5)
    previous_ma5 = _moving_average(completed, 5, offset=1)
    ma20 = _moving_average(completed, 20)
    previous_ma20 = _moving_average(completed, 20, offset=1)
    older_ma20 = _moving_average(completed, 20, offset=2)

    ma5_weak = bool(
        ma5 is not None
        and previous_ma5 is not None
        and current < ma5
        and ma5 < previous_ma5
    )
    trend_break = bool(
        ma5 is not None
        and ma20 is not None
        and current < ma20
        and (
            ma5 < ma20
            or (
                previous_ma20 is not None
                and ma20 < previous_ma20
            )
        )
    )
    persistent_ma20_down = bool(
        ma20 is not None
        and previous_ma20 is not None
        and older_ma20 is not None
        and current < ma20
        and ma20 < previous_ma20 < older_ma20
    )

    failed_rebound = False
    failed_rebound_date = None
    start = max(1, len(prior_rows) - 6)
    for index in range(len(prior_rows) - 1, start - 1, -1):
        rebound_close = _number(prior_rows[index].get("close"))
        previous_close = _number(prior_rows[index - 1].get("close"))
        if rebound_close is None or previous_close is None or rebound_close <= previous_close:
            continue
        rebound_low = _number(prior_rows[index].get("low")) or rebound_close
        failed_rebound = current < rebound_low
        failed_rebound_date = prior_rows[index].get("date") if failed_rebound else None
        break

    prior_five_closes = [
        value for row in prior_rows[-5:]
        if (value := _number(row.get("close"))) is not None
    ]
    prior_five_support = min(prior_five_closes) if len(prior_five_closes) >= 5 else None
    support_break = bool(prior_five_support is not None and current < prior_five_support)

    structure_rows = completed
    lower_high_low = False
    if len(structure_rows) >= 2:
        latest_high = _number(structure_rows[-1].get("high"))
        previous_high = _number(structure_rows[-2].get("high"))
        latest_low = _number(structure_rows[-1].get("low"))
        previous_low = _number(structure_rows[-2].get("low"))
        lower_high_low = bool(
            latest_high is not None
            and previous_high is not None
            and latest_low is not None
            and previous_low is not None
            and latest_high < previous_high
            and latest_low < previous_low
        )

    distribution = False
    distribution_ratio = None
    if latest_is_final and len(completed) >= 21:
        prior_volumes = [
            value for row in completed[-21:-1]
            if (value := _number(row.get("volume"))) is not None and value > 0
        ]
        latest_volume = _number(completed[-1].get("volume"))
        previous_close = _number(completed[-2].get("close"))
        average_volume = _mean(prior_volumes)
        if latest_volume is not None and average_volume:
            distribution_ratio = latest_volume / average_volume
            distribution = bool(
                previous_close is not None
                and current < previous_close
                and distribution_ratio >= 1.10
            )

    signal_rows = [
        {
            "key": "ma5Weak",
            "ok": ma5_weak,
            "weight": 20,
            "evidence": "現價低於 MA5，且 MA5 下彎",
        },
        {
            "key": "trendBreak",
            "ok": trend_break,
            "weight": 30,
            "evidence": (
                "現價跌破 MA20，且 MA5 已低於 MA20"
                if ma5 is not None and ma20 is not None and ma5 < ma20
                else "現價跌破 MA20，且 MA20 下彎"
            ),
        },
        {
            "key": "failedRebound",
            "ok": failed_rebound,
            "weight": 20,
            "evidence": "最近一次反彈後又跌破該反彈日低點",
        },
        {
            "key": "persistentMa20Down",
            "ok": persistent_ma20_down,
            "weight": 15,
            "evidence": "MA20 已連續兩個完成交易日下彎",
        },
        {
            "key": "supportBreak",
            "ok": support_break,
            "weight": 15,
            "evidence": "跌破前 5 個完成交易日收盤支撐",
        },
        {
            "key": "lowerHighLow",
            "ok": lower_high_low,
            "weight": 10,
            "evidence": "完成交易日高點與低點同步下移",
        },
        {
            "key": "distribution",
            "ok": distribution,
            "weight": 10,
            "evidence": "完整交易日帶量收低",
        },
    ]
    evidence = [signal["evidence"] for signal in signal_rows if signal["ok"]]
    score = min(100, sum(signal["weight"] for signal in signal_rows if signal["ok"]))
    weak = score >= SHORT_REBOUND_WEAK_SCORE and len(evidence) >= 2
    confirmed = bool(
        score >= SHORT_REBOUND_EXIT_SCORE
        and len(evidence) >= SHORT_REBOUND_MIN_EVIDENCE
        and trend_break
        and (failed_rebound or persistent_ma20_down)
    )
    return {
        "score": score,
        "level": "unlikely" if confirmed else "weak" if weak else "possible",
        "weak": weak,
        "confirmed": confirmed,
        "evidence": evidence,
        "evidenceCount": len(evidence),
        "calibratedProbability": False,
        "basis": "real_completed_daily_price_volume_rules",
        "completedRows": len(completed),
        "ma5": round(ma5, 4) if ma5 is not None else None,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "trendBreak": trend_break,
        "failedRebound": failed_rebound,
        "persistentMa20Down": persistent_ma20_down,
        "failedReboundDate": failed_rebound_date,
        "priorFiveSupport": round(prior_five_support, 4) if prior_five_support is not None else None,
        "distributionRatio": round(distribution_ratio, 4) if distribution_ratio is not None else None,
        "signals": {signal["key"]: signal["ok"] for signal in signal_rows},
    }


def _fundamental_deterioration(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    def source_for(row: dict[str, Any]) -> str:
        return " | ".join(dict.fromkeys(
            str(row.get(key) or "").strip()
            for key in ("financial_statement_source", "revenue_source", "finance_source")
            if str(row.get(key) or "").strip()
        ))

    def source_is_official(row: dict[str, Any]) -> bool:
        source = source_for(row).lower()
        return (
            bool(source)
            and any(word in source for word in OFFICIAL_FINANCE_SOURCE_WORDS)
            and not any(word in source for word in NON_OFFICIAL_SOURCE_WORDS)
        )

    snapshots = [
        row for row in rows
        if source_is_official(row)
        and any(_number(row.get(field)) is not None for field in FUNDAMENTAL_FIELDS)
    ]
    if not snapshots:
        return [], {"available": False, "source": ""}
    latest = snapshots[-1]
    latest_date = _date(latest.get("date"))
    comparison = None
    for row in reversed(snapshots[:-1]):
        row_date = _date(row.get("date"))
        if latest_date and row_date and (latest_date - row_date).days >= 45:
            comparison = row
            break
    if comparison is None and len(snapshots) >= 2:
        comparison = snapshots[max(0, len(snapshots) - 61)]
    if comparison is None:
        return [], {
            "available": False,
            "source": source_for(latest),
        }

    reasons: list[str] = []
    current_revenue = _number(latest.get("revenue_growth"))
    previous_revenue = _number(comparison.get("revenue_growth"))
    if current_revenue is not None and previous_revenue is not None and (
        (current_revenue < 0 <= previous_revenue) or current_revenue <= previous_revenue - 10
    ):
        reasons.append("營收年增率明顯惡化")

    for field, label, threshold in (
        ("gross_margin", "毛利率", 2.0),
        ("operating_margin", "營業利益率", 2.0),
        ("roe", "ROE", 2.0),
        ("operating_cashflow_ratio", "營業現金流比率", 5.0),
    ):
        current = _number(latest.get(field))
        previous = _number(comparison.get(field))
        if current is not None and previous is not None and current <= previous - threshold:
            reasons.append(f"{label}惡化")

    current_debt = _number(latest.get("debt_ratio"))
    previous_debt = _number(comparison.get("debt_ratio"))
    if current_debt is not None and previous_debt is not None and current_debt >= previous_debt + 5:
        reasons.append("負債比上升")

    return reasons, {
        "available": True,
        "latestDate": latest.get("date"),
        "comparisonDate": comparison.get("date"),
        "source": source_for(latest),
    }


def _base_result(
    lot: dict[str, Any], rows: list[dict[str, Any]], quote: dict[str, Any], evaluation_date: dt.date
) -> dict[str, Any]:
    horizon = strategy_horizon_info(lot.get("strategyHorizon") or lot.get("strategy_horizon"))
    entry_price = _number(lot.get("price") or lot.get("buyPrice")) or 0.0
    shares = max(0, int(_number(lot.get("shares")) or 0))
    current_price = _number(quote.get("currentPrice") or quote.get("price")) or 0.0
    buy_date = _date(lot.get("buyDate") or lot.get("buy_at") or lot.get("filled_at"))
    quote_fresh = _quote_is_fresh(quote, evaluation_date)
    raw_quote_at = quote.get("snapshotAt")
    quote_stamp = _datetime(raw_quote_at)
    data_date = _date(rows[-1].get("date")) if rows else None
    if data_date is None and quote_fresh:
        data_date = _date(quote.get("quoteDate") or quote.get("snapshotAt"))
    trading_days = None
    if buy_date is not None and data_date is not None:
        trading_days = len({
            row["date"] for row in rows
            if buy_date < (_date(row.get("date")) or buy_date) <= data_date
        })
    entry_cost_includes_buy_fee = bool(
        lot.get("entryCostIncludesBuyFee")
        or lot.get("entry_cost_includes_buy_fee")
    )
    entry_cost_amount = _number(
        lot.get("entryCostAmount") or lot.get("entry_cost_amount")
    )
    economics = (
        _position_economics(
            entry_price,
            current_price,
            shares,
            entry_cost_includes_buy_fee=entry_cost_includes_buy_fee,
            entry_cost_amount=entry_cost_amount,
        )
        if entry_price > 0 and current_price > 0 and shares > 0
        else {}
    )
    minimum_rows = int(horizon["minimumRows"])
    history_ready = len(rows) >= minimum_rows
    return {
        "policyVersion": POLICY_VERSION,
        "calculationRevision": CALCULATION_REVISION,
        "strategyHorizon": horizon["key"],
        "strategyHorizonLabel": horizon["label"],
        "strategyHorizonDays": horizon["holdingDays"],
        "strategyHorizonSource": lot.get("strategyHorizonSource") or lot.get("strategy_horizon_source") or "",
        "strategyHorizonLockedAt": lot.get("strategyHorizonLockedAt") or lot.get("strategy_horizon_locked_at"),
        "tradeId": lot.get("tradeId") or lot.get("id"),
        "buyDate": buy_date.isoformat() if buy_date else None,
        "buyDateKnown": buy_date is not None,
        "tradingDaysHeld": trading_days,
        "buyPrice": round(entry_price, 4) if entry_price > 0 else None,
        "entryCostIncludesBuyFee": entry_cost_includes_buy_fee,
        "currentPrice": round(current_price, 4) if current_price > 0 else None,
        "shares": shares,
        "dataDate": data_date.isoformat() if data_date else None,
        "quoteAt": quote_stamp.strftime("%Y-%m-%d %H:%M:%S") if quote_stamp else None,
        "quoteTimestampRaw": str(raw_quote_at) if raw_quote_at and not quote_stamp else None,
        "priceSource": quote.get("priceSource") or "Shioaji / 永豐庫存",
        "quoteFresh": quote_fresh,
        "historyRows": len(rows),
        "minimumHistoryRows": minimum_rows,
        "historyReady": history_ready,
        "dataReady": bool(entry_price > 0 and current_price > 0 and quote_fresh and history_ready),
        "type": "hold" if horizon["key"] != "unknown" else "unknown",
        "status": "續抱觀察" if horizon["key"] != "unknown" else "策略週期未知，只觀察",
        "note": "尚未達到出場條件" if horizon["key"] != "unknown" else "成交時未鎖定策略週期，不套用任何時間出場規則",
        "evidence": [],
        "conditions": [],
        "decisionVerified": False,
        "canNotify": False,
        "stopLoss": None,
        "takeProfit": None,
        "trailingStop": None,
        "confirmSellPrice": None,
        "ma5": _moving_average(rows, 5),
        "ma20": _moving_average(rows, 20),
        "ma60": _moving_average(rows, 60),
        "ma120": _moving_average(rows, 120),
        "swingHigh": None,
        "volumeRatio": None,
        "riskLineBreached": False,
        "shortReboundRiskScore": None,
        "shortReboundRiskLevel": None,
        "shortReboundEvidence": [],
        "shortReboundAssessmentBasis": None,
        "shortReboundCalibratedProbability": False,
        "targetEverReached": False,
        "profitContinuationIntact": False,
        "profitTrailBreached": False,
        "profitPeakDrawdownPct": None,
        **economics,
    }


def evaluate_exit_lot(
    lot: dict[str, Any],
    price_rows: Iterable[dict[str, Any]],
    quote: dict[str, Any],
    evaluation_date: dt.date | str | None = None,
) -> dict[str, Any]:
    """Evaluate one immutable FIFO BUY lot with its locked strategy horizon."""

    if isinstance(evaluation_date, str):
        evaluation_date = _date(evaluation_date)
    today = evaluation_date if isinstance(evaluation_date, dt.date) else dt.date.today()
    rows = _rows_with_quote(price_rows, quote or {}, today)
    result = _base_result(lot or {}, rows, quote or {}, today)
    horizon = result["strategyHorizon"]
    current = _number(result.get("currentPrice")) or 0.0
    entry = _number(result.get("buyPrice")) or 0.0
    quote_fresh = result["quoteFresh"] is True

    if horizon == "unknown":
        result["dataReady"] = False
        result["conditions"] = [
            {"label": "成交時策略週期已鎖定", "ok": False, "value": "未知，不得每日重新分類"},
            {"label": "時間停損", "ok": False, "value": "停用"},
        ]
        return result

    if entry <= 0 or current <= 0:
        result.update(status="資料不足，只觀察", note="缺少真實成交價或即時現價")
        return result

    gross_return = (current / entry - 1) * 100
    ma5 = _number(result.get("ma5"))
    ma20 = _number(result.get("ma20"))
    ma60 = _number(result.get("ma60"))
    ma120 = _number(result.get("ma120"))
    previous_ma20 = _moving_average(rows, 20, offset=1)
    previous_ma60 = _moving_average(rows, 60, offset=1)

    buy_date = _date(result.get("buyDate"))
    since_buy = [
        row for row in rows
        if buy_date is not None and (_date(row.get("date")) or today) >= buy_date
    ]
    peak_rows = since_buy
    swing_high = max(
        (_number(row.get("high")) or _number(row.get("close")) or 0.0 for row in peak_rows),
        default=0.0,
    )
    result["swingHigh"] = round(swing_high, 4) if swing_high > 0 else None

    if horizon == "short_trade":
        stop = entry * 0.93
        target = entry * 1.10
        target_reached = current >= target - max(0.0001, target * 1e-9)
        target_ever_reached = bool(
            target_reached
            or (result["buyDateKnown"] and swing_high >= target - max(0.0001, target * 1e-9))
        )
        profit_peak = swing_high if swing_high > 0 else current if target_reached else 0.0
        profit_trailing_stop = (
            max(entry * (1 + SHORT_PROFIT_FLOOR_PCT), profit_peak * (1 - SHORT_PROFIT_TRAIL_PCT))
            if target_ever_reached and profit_peak > 0
            else None
        )
        result.update(
            stopLoss=round(stop, 4),
            takeProfit=round(target, 4),
            trailingStop=round(profit_trailing_stop, 4) if profit_trailing_stop else None,
        )
        prior_volumes = [
            _number(row.get("volume")) for row in rows[-21:-1]
            if (_number(row.get("volume")) or 0) > 0
        ]
        avg_volume = _mean(prior_volumes)
        latest_volume = _number(rows[-1].get("volume")) if rows else None
        volume_ratio = latest_volume / avg_volume if latest_volume is not None and avg_volume else None
        result["volumeRatio"] = round(volume_ratio, 4) if volume_ratio is not None else None
        volume_final = _volume_is_final(quote or {}, today)
        day_down = len(rows) >= 2 and current < (_number(rows[-2].get("close")) or current)
        volume_weak = bool(
            volume_final
            and volume_ratio is not None
            and volume_ratio <= 0.75
            and (day_down or (ma5 is not None and current < ma5))
        )
        time_expired = bool(
            result["buyDateKnown"]
            and result["tradingDaysHeld"] is not None
            and result["tradingDaysHeld"] >= 10
            and not target_ever_reached
        )
        stop_reached = current <= stop + max(0.0001, stop * 1e-9)
        rebound = _short_rebound_assessment(rows, current, today, volume_final)
        rebound_weak = rebound["weak"] is True
        rebound_failed = rebound["confirmed"] is True
        profit_peak_drawdown_pct = (
            (current / profit_peak - 1) * 100
            if target_ever_reached and profit_peak > 0
            else None
        )
        profit_trail_breached = bool(
            target_ever_reached
            and profit_trailing_stop is not None
            and current <= profit_trailing_stop + max(0.0001, profit_trailing_stop * 1e-9)
        )
        profit_continuation_intact = bool(
            target_ever_reached
            and not rebound["trendBreak"]
            and (ma5 is None or current >= ma5)
        )
        profit_exit_ready = bool(
            target_ever_reached
            and profit_trail_breached
            and rebound_weak
            and rebound["trendBreak"]
        )
        time_exit_ready = bool(time_expired and rebound_weak and rebound["trendBreak"])
        volume_exit_ready = bool(volume_weak and rebound_weak and rebound["trendBreak"])
        result.update(
            riskLineBreached=stop_reached,
            shortReboundRiskScore=rebound["score"],
            shortReboundRiskLevel=rebound["level"],
            shortReboundEvidence=rebound["evidence"],
            shortReboundAssessmentBasis=rebound["basis"],
            shortReboundCalibratedProbability=False,
            shortReboundAssessment=rebound,
            targetEverReached=target_ever_reached,
            profitContinuationIntact=profit_continuation_intact,
            profitTrailBreached=profit_trail_breached,
            profitPeakDrawdownPct=(
                round(profit_peak_drawdown_pct, 4)
                if profit_peak_drawdown_pct is not None
                else None
            ),
        )
        result["conditions"] = [
            {
                "label": "+10% 達標只記錄，不直接賣出",
                "ok": target_ever_reached,
                "value": f"目前 {gross_return:.2f}%",
            },
            {
                "label": "達標後續漲結構",
                "ok": profit_continuation_intact,
                "value": "仍在，續抱" if profit_continuation_intact else "整理中或轉弱確認中",
            },
            {
                "label": "達標後高點回撤保護",
                "ok": profit_trail_breached,
                "value": (
                    f"保護價 {profit_trailing_stop:.2f}｜高點回撤 {profit_peak_drawdown_pct:.2f}%"
                    if profit_trailing_stop is not None and profit_peak_drawdown_pct is not None
                    else "尚未達標，不啟用"
                ),
            },
            {
                "label": "短期不易反彈風險",
                "ok": rebound_failed,
                "value": f"{rebound['score']}/100（規則分數，非機率）",
            },
            {
                "label": "20 日趨勢結構失守",
                "ok": rebound["trendBreak"],
                "value": "成立" if rebound["trendBreak"] else "未成立",
            },
            {
                "label": "-7% 風險線（只作輔助）",
                "ok": stop_reached,
                "value": f"{gross_return:.2f}%｜不單獨觸發賣出",
            },
            {
                "label": "完整收盤量縮且反彈偏弱",
                "ok": volume_exit_ready,
                "value": "盤中不使用未走完的成交量" if not volume_final else (f"{volume_ratio:.2f} 倍" if volume_ratio is not None else "無資料"),
            },
            {"label": "真實買進日", "ok": result["buyDateKnown"], "value": result["buyDate"] or "未知，禁止時間出場"},
            {
                "label": "10 日未達標且反彈偏弱",
                "ok": time_exit_ready,
                "value": str(result["tradingDaysHeld"] if result["tradingDaysHeld"] is not None else "停用"),
            },
        ]
        if target_ever_reached and profit_exit_ready:
            profitable_exit = (_number(result.get("estimatedNetPnl")) or 0) > 0
            result.update(
                type="phase1" if profitable_exit else "phase3",
                status=(
                    "達標後續漲結構轉弱"
                    if profitable_exit
                    else "達標後漲幅回吐且趨勢轉弱"
                ),
                note=(
                    "曾達 +10% 只代表目標完成；目前仍有獲利，但已跌破高點回撤保護線，且 20 日趨勢同步失守，才確認停利"
                    if profitable_exit
                    else "曾達 +10% 但獲利已回吐；目前已跌破高點回撤保護線，且 20 日趨勢同步失守，依短期走勢轉弱處理"
                ),
                evidence=[
                    "買進後曾達 +10%",
                    f"自買進後高點回撤 {abs(profit_peak_drawdown_pct or 0):.2f}% 並跌破保護價",
                    *rebound["evidence"][:2],
                ],
                confirmSellPrice=round(current, 4),
            )
        elif target_ever_reached:
            if profit_trail_breached:
                result.update(
                    status="達標後回檔，尚未確認轉弱，續抱",
                    note="已跌近或跌破高點回撤保護線，但 20 日趨勢與反彈弱勢尚未同時成立，不通知賣出",
                )
            elif profit_continuation_intact:
                result.update(
                    status="已達 +10%，續漲結構仍在",
                    note="+10% 只記錄策略達標；趨勢未轉弱，繼續持有，不通知賣出",
                )
            else:
                result.update(
                    status="已達 +10%，整理中續抱",
                    note="尚未同時跌破高點回撤保護線與 20 日趨勢，繼續持有，不通知賣出",
                )
        elif rebound_failed:
            result.update(
                type="phase3",
                status="短期反彈結構失敗",
                note="反彈風險規則分數達門檻，且 20 日趨勢結構已失守；不是只因跌破 -7% 出場",
                evidence=rebound["evidence"],
                confirmSellPrice=round(current, 4),
            )
        elif time_exit_ready:
            result.update(
                type="time_stop",
                status="短期逾時且反彈力弱",
                note="真實買進日起已滿 10 個交易日、尚未達 +10%，且反彈風險已達偏弱門檻",
                evidence=[
                    "FIFO 真實買進日已確認",
                    "10 個交易日仍未達 +10%",
                    *rebound["evidence"][:2],
                ],
                confirmSellPrice=round(current, 4),
            )
        elif volume_exit_ready:
            result.update(
                type="phase2",
                status="短期量價反彈力不足",
                note="完整交易日量能低於 20 日均量 75%，且另有多項反彈偏弱證據",
                evidence=[
                    "成交量縮至 20 日均量 75% 以下",
                    *rebound["evidence"][:2],
                ],
                confirmSellPrice=round(current, 4),
            )
        elif stop_reached:
            result.update(
                status="跌破 -7% 風險線，等待反彈結構確認",
                note="-7% 只作風險警示；反彈風險未達正式賣出門檻，不單獨通知賣出",
            )
        elif rebound_weak:
            result.update(
                status="短期反彈力偏弱，續觀察",
                note="已有部分反彈偏弱證據，但尚未同時失守 20 日趨勢結構",
            )
        else:
            result.update(
                status="短期仍有反彈結構",
                note="目前沒有足夠真實價量證據判定短期不易回漲",
            )
        if not result["buyDateKnown"] and result["type"] == "hold":
            result["note"] = f"{result['note']}；買進日未知，時間出場已停用"

    elif horizon == "mid_swing":
        buy_date_known = result["buyDateKnown"] is True
        trailing_candidates = [value for value in (ma20, ma60) if value is not None and value > 0]
        if buy_date_known and swing_high >= entry * 1.08:
            trailing_candidates.append(swing_high * 0.92)
        trailing = max(trailing_candidates) if buy_date_known and trailing_candidates else None
        result.update(
            stopLoss=None,
            takeProfit=round(swing_high, 4) if buy_date_known and swing_high > 0 else None,
            trailingStop=round(trailing, 4) if trailing else None,
        )
        below_ma20 = ma20 is not None and current < ma20
        below_ma60 = ma60 is not None and current < ma60
        ma20_down = ma20 is not None and previous_ma20 is not None and ma20 < previous_ma20
        ma60_down = ma60 is not None and previous_ma60 is not None and ma60 < previous_ma60
        trail_breached = buy_date_known and trailing is not None and current < trailing
        drawdown = (current / swing_high - 1) * 100 if buy_date_known and swing_high > 0 else None
        result["conditions"] = [
            {"label": "跌破 MA20", "ok": below_ma20, "value": round(ma20, 4) if ma20 else "無資料"},
            {"label": "跌破 MA60", "ok": below_ma60, "value": round(ma60, 4) if ma60 else "無資料"},
            {"label": "MA20 轉弱", "ok": ma20_down, "value": "下降" if ma20_down else "未下降"},
            {
                "label": "買進後波段高點回撤",
                "ok": drawdown is not None and drawdown <= -8,
                "value": f"{drawdown:.2f}%" if drawdown is not None else "買進日未知，停用",
            },
            {
                "label": "移動停利",
                "ok": trail_breached,
                "value": round(trailing, 4) if trailing else "買進日未知，停用" if not buy_date_known else "無資料",
            },
            {"label": "短線 10 日停損", "ok": False, "value": "中期策略不適用"},
        ]
        if below_ma60 and ma60_down and ma20 is not None and ma60 is not None and ma20 < ma60:
            result.update(
                type="phase3",
                status="中期趨勢反轉",
                note="跌破 MA60，且 MA20 已落在 MA60 下方、MA60 同步下彎",
                evidence=["收盤跌破 MA60", "MA20 低於 MA60", "MA60 轉弱"],
                confirmSellPrice=round(ma60, 4),
            )
        elif below_ma20 and ma20_down and (
            not buy_date_known or trail_breached or (drawdown is not None and drawdown <= -8)
        ):
            evidence = ["收盤跌破 MA20", "MA20 轉弱"]
            if buy_date_known:
                evidence.append("買進後波段高點回撤或移動停利被跌破")
            result.update(
                type="phase2",
                status="中期移動停利成立" if buy_date_known else "中期 MA20 趨勢轉弱",
                note=(
                    "波段高點回撤或跌破移動停利，且 MA20 已轉弱"
                    if buy_date_known
                    else "買進日未知，未使用歷史高點；僅依跌破 MA20 且 MA20 轉弱判斷"
                ),
                evidence=evidence,
                confirmSellPrice=round((trailing if buy_date_known else None) or ma20 or current, 4),
            )

    elif horizon == "long_trend":
        fundamental_reasons, fundamental = _fundamental_deterioration(rows)
        below_ma60 = ma60 is not None and current < ma60
        below_ma120 = ma120 is not None and current < ma120
        ma60_down = ma60 is not None and previous_ma60 is not None and ma60 < previous_ma60
        death_cross = ma60 is not None and ma120 is not None and ma60 < ma120
        trend_reasons = [
            reason for ok, reason in (
                (below_ma60, "收盤跌破 MA60"),
                (below_ma120, "收盤跌破 MA120"),
                (ma60_down, "MA60 轉弱"),
                (death_cross, "MA60 低於 MA120"),
            ) if ok
        ]
        result.update(
            stopLoss=None,
            takeProfit=None,
            trailingStop=round(ma60, 4) if ma60 else None,
            fundamental=fundamental,
            fundamentalDeterioration=fundamental_reasons,
        )
        result["conditions"] = [
            {"label": "跌破 MA60", "ok": below_ma60, "value": round(ma60, 4) if ma60 else "無資料"},
            {"label": "跌破 MA120", "ok": below_ma120, "value": round(ma120, 4) if ma120 else "無資料"},
            {"label": "MA60 轉弱", "ok": ma60_down, "value": "下降" if ma60_down else "未下降"},
            {"label": "基本面惡化", "ok": bool(fundamental_reasons), "value": "、".join(fundamental_reasons) or "未確認"},
            {"label": "短線 10 日停損", "ok": False, "value": "長期策略不適用"},
        ]
        if len(trend_reasons) >= 2 and fundamental_reasons:
            result.update(
                type="phase3",
                status="長期趨勢與基本面同步反轉",
                note="長期部位只在趨勢反轉且正式基本面同步惡化時出場",
                evidence=[*trend_reasons[:2], fundamental_reasons[0]],
                confirmSellPrice=round(ma60 or ma120 or current, 4),
            )
        elif len(trend_reasons) >= 2:
            result.update(status="長期趨勢轉弱，等待基本面確認", note="不因 10 日漲幅不足出場")
        elif fundamental_reasons:
            result.update(status="長期基本面轉弱，等待趨勢確認", note="不因單一基本面變化直接賣出")

    actionable = result["type"] in ACTIONABLE_TYPES
    result["decisionVerified"] = bool(actionable and result["dataReady"] and len(result["evidence"]) >= 2)
    result["canNotify"] = result["decisionVerified"]
    if actionable and not result["dataReady"]:
        result["status"] = f"{result['status']}，資料未完成只觀察"
        result["note"] = "出場條件已出現，但歷史資料或即時報價新鮮度未通過，不發通知"
    return result


def _alert_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    def plain_status() -> str:
        decision_type = str(item.get("type") or "")
        horizon = str(item.get("strategyHorizon") or "unknown")
        technical_status = str(item.get("status") or "")

        if "不足整張" in technical_status:
            return "符合賣出條件，但不足 1 張，不提醒"
        if horizon == "unknown":
            return "資料不足，先不要操作"
        if decision_type in ACTIONABLE_TYPES and item.get("dataReady") is not True:
            return "出現賣出訊號，但資料不完整，先不要賣"
        if decision_type == "phase1":
            return "獲利後走勢確認轉弱，建議停利"
        if decision_type == "time_stop":
            return "持有 10 天仍未達標，建議賣出"
        if decision_type == "phase2":
            return (
                "中期漲勢轉弱，建議賣出"
                if horizon == "mid_swing"
                else "反彈力道不足，建議賣出"
            )
        if decision_type == "phase3":
            return {
                "short_trade": "短期走勢轉弱，建議賣出",
                "mid_swing": "中期走勢轉壞，建議賣出",
                "long_trend": "長期走勢和公司表現都轉壞，建議賣出",
            }.get(horizon, "已達賣出條件，建議賣出")
        if item.get("dataReady") is not True:
            return "資料不完整，先觀察"
        if horizon == "short_trade":
            if item.get("riskLineBreached") is True:
                return "跌幅較大，但還沒確認要賣"
            if (_number(item.get("shortReboundRiskScore")) or 0) >= SHORT_REBOUND_WEAK_SCORE:
                return "反彈偏弱，先觀察"
        if horizon == "long_trend":
            if "基本面轉弱" in technical_status:
                return "公司表現轉弱，先觀察"
            if "趨勢轉弱" in technical_status:
                return "長期走勢轉弱，先觀察"
        return "目前沒有賣出訊號，繼續持有"

    broker_average = _number(item.get("brokerAveragePrice"))
    holding_price = broker_average if broker_average is not None and broker_average > 0 else item.get("buyPrice")
    user_status = plain_status()
    common = {
        "code": item.get("symbol"),
        "name": item.get("name") or item.get("symbol"),
        "period": item.get("strategyHorizonLabel"),
        "currentPrice": item.get("currentPrice"),
        "buyPrice": item.get("buyPrice"),
        "holdingPrice": holding_price,
        "holdingPriceSource": "broker_average" if broker_average is not None and broker_average > 0 else "selected_fifo_lot",
        "pnlRate": item.get("netPnlRate"),
        "grossPnlRate": item.get("grossReturnPct"),
        "estimatedNetPnl": item.get("estimatedNetPnl"),
        "estimatedExitCosts": item.get("estimatedExitCosts"),
        "shares": item.get("shares"),
        "priceSource": item.get("priceSource"),
        "dataDate": item.get("dataDate"),
        "dataReady": item.get("dataReady") is True,
        "decisionVerified": item.get("decisionVerified") is True,
        "decisionType": item.get("type"),
        "decisionReasons": item.get("evidence") or [],
        "decisionAt": item.get("generatedAt"),
        "decisionDate": item.get("decisionDate"),
        "strategyHorizon": item.get("strategyHorizon"),
        "strategyHorizonLabel": item.get("strategyHorizonLabel"),
        "strategyHorizonDays": item.get("strategyHorizonDays"),
        "buyDate": item.get("buyDate"),
        "buyDateKnown": item.get("buyDateKnown"),
        "sellShares": item.get("sellShares"),
        "policyVersion": item.get("policyVersion"),
        "technicalStatus": item.get("status"),
        "fullNote": item.get("note"),
        "conditions": item.get("conditions") or [],
        "historyRows": item.get("historyRows"),
        "minimumHistoryRows": item.get("minimumHistoryRows"),
        "quoteFresh": item.get("quoteFresh") is True,
        "tradingDaysHeld": item.get("tradingDaysHeld"),
        "riskLineBreached": item.get("riskLineBreached") is True,
        "shortReboundRiskScore": item.get("shortReboundRiskScore"),
        "shortReboundRiskLevel": item.get("shortReboundRiskLevel"),
        "shortReboundEvidence": item.get("shortReboundEvidence") or [],
        "shortReboundCalibratedProbability": False,
        "targetEverReached": item.get("targetEverReached") is True,
        "profitContinuationIntact": item.get("profitContinuationIntact") is True,
        "profitTrailBreached": item.get("profitTrailBreached") is True,
        "profitPeakDrawdownPct": item.get("profitPeakDrawdownPct"),
    }
    horizon = item.get("strategyHorizon")
    target_status = (
        user_status if item.get("type") in ACTIONABLE_TYPES
        else "已達 +10%，趨勢未轉弱就續抱" if horizon == "short_trade" and item.get("targetEverReached") is True
        else "+10% 只作達標記錄，不直接賣出" if horizon == "short_trade"
        else "中期漲勢轉弱時賣出" if horizon == "mid_swing"
        else "長期不設短線目標" if horizon == "long_trend"
        else "資料不足，無法設定目標"
    )
    defense_price = item.get("trailingStop") or item.get("stopLoss")
    defense_status = (
        "達標後須同時跌破高點保護線且趨勢轉弱才停利"
        if horizon == "short_trade" and item.get("targetEverReached") is True
        else "跌到 -7% 只提醒，不會直接叫賣" if horizon == "short_trade"
        else "中期走勢轉弱時才考慮賣出" if horizon == "mid_swing"
        else "長期走勢轉壞時才考慮賣出" if horizon == "long_trend"
        else "資料不足，不啟用賣出判斷"
    )
    confirm_status = user_status
    actionable = bool(
        item.get("decisionVerified") is True
        and _whole_lot_sell_shares(item.get("sellShares")) >= BOARD_LOT_SHARES
    )
    return [
        {
            **common,
            "key": f"{item.get('symbol')}-sell",
            "type": "賣出",
            "typeClass": "sell",
            "targetPrice": item.get("takeProfit"),
            "status": target_status,
            "note": item.get("note") if horizon == "short_trade" or item.get("type") == "phase1" else "依鎖定週期執行",
            "canNotify": bool(actionable and item.get("type") == "phase1"),
        },
        {
            **common,
            "key": f"{item.get('symbol')}-defense",
            "type": "防守價",
            "typeClass": "defense",
            "targetPrice": defense_price,
            "stopLossBase": item.get("stopLoss"),
            "status": defense_status,
            "note": "風險線只供輔助；短期出場以後端反彈結構與完整價量證據為準",
            "canNotify": False,
        },
        {
            **common,
            "key": f"{item.get('symbol')}-confirm",
            "type": "確認賣出",
            "typeClass": "confirm",
            "targetPrice": item.get("confirmSellPrice"),
            "status": confirm_status,
            "note": item.get("note"),
            "canNotify": bool(actionable and item.get("type") != "phase1"),
        },
    ]


def build_position_exit(
    symbol: str,
    name: str,
    holding: dict[str, Any],
    lots: Iterable[dict[str, Any]],
    price_rows: Iterable[dict[str, Any]],
    evaluation_date: dt.date | str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Evaluate reconciled FIFO lots and select the most urgent real lot."""

    quote = {
        "currentPrice": holding.get("currentPrice") or holding.get("current_price") or holding.get("lastPrice"),
        "openPrice": holding.get("openPrice") or holding.get("open_price"),
        "highPrice": holding.get("highPrice") or holding.get("high_price"),
        "lowPrice": holding.get("lowPrice") or holding.get("low_price"),
        "totalVolume": holding.get("totalVolume") or holding.get("volume"),
        "totalVolumeUnit": holding.get("totalVolumeUnit") or holding.get("volumeUnit"),
        "snapshotAt": holding.get("snapshotAt") or holding.get("updatedAt"),
        "quoteDate": holding.get("quoteDate"),
        "quoteFresh": holding.get("quoteFresh"),
        "sessionFinal": holding.get("sessionFinal"),
        "priceSource": holding.get("priceSource") or "Shioaji / 永豐庫存",
    }
    lot_list = [dict(lot) for lot in lots or [] if isinstance(lot, dict)]
    total_shares = max(0, int(_number(holding.get("shares")) or 0))
    if total_shares <= 0:
        quantity = _number(holding.get("quantity")) or 0
        total_shares = max(0, int(quantity * 1000))
    if not lot_list and total_shares > 0:
        lot_list = [{
            "shares": total_shares,
            "price": holding.get("price") or holding.get("avgPrice"),
            "strategyHorizon": "unknown",
            "strategyHorizonSource": "missing_fifo_trade",
            "buyDate": None,
        }]

    evaluations = [evaluate_exit_lot(lot, price_rows, quote, evaluation_date) for lot in lot_list]
    if not evaluations:
        evaluations = [evaluate_exit_lot({"strategyHorizon": "unknown"}, price_rows, quote, evaluation_date)]
    selected = max(
        evaluations,
        key=lambda item: (
            1 if item.get("decisionVerified") else 0,
            DECISION_PRIORITY.get(str(item.get("type")), 0),
            item.get("tradingDaysHeld") or -1,
        ),
    )
    result = dict(selected)
    selected_shares = max(0, int(_number(selected.get("shares")) or 0))
    sell_shares = _whole_lot_sell_shares(selected_shares) if selected.get("decisionVerified") else 0
    result.update({
        "symbol": str(symbol or "").strip(),
        "name": str(name or symbol or "").strip(),
        "generatedAt": generated_at or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decisionDate": (_date(evaluation_date) if isinstance(evaluation_date, str) else evaluation_date or dt.date.today()).isoformat(),
        "sellShares": sell_shares,
        "wholeLotSellOnly": True,
        "boardLotShares": BOARD_LOT_SHARES,
        "oddLotSharesExcluded": selected_shares - sell_shares if selected.get("decisionVerified") else 0,
        "positionShares": total_shares,
        "brokerAveragePrice": _number(holding.get("price") or holding.get("avgPrice")),
        "coveredShares": sum(int(item.get("shares") or 0) for item in evaluations if item.get("tradeId") is not None),
        "unknownShares": sum(int(item.get("shares") or 0) for item in evaluations if item.get("strategyHorizon") == "unknown"),
        "lotCount": len(evaluations),
        "mixedHorizons": len({item.get("strategyHorizon") for item in evaluations}) > 1,
        "positionBuyDateKnown": bool(evaluations) and all(item.get("buyDateKnown") is True for item in evaluations),
        "lots": [{
            "tradeId": item.get("tradeId"),
            "shares": item.get("shares"),
            "buyPrice": item.get("buyPrice"),
            "buyDate": item.get("buyDate"),
            "buyDateKnown": item.get("buyDateKnown"),
            "strategyHorizon": item.get("strategyHorizon"),
            "strategyHorizonLabel": item.get("strategyHorizonLabel"),
            "strategyHorizonSource": item.get("strategyHorizonSource"),
            "strategyHorizonLockedAt": item.get("strategyHorizonLockedAt"),
            "costBasisPrice": item.get("entryCostPrice") or item.get("buyPrice"),
            "type": item.get("type"),
            "status": item.get("status"),
            "decisionVerified": item.get("decisionVerified"),
            "shortReboundRiskScore": item.get("shortReboundRiskScore"),
            "shortReboundRiskLevel": item.get("shortReboundRiskLevel"),
            "riskLineBreached": item.get("riskLineBreached") is True,
        } for item in evaluations],
    })
    if selected.get("decisionVerified") and sell_shares < BOARD_LOT_SHARES:
        result.update(
            decisionVerified=False,
            canNotify=False,
            status=f"{selected.get('status') or '出場條件成立'}，不足整張只觀察",
            note="出場條件已成立，但依整張賣出偏好不足 1,000 股，不發通知也不建立零股賣單",
        )
    result["alerts"] = _alert_rows(result)
    return result


def exit_policy_payload() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "calculationRevision": CALCULATION_REVISION,
        "horizons": {
            "short_trade": {
                **HORIZON_CONFIG["short_trade"],
                "targetReturnPct": 10,
                "riskReferencePct": -7,
                "fixedStopIsPrimary": False,
                "riskReferenceAction": "warning_only_without_rebound_failure",
                "targetAction": "record_milestone_then_hold_while_trend_intact",
                "profitTrailPct": SHORT_PROFIT_TRAIL_PCT * 100,
                "profitFloorPct": SHORT_PROFIT_FLOOR_PCT * 100,
                "profitExit": "曾達 +10% 後，跌破高點回撤保護線，且反彈偏弱與 20 日趨勢失守同時成立",
                "timeExitSessions": 10,
                "timeExit": "從未達 +10% 且滿 10 個交易日、反彈風險達偏弱門檻並失守 20 日趨勢",
                "volumeExit": "完整交易日量縮、反彈風險達偏弱門檻且 20 日趨勢失守",
                "primaryExitBasis": "real_price_volume_rebound_structure",
                "reboundRisk": {
                    "weakScore": SHORT_REBOUND_WEAK_SCORE,
                    "exitScore": SHORT_REBOUND_EXIT_SCORE,
                    "minimumEvidence": SHORT_REBOUND_MIN_EVIDENCE,
                    "requiresTwentyDayTrendBreak": True,
                    "requiresFailedReboundOrPersistentMa20Down": True,
                    "calibratedProbability": False,
                },
            },
            "mid_swing": {
                **HORIZON_CONFIG["mid_swing"],
                "rules": ["MA20", "MA60", "波段高點", "移動停利"],
                "shortTimeStopDisabled": True,
            },
            "long_trend": {
                **HORIZON_CONFIG["long_trend"],
                "rules": ["MA60", "MA120", "基本面惡化", "趨勢反轉"],
                "shortTimeStopDisabled": True,
            },
        },
        "unknownBuyDateDisablesTimeExit": True,
        "horizonLockedAtBuyFill": True,
        "pnlBasis": "net_after_fees_tax_and_estimated_slippage",
        "sellQuantityPolicy": "whole_board_lots_only",
        "boardLotShares": BOARD_LOT_SHARES,
    }
