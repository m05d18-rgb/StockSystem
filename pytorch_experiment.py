"""Independent daily TCN experiment and fair tree-model comparison.

This module is deliberately outside the production radar path. It uses one
sample set, one chronological split, one execution policy, and one target
contract for TCN, XGBoost, and LightGBM. Results are observation-only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import random
import sqlite3
import sys
import traceback
import uuid

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    mean_absolute_error,
    precision_score,
    recall_score,
    roc_auc_score,
)

try:
    from xgboost import XGBClassifier, XGBRegressor
except Exception:
    XGBClassifier = None
    XGBRegressor = None

try:
    from lightgbm import LGBMClassifier, LGBMRegressor
except Exception:
    LGBMClassifier = None
    LGBMRegressor = None

from ml_backend import (
    MONSTER_TARGET_HORIZON_DAYS,
    MONSTER_TARGET_RETURN,
    PAPER_BASE_SLIPPAGE_RATE,
    PAPER_BUY_COMMISSION_RATE,
    PAPER_SELL_COMMISSION_RATE,
    PAPER_SELL_TAX_RATE,
    RADAR_STOP_LOSS_RETURN,
    radar_trade_policy_payload,
    simulate_radar_trade_path,
)


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "stock_system.sqlite3"
ARTIFACT_ROOT = ROOT / "model_experiments"
PYTORCH_VENV_PYTHON = ROOT / ".venv-pytorch" / "Scripts" / "python.exe"
EXPERIMENT_VERSION = "daily-tcn-multitarget-v1"

SEQUENCE_LENGTH = 60
MAX_FUTURE_HORIZON = 60
EMBARGO_SESSIONS = 60
DEFAULT_MAX_SYMBOLS = 400
DEFAULT_MAX_SAMPLES = 30_000
DEFAULT_EPOCHS = 5
MIN_GATE_SAMPLES = 20_000
MIN_GATE_TEST_SAMPLES = 3_000
MIN_GATE_SYMBOLS = 80
MAX_POSITIVE_RATE_DRIFT = 0.15

SEQUENCE_FEATURES = (
    "close_log_return",
    "open_gap_log_return",
    "high_to_close_log",
    "low_to_close_log",
    "close_location",
    "log_volume",
    "log_volume_ratio_20",
    "foreign_flow_to_volume",
    "trust_flow_to_volume",
    "margin_balance_change",
    "day_trade_ratio",
    "revenue_growth",
)

REGRESSION_TARGETS = (
    "net_return_10d",
    "mfe_10d",
    "mae_10d",
    "hold_days_10d",
    "net_return_5d",
    "net_return_20d",
    "net_return_60d",
)

INTRADAY_MIN_SESSIONS = 60
INTRADAY_MIN_BARS = 100_000
INTRADAY_MIN_SYMBOLS = 100
TFT_MIN_SESSIONS = 120
ORDER_BOOK_MIN_SESSIONS = 60
ORDER_BOOK_MIN_ROWS = 120_000
ORDER_BOOK_MIN_SYMBOLS = 40


def now_text():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback_value):
        try:
            return super().__exit__(exc_type, exc_value, traceback_value)
        finally:
            self.close()


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(str(db_path), timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_experiment_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_experiment_runs (
            run_id TEXT PRIMARY KEY,
            experiment_version TEXT NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'observation',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            data_max_date TEXT,
            sample_count INTEGER NOT NULL DEFAULT 0,
            train_count INTEGER NOT NULL DEFAULT 0,
            validation_count INTEGER NOT NULL DEFAULT 0,
            test_count INTEGER NOT NULL DEFAULT 0,
            config_json TEXT,
            target_json TEXT,
            split_json TEXT,
            metrics_json TEXT,
            comparison_json TEXT,
            gate_json TEXT,
            artifact_path TEXT,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_experiment_predictions (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            actual_hit INTEGER NOT NULL,
            actual_net_return REAL NOT NULL,
            tcn_probability REAL,
            xgboost_probability REAL,
            lightgbm_probability REAL,
            tcn_net_return REAL,
            xgboost_net_return REAL,
            lightgbm_net_return REAL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, symbol, signal_date),
            FOREIGN KEY (run_id) REFERENCES model_experiment_runs(run_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_model_experiment_predictions_date
        ON model_experiment_predictions(run_id, signal_date)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intraday_minute_bars (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            minute TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume_lots REAL NOT NULL DEFAULT 0,
            cumulative_volume_lots REAL NOT NULL DEFAULT 0,
            active_buy_volume_lots REAL NOT NULL DEFAULT 0,
            active_sell_volume_lots REAL NOT NULL DEFAULT 0,
            unknown_volume_lots REAL NOT NULL DEFAULT 0,
            active_buy_amount REAL NOT NULL DEFAULT 0,
            active_sell_amount REAL NOT NULL DEFAULT 0,
            unknown_amount REAL NOT NULL DEFAULT 0,
            large_buy_volume_lots REAL NOT NULL DEFAULT 0,
            large_sell_volume_lots REAL NOT NULL DEFAULT 0,
            raw_tick_count INTEGER NOT NULL DEFAULT 0,
            directional_tick_count INTEGER NOT NULL DEFAULT 0,
            unknown_tick_count INTEGER NOT NULL DEFAULT 0,
            first_tick_at TEXT,
            last_tick_at TEXT,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, date, minute)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intraday_minute_bars_date_symbol
        ON intraday_minute_bars(date, symbol)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_book_5m_features (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            minute TEXT NOT NULL,
            observation_count INTEGER NOT NULL DEFAULT 0,
            spread_observation_count INTEGER NOT NULL DEFAULT 0,
            avg_bid_depth_lots REAL NOT NULL DEFAULT 0,
            avg_ask_depth_lots REAL NOT NULL DEFAULT 0,
            avg_imbalance REAL NOT NULL DEFAULT 0,
            min_imbalance REAL NOT NULL DEFAULT 0,
            max_imbalance REAL NOT NULL DEFAULT 0,
            last_imbalance REAL NOT NULL DEFAULT 0,
            avg_spread_pct REAL,
            max_spread_pct REAL,
            last_spread_pct REAL,
            avg_microprice_gap_pct REAL,
            last_microprice_gap_pct REAL,
            last_best_bid REAL,
            last_best_ask REAL,
            net_bid_volume_change_lots REAL NOT NULL DEFAULT 0,
            net_ask_volume_change_lots REAL NOT NULL DEFAULT 0,
            first_snapshot_at TEXT,
            last_snapshot_at TEXT,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, date, minute)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_order_book_5m_date_symbol
        ON order_book_5m_features(date, symbol)
    """)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value, default):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        output = float(value)
        return output if math.isfinite(output) else default
    except (TypeError, ValueError):
        return default


def _clip(value, lower, upper):
    return max(lower, min(upper, _safe_float(value)))


def _official_source(value):
    text = str(value or "").lower()
    official = ("twse", "tpex", "finmind", "shioaji", "sinopac", "永豐")
    excluded = ("yahoo", "fallback", "simulate", "simulation", "estimate", "推估")
    return any(token in text for token in official) and not any(token in text for token in excluded)


def select_point_in_time_universe(conn, max_symbols=DEFAULT_MAX_SYMBOLS, history_sessions=900, seed=20260710):
    dates = [str(row[0]) for row in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT ?", (history_sessions,)
    ).fetchall()]
    if not dates:
        return [], None
    cutoff = min(dates)
    rows = conn.execute(
        """
        SELECT p.symbol, COUNT(*) AS sessions,
               COALESCE(s.name, '') AS name,
               COALESCE(s.sector, '') AS sector
        FROM prices p
        LEFT JOIN stock_info s ON s.symbol = p.symbol
        WHERE p.date >= ?
          AND length(p.symbol) = 4
          AND p.symbol GLOB '[0-9][0-9][0-9][0-9]'
          AND p.close > 0 AND p.volume > 0
          AND lower(COALESCE(s.sector, '')) NOT LIKE '%etf%'
          AND lower(COALESCE(s.sector, '')) NOT LIKE '%指數股票型基金%'
          AND lower(COALESCE(s.name, '')) NOT LIKE '%etf%'
        GROUP BY p.symbol
        HAVING COUNT(*) >= ?
        """,
        (cutoff, min(420, max(300, int(len(dates) * 0.55)))),
    ).fetchall()
    # A latest-turnover ranking leaks future universe membership into historical
    # evaluation. Use a deterministic hash sample that is independent of price
    # performance, then apply point-in-time liquidity at each signal date.
    candidates = [str(row["symbol"]) for row in rows]
    candidates.sort(
        key=lambda symbol: hashlib.sha256(f"{seed}:{symbol}".encode("ascii")).digest()
    )
    return candidates[:max(1, int(max_symbols))], max(dates)


def load_symbol_rows(conn, symbols, history_sessions=900):
    if not symbols:
        return {}
    cutoff_row = conn.execute(
        "SELECT MIN(date) FROM (SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT ?)",
        (history_sessions,),
    ).fetchone()
    cutoff = str(cutoff_row[0] or "") if cutoff_row else ""
    output = {symbol: [] for symbol in symbols}
    for offset in range(0, len(symbols), 300):
        chunk = symbols[offset:offset + 300]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT symbol, date, open, high, low, close, volume,
                   foreign_buy_sell, trust_buy_sell, margin_balance,
                   day_trade_ratio, revenue_growth, price_source
            FROM prices
            WHERE symbol IN ({placeholders}) AND date >= ?
            ORDER BY symbol, date
            """,
            (*chunk, cutoff),
        ).fetchall()
        for row in rows:
            output[str(row["symbol"])].append(dict(row))
    return output


def sequence_feature_matrix(rows):
    count = len(rows)
    features = np.zeros((count, len(SEQUENCE_FEATURES)), dtype=np.float32)
    volumes = np.asarray([max(0.0, _safe_float(row.get("volume"))) for row in rows], dtype=np.float64)
    margins = np.asarray([max(0.0, _safe_float(row.get("margin_balance"))) for row in rows], dtype=np.float64)
    official = np.asarray([1 if _official_source(row.get("price_source")) else 0 for row in rows], dtype=np.int8)
    for index, row in enumerate(rows):
        close = max(0.01, _safe_float(row.get("close"), 0.01))
        open_price = max(0.01, _safe_float(row.get("open"), close))
        high = max(close, open_price, _safe_float(row.get("high"), close))
        low = max(0.01, min(close, open_price, _safe_float(row.get("low"), close)))
        previous_close = close if index == 0 else max(0.01, _safe_float(rows[index - 1].get("close"), close))
        previous_margin = margins[index - 1] if index else margins[index]
        volume = volumes[index]
        volume_start = max(0, index - 20)
        past_volume = volumes[volume_start:index]
        average_volume = float(np.mean(past_volume)) if len(past_volume) else max(volume, 1.0)
        price_range = max(high - low, close * 0.001)
        foreign = _safe_float(row.get("foreign_buy_sell"))
        trust = _safe_float(row.get("trust_buy_sell"))
        margin_change = (margins[index] - previous_margin) / max(previous_margin, 1.0)
        day_trade = _safe_float(row.get("day_trade_ratio"))
        if abs(day_trade) > 2:
            day_trade /= 100.0
        revenue_growth = _safe_float(row.get("revenue_growth"))
        if abs(revenue_growth) > 3:
            revenue_growth /= 100.0
        features[index] = (
            _clip(math.log(close / previous_close), -0.25, 0.25),
            _clip(math.log(open_price / previous_close), -0.25, 0.25),
            _clip(math.log(high / close), 0.0, 0.25),
            _clip(math.log(low / close), -0.25, 0.0),
            _clip((close - low) / price_range - 0.5, -0.5, 0.5),
            math.log1p(volume),
            _clip(math.log(max(volume, 1.0) / max(average_volume, 1.0)), -4.0, 4.0),
            _clip(foreign / max(volume, 1.0), -5.0, 5.0),
            _clip(trust / max(volume, 1.0), -5.0, 5.0),
            _clip(margin_change, -0.5, 0.5),
            _clip(day_trade, 0.0, 1.5),
            _clip(revenue_growth, -3.0, 3.0),
        )
    return features, official


def cost_aware_horizon_return(entry_fill, exit_close):
    entry_cost = entry_fill * (1 + PAPER_BUY_COMMISSION_RATE)
    exit_fill = exit_close * (1 - PAPER_BASE_SLIPPAGE_RATE)
    proceeds = exit_fill * (1 - PAPER_SELL_COMMISSION_RATE - PAPER_SELL_TAX_RATE)
    return proceeds / entry_cost - 1


def build_multitarget(rows, signal_index):
    if signal_index + MAX_FUTURE_HORIZON >= len(rows):
        return None
    entry_row = rows[signal_index + 1]
    raw_open = _safe_float(entry_row.get("open")) or _safe_float(entry_row.get("close"))
    if raw_open <= 0:
        return None
    entry_fill = raw_open * (1 + PAPER_BASE_SLIPPAGE_RATE)
    future_10 = rows[signal_index + 1:signal_index + 1 + MONSTER_TARGET_HORIZON_DAYS]
    outcome = simulate_radar_trade_path(entry_fill, future_10)
    if not outcome or not outcome.get("settled") or outcome.get("netReturn") is None:
        return None
    horizon_returns = {}
    for horizon in (5, 20, 60):
        exit_close = _safe_float(rows[signal_index + horizon].get("close"))
        if exit_close <= 0:
            return None
        horizon_returns[horizon] = cost_aware_horizon_return(entry_fill, exit_close)
    regressions = np.asarray(
        [
            _safe_float(outcome.get("netReturn")),
            _safe_float(outcome.get("maxFavorable")),
            _safe_float(outcome.get("maxAdverse")),
            _safe_float(outcome.get("holdDays"), MONSTER_TARGET_HORIZON_DAYS),
            horizon_returns[5],
            horizon_returns[20],
            horizon_returns[60],
        ],
        dtype=np.float32,
    )
    return int(bool(outcome.get("targetHit"))), regressions


def _reservoir_append(reservoir, item, seen, limit, rng):
    if len(reservoir) < limit:
        reservoir.append(item)
        return
    replacement = rng.randrange(seen)
    if replacement < limit:
        reservoir[replacement] = item


def build_dataset(
    db_path=DB_PATH,
    max_symbols=DEFAULT_MAX_SYMBOLS,
    max_samples=DEFAULT_MAX_SAMPLES,
    sequence_length=SEQUENCE_LENGTH,
    stride=2,
    seed=20260710,
):
    rng = random.Random(seed)
    reservoir = []
    seen = 0
    with connect(db_path) as conn:
        symbols, data_max_date = select_point_in_time_universe(
            conn, max_symbols=max_symbols, seed=seed
        )
        rows_by_symbol = load_symbol_rows(conn, symbols)
    for symbol in symbols:
        rows = rows_by_symbol.get(symbol) or []
        if len(rows) < sequence_length + MAX_FUTURE_HORIZON + 20:
            continue
        matrix, official = sequence_feature_matrix(rows)
        official_cumulative = np.concatenate(([0], np.cumsum(official, dtype=np.int64)))
        invalid_transition = np.zeros(len(rows), dtype=np.int8)
        for transition_index in range(1, len(rows)):
            previous_close = _safe_float(rows[transition_index - 1].get("close"))
            current_close = _safe_float(rows[transition_index].get("close"))
            if previous_close <= 0 or current_close <= 0:
                invalid_transition[transition_index] = 1
            elif abs(math.log(current_close / previous_close)) > math.log(1.25):
                # Taiwan's normal daily price limit is far below 25%. Larger
                # discontinuities are usually split/reduction/unadjusted-price
                # artifacts and must not become fake long-horizon alpha.
                invalid_transition[transition_index] = 1
        invalid_cumulative = np.concatenate(([0], np.cumsum(invalid_transition, dtype=np.int64)))
        for index in range(sequence_length - 1, len(rows) - MAX_FUTURE_HORIZON, max(1, stride)):
            start = index - sequence_length + 1
            official_count = int(official_cumulative[index + 1] - official_cumulative[start])
            if official_count < math.ceil(sequence_length * 0.90) or not official[index]:
                continue
            future_official_count = int(
                official_cumulative[index + MAX_FUTURE_HORIZON + 1]
                - official_cumulative[index + 1]
            )
            if future_official_count < math.ceil(MAX_FUTURE_HORIZON * 0.90):
                continue
            invalid_count = int(
                invalid_cumulative[index + MAX_FUTURE_HORIZON + 1]
                - invalid_cumulative[start + 1]
            )
            if invalid_count:
                continue
            signal_close = _safe_float(rows[index].get("close"))
            signal_volume = _safe_float(rows[index].get("volume"))
            point_in_time_rows = rows[max(0, index - 20):index]
            average_volume = float(np.mean([
                _safe_float(item.get("volume")) for item in point_in_time_rows
            ])) if point_in_time_rows else 0.0
            average_turnover = float(np.mean([
                _safe_float(item.get("close")) * _safe_float(item.get("volume"))
                for item in point_in_time_rows
            ])) if point_in_time_rows else 0.0
            if (
                signal_close <= 0
                or signal_volume <= 0
                or signal_close * signal_volume < 10_000_000
                or average_volume < 300_000
                or average_turnover < 30_000_000
            ):
                continue
            target = build_multitarget(rows, index)
            if target is None:
                continue
            hit, regressions = target
            seen += 1
            item = (
                matrix[start:index + 1].copy(),
                hit,
                regressions,
                symbol,
                str(rows[index].get("date") or ""),
            )
            _reservoir_append(reservoir, item, seen, max(1, int(max_samples)), rng)
    if not reservoir:
        raise RuntimeError("no eligible official daily samples were built")
    reservoir.sort(key=lambda item: (item[4], item[3]))
    x = np.stack([item[0] for item in reservoir]).astype(np.float32)
    y_hit = np.asarray([item[1] for item in reservoir], dtype=np.float32)
    y_reg = np.stack([item[2] for item in reservoir]).astype(np.float32)
    sample_symbols = np.asarray([item[3] for item in reservoir])
    sample_dates = np.asarray([item[4] for item in reservoir])
    return {
        "x": x,
        "y_hit": y_hit,
        "y_reg": y_reg,
        "symbols": sample_symbols,
        "dates": sample_dates,
        "universe": symbols,
        "sampledSymbolCount": len(set(sample_symbols.tolist())),
        "eligibleBeforeCap": seen,
        "dataMaxDate": data_max_date,
    }


def chronological_split(dates, embargo_sessions=EMBARGO_SESSIONS):
    unique_dates = sorted({str(value) for value in dates})
    if len(unique_dates) < 420:
        raise RuntimeError(
            f"need at least 420 distinct signal sessions for a 60-session purged split; got {len(unique_dates)}"
        )
    test_days = min(120, max(60, int(len(unique_dates) * 0.16)))
    validation_days = min(80, max(40, int(len(unique_dates) * 0.10)))
    train_end = len(unique_dates) - test_days - embargo_sessions - validation_days - embargo_sessions
    if train_end < 180:
        raise RuntimeError("not enough pre-embargo training sessions")
    validation_start = train_end + embargo_sessions
    validation_end = validation_start + validation_days
    test_start = validation_end + embargo_sessions
    train_set = set(unique_dates[:train_end])
    validation_set = set(unique_dates[validation_start:validation_end])
    test_set = set(unique_dates[test_start:])
    train_mask = np.asarray([str(value) in train_set for value in dates])
    validation_mask = np.asarray([str(value) in validation_set for value in dates])
    test_mask = np.asarray([str(value) in test_set for value in dates])
    if not train_mask.any() or not validation_mask.any() or not test_mask.any():
        raise RuntimeError("chronological split produced an empty partition")
    split = {
        "method": "chronological_purged_holdout",
        "embargoSessions": embargo_sessions,
        "trainStart": unique_dates[0],
        "trainEnd": unique_dates[train_end - 1],
        "validationStart": unique_dates[validation_start],
        "validationEnd": unique_dates[validation_end - 1],
        "testStart": unique_dates[test_start],
        "testEnd": unique_dates[-1],
        "droppedEmbargoSessions": embargo_sessions * 2,
    }
    return train_mask, validation_mask, test_mask, split


def normalize_from_training(x, y_reg, train_mask):
    feature_mean = x[train_mask].mean(axis=(0, 1), dtype=np.float64).astype(np.float32)
    feature_std = x[train_mask].std(axis=(0, 1), dtype=np.float64).astype(np.float32)
    feature_std = np.where(feature_std < 1e-6, 1.0, feature_std).astype(np.float32)
    normalized_x = np.clip((x - feature_mean[None, None, :]) / feature_std[None, None, :], -8, 8)
    target_mean = y_reg[train_mask].mean(axis=0, dtype=np.float64).astype(np.float32)
    target_std = y_reg[train_mask].std(axis=0, dtype=np.float64).astype(np.float32)
    target_std = np.where(target_std < 1e-6, 1.0, target_std).astype(np.float32)
    normalized_y = np.clip((y_reg - target_mean[None, :]) / target_std[None, :], -10, 10)
    return normalized_x.astype(np.float32), normalized_y.astype(np.float32), {
        "featureMean": feature_mean,
        "featureStd": feature_std,
        "targetMean": target_mean,
        "targetStd": target_std,
    }


def choose_threshold(y_true, probabilities):
    best = (0.5, -1.0, -1.0)
    for threshold in np.arange(0.20, 0.81, 0.02):
        predicted = probabilities >= threshold
        if not predicted.any():
            continue
        f1 = f1_score(y_true, predicted, zero_division=0)
        precision = precision_score(y_true, predicted, zero_division=0)
        candidate = (float(threshold), float(f1), float(precision))
        if (candidate[1], candidate[2], candidate[0]) > (best[1], best[2], best[0]):
            best = candidate
    return best[0]


def _trading_metrics(probabilities, y_hit, y_net, dates, threshold):
    picks = []
    for date in sorted({str(value) for value in dates}):
        indexes = np.where(dates == date)[0]
        ranked = sorted(indexes, key=lambda index: float(probabilities[index]), reverse=True)
        selected = [index for index in ranked[:5] if probabilities[index] >= threshold]
        picks.extend(selected)
    if not picks:
        return {
            "dailyTop5Trades": 0,
            "dailyTop5Precision": None,
            "dailyTop5AvgNetReturn": None,
            "dailyTop5ProfitFactor": None,
            "dailyTop5MaxDrawdown": None,
        }
    returns = [float(y_net[index]) for index in picks]
    wins = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    daily = {}
    for index in picks:
        daily.setdefault(str(dates[index]), []).append(float(y_net[index]))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for date in sorted(daily):
        equity *= 1 + float(np.mean(daily[date]))
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    return {
        "dailyTop5Trades": len(picks),
        "dailyTop5Precision": round(float(np.mean(y_hit[picks])), 4),
        "dailyTop5AvgNetReturn": round(float(np.mean(returns)), 5),
        "dailyTop5ProfitFactor": round(wins / losses, 4) if losses > 0 else None,
        "dailyTop5MaxDrawdown": round(max_drawdown, 5),
    }


def evaluate_predictions(y_hit, y_net, dates, probabilities, predicted_net, threshold):
    predicted = probabilities >= threshold
    try:
        auc = roc_auc_score(y_hit, probabilities)
    except ValueError:
        auc = None
    metrics = {
        "threshold": round(float(threshold), 4),
        "samples": int(len(y_hit)),
        "positiveRate": round(float(np.mean(y_hit)), 4),
        "auc": round(float(auc), 4) if auc is not None else None,
        "accuracy": round(float(accuracy_score(y_hit, predicted)), 4),
        "precision": round(float(precision_score(y_hit, predicted, zero_division=0)), 4),
        "recall": round(float(recall_score(y_hit, predicted, zero_division=0)), 4),
        "f1": round(float(f1_score(y_hit, predicted, zero_division=0)), 4),
        "selectionRate": round(float(np.mean(predicted)), 4),
        "netReturnMae": round(float(mean_absolute_error(y_net, predicted_net)), 5),
    }
    metrics.update(_trading_metrics(probabilities, y_hit, y_net, dates, threshold))
    return metrics


def train_tcn(x, y_hit, normalized_y, train_mask, validation_mask, epochs, seed):
    try:
        import torch
        from torch import nn
        from torch.utils.data import DataLoader, TensorDataset
    except Exception as exc:
        raise RuntimeError(
            f"PyTorch is unavailable in {sys.executable}; use {PYTORCH_VENV_PYTHON}"
        ) from exc

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(max(1, min(12, os.cpu_count() or 1)))

    class CausalBlock(nn.Module):
        def __init__(self, channels, dilation, dropout=0.12):
            super().__init__()
            padding = (3 - 1) * dilation
            self.padding = padding
            self.conv1 = nn.Conv1d(channels, channels, 3, padding=padding, dilation=dilation)
            self.conv2 = nn.Conv1d(channels, channels, 3, padding=padding, dilation=dilation)
            self.activation = nn.GELU()
            self.dropout = nn.Dropout(dropout)
            self.norm1 = nn.GroupNorm(1, channels)
            self.norm2 = nn.GroupNorm(1, channels)

        def causal(self, value, layer):
            output = layer(value)
            return output[:, :, :-self.padding] if self.padding else output

        def forward(self, value):
            residual = value
            value = self.dropout(self.activation(self.norm1(self.causal(value, self.conv1))))
            value = self.dropout(self.activation(self.norm2(self.causal(value, self.conv2))))
            return value + residual

    class DailyTCN(nn.Module):
        def __init__(self, feature_count, regression_count):
            super().__init__()
            hidden = 48
            self.input_projection = nn.Conv1d(feature_count, hidden, 1)
            self.blocks = nn.Sequential(*(CausalBlock(hidden, dilation) for dilation in (1, 2, 4, 8)))
            self.shared = nn.Sequential(nn.Linear(hidden, 64), nn.GELU(), nn.Dropout(0.12))
            self.hit_head = nn.Linear(64, 1)
            self.regression_head = nn.Linear(64, regression_count)

        def forward(self, value):
            value = value.transpose(1, 2)
            value = self.blocks(self.input_projection(value))[:, :, -1]
            shared = self.shared(value)
            return self.hit_head(shared).squeeze(1), self.regression_head(shared)

    model = DailyTCN(x.shape[2], normalized_y.shape[1])
    train_indexes = np.where(train_mask)[0]
    validation_indexes = np.where(validation_mask)[0]
    positives = max(1.0, float(y_hit[train_mask].sum()))
    negatives = max(1.0, float(len(train_indexes) - positives))
    pos_weight = min(8.0, negatives / positives)
    bce = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight, dtype=torch.float32))
    regression_loss = nn.SmoothL1Loss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0015, weight_decay=0.0005)
    train_data = TensorDataset(
        torch.from_numpy(x[train_indexes]),
        torch.from_numpy(y_hit[train_indexes]),
        torch.from_numpy(normalized_y[train_indexes]),
    )
    loader = DataLoader(train_data, batch_size=256, shuffle=True, num_workers=0)
    validation_x = torch.from_numpy(x[validation_indexes])
    validation_hit = torch.from_numpy(y_hit[validation_indexes])
    validation_reg = torch.from_numpy(normalized_y[validation_indexes])
    best_state = None
    best_loss = float("inf")
    stale_epochs = 0
    history = []
    for epoch in range(max(1, int(epochs))):
        model.train()
        total_loss = 0.0
        total_rows = 0
        for batch_x, batch_hit, batch_reg in loader:
            optimizer.zero_grad(set_to_none=True)
            hit_logits, reg_output = model(batch_x)
            loss = bce(hit_logits, batch_hit) + 0.55 * regression_loss(reg_output, batch_reg)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
            total_loss += float(loss.item()) * len(batch_x)
            total_rows += len(batch_x)
        model.eval()
        validation_total = 0.0
        with torch.no_grad():
            for start in range(0, len(validation_x), 1024):
                hit_logits, reg_output = model(validation_x[start:start + 1024])
                loss = bce(hit_logits, validation_hit[start:start + 1024])
                loss += 0.55 * regression_loss(reg_output, validation_reg[start:start + 1024])
                validation_total += float(loss.item()) * len(hit_logits)
        validation_average = validation_total / max(1, len(validation_x))
        history.append({
            "epoch": epoch + 1,
            "trainLoss": round(total_loss / max(1, total_rows), 6),
            "validationLoss": round(validation_average, 6),
        })
        if validation_average < best_loss - 1e-5:
            best_loss = validation_average
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= 2:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    return model, history, torch


def torch_predict(model, torch, x):
    model.eval()
    probabilities = []
    regressions = []
    with torch.no_grad():
        for start in range(0, len(x), 1024):
            logits, values = model(torch.from_numpy(x[start:start + 1024]))
            probabilities.append(torch.sigmoid(logits).cpu().numpy())
            regressions.append(values.cpu().numpy())
    return np.concatenate(probabilities), np.concatenate(regressions)


def _tree_sample_weights(y):
    positives = max(1.0, float(y.sum()))
    negative = max(1.0, float(len(y) - positives))
    positive_weight = min(8.0, negative / positives)
    return np.where(y > 0.5, positive_weight, 1.0).astype(np.float32)


def train_tree_baselines(x_train, y_hit_train, y_net_train, seed):
    models = {}
    weights = _tree_sample_weights(y_hit_train)
    if XGBClassifier is not None and XGBRegressor is not None:
        classifier = XGBClassifier(
            n_estimators=180,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.82,
            colsample_bytree=0.70,
            min_child_weight=8,
            reg_lambda=2.0,
            tree_method="hist",
            n_jobs=max(1, min(12, os.cpu_count() or 1)),
            random_state=seed,
            eval_metric="logloss",
        )
        regressor = XGBRegressor(
            n_estimators=150,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.82,
            colsample_bytree=0.70,
            min_child_weight=8,
            reg_lambda=2.0,
            tree_method="hist",
            n_jobs=max(1, min(12, os.cpu_count() or 1)),
            random_state=seed,
            objective="reg:squarederror",
        )
        classifier.fit(x_train, y_hit_train, sample_weight=weights)
        regressor.fit(x_train, y_net_train)
        models["xgboost"] = (classifier, regressor)
    if LGBMClassifier is not None and LGBMRegressor is not None:
        classifier = LGBMClassifier(
            n_estimators=220,
            num_leaves=31,
            max_depth=-1,
            learning_rate=0.04,
            subsample=0.82,
            colsample_bytree=0.70,
            min_child_samples=40,
            reg_lambda=2.0,
            n_jobs=max(1, min(12, os.cpu_count() or 1)),
            random_state=seed,
            verbosity=-1,
        )
        regressor = LGBMRegressor(
            n_estimators=180,
            num_leaves=31,
            learning_rate=0.04,
            subsample=0.82,
            colsample_bytree=0.70,
            min_child_samples=40,
            reg_lambda=2.0,
            n_jobs=max(1, min(12, os.cpu_count() or 1)),
            random_state=seed,
            verbosity=-1,
        )
        classifier.fit(x_train, y_hit_train, sample_weight=weights)
        regressor.fit(x_train, y_net_train)
        models["lightgbm"] = (classifier, regressor)
    return models


def _daily_gate(
    metrics,
    sample_count,
    test_count,
    universe_count,
    train_positive_rate=0.0,
    test_positive_rate=0.0,
):
    tcn = metrics.get("tcn") or {}
    baselines = [metrics.get(name) or {} for name in ("xgboost", "lightgbm") if metrics.get(name)]
    sample_gate = bool(
        sample_count >= MIN_GATE_SAMPLES
        and test_count >= MIN_GATE_TEST_SAMPLES
        and universe_count >= MIN_GATE_SYMBOLS
    )
    positive_rate_drift = abs(float(test_positive_rate) - float(train_positive_rate))
    regime_gate = positive_rate_drift <= MAX_POSITIVE_RATE_DRIFT
    if len(baselines) < 2:
        return {
            "dailyTcnQualified": False,
            "reason": "both XGBoost and LightGBM baselines are required",
        }
    baseline_auc = max(_safe_float(item.get("auc"), -1) for item in baselines)
    baseline_precision = max(_safe_float(item.get("dailyTop5Precision"), -1) for item in baselines)
    baseline_return = max(_safe_float(item.get("dailyTop5AvgNetReturn"), -99) for item in baselines)
    qualified = bool(
        sample_gate
        and regime_gate
        and _safe_float(tcn.get("auc"), -1) >= baseline_auc + 0.01
        and _safe_float(tcn.get("dailyTop5Precision"), -1) >= baseline_precision + 0.01
        and _safe_float(tcn.get("dailyTop5AvgNetReturn"), -99) >= baseline_return
        and _safe_float(tcn.get("dailyTop5AvgNetReturn"), -99) > 0
    )
    return {
        "dailyTcnQualified": qualified,
        "requiredAucLift": 0.01,
        "requiredTop5PrecisionLift": 0.01,
        "requiresPositiveNetReturn": True,
        "sampleGatePassed": sample_gate,
        "regimeGatePassed": regime_gate,
        "trainPositiveRate": round(float(train_positive_rate), 4),
        "testPositiveRate": round(float(test_positive_rate), 4),
        "positiveRateDrift": round(positive_rate_drift, 4),
        "maximumPositiveRateDrift": MAX_POSITIVE_RATE_DRIFT,
        "minimumSamples": MIN_GATE_SAMPLES,
        "minimumTestSamples": MIN_GATE_TEST_SAMPLES,
        "minimumSymbols": MIN_GATE_SYMBOLS,
        "sampleCount": int(sample_count),
        "testCount": int(test_count),
        "universeCount": int(universe_count),
        "bestTreeAuc": round(baseline_auc, 4),
        "bestTreeTop5Precision": round(baseline_precision, 4),
        "bestTreeTop5AvgNetReturn": round(baseline_return, 5),
        "reason": (
            "TCN passed all out-of-sample gates"
            if qualified
            else (
                "TCN stays observation-only because the minimum sample gate is not met"
                if not sample_gate else
                "TCN stays observation-only because the target base rate shifted too far between train and test"
                if not regime_gate else
                "TCN stays observation-only until it beats both tree baselines on held-out data"
            )
        ),
    }


def _insert_run_start(conn, run_id, config):
    ensure_experiment_schema(conn)
    conn.execute(
        """
        INSERT INTO model_experiment_runs (
            run_id, experiment_version, status, mode, started_at, config_json, target_json
        ) VALUES (?, ?, 'running', 'observation', ?, ?, ?)
        """,
        (
            run_id,
            EXPERIMENT_VERSION,
            now_text(),
            _json(config),
            _json({
                "primary": "target_hit_10d",
                "regressions": list(REGRESSION_TARGETS),
                "tradePolicy": radar_trade_policy_payload(),
                "shortHorizon": 5,
                "mediumHorizon": 20,
                "longHorizon": 60,
            }),
        ),
    )


def _save_predictions(conn, run_id, symbols, dates, y_hit, y_net, predictions):
    rows = []
    for index in range(len(dates)):
        rows.append((
            run_id,
            str(symbols[index]),
            str(dates[index]),
            int(y_hit[index]),
            float(y_net[index]),
            float(predictions["tcn"][0][index]),
            float(predictions.get("xgboost", (np.full(len(dates), np.nan),))[0][index])
            if "xgboost" in predictions else None,
            float(predictions.get("lightgbm", (np.full(len(dates), np.nan),))[0][index])
            if "lightgbm" in predictions else None,
            float(predictions["tcn"][1][index]),
            float(predictions["xgboost"][1][index]) if "xgboost" in predictions else None,
            float(predictions["lightgbm"][1][index]) if "lightgbm" in predictions else None,
            now_text(),
        ))
    conn.executemany(
        """
        INSERT OR REPLACE INTO model_experiment_predictions (
            run_id, symbol, signal_date, actual_hit, actual_net_return,
            tcn_probability, xgboost_probability, lightgbm_probability,
            tcn_net_return, xgboost_net_return, lightgbm_net_return, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def train_experiment(
    db_path=DB_PATH,
    max_symbols=DEFAULT_MAX_SYMBOLS,
    max_samples=DEFAULT_MAX_SAMPLES,
    epochs=DEFAULT_EPOCHS,
    stride=2,
    seed=20260710,
):
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    config = {
        "maxSymbols": int(max_symbols),
        "maxSamples": int(max_samples),
        "epochs": int(epochs),
        "stride": int(stride),
        "seed": int(seed),
        "sequenceLength": SEQUENCE_LENGTH,
        "sequenceFeatures": list(SEQUENCE_FEATURES),
        "independentModelUsedByRadar": False,
        "devicePolicy": "cpu_baseline",
    }
    with connect(db_path) as conn:
        _insert_run_start(conn, run_id, config)
    try:
        dataset = build_dataset(
            db_path=db_path,
            max_symbols=max_symbols,
            max_samples=max_samples,
            stride=stride,
            seed=seed,
        )
        train_mask, validation_mask, test_mask, split = chronological_split(dataset["dates"])
        x, normalized_y, normalization = normalize_from_training(
            dataset["x"], dataset["y_reg"], train_mask
        )
        tcn, history, torch = train_tcn(
            x,
            dataset["y_hit"],
            normalized_y,
            train_mask,
            validation_mask,
            epochs,
            seed,
        )
        tcn_validation_prob, tcn_validation_reg_norm = torch_predict(tcn, torch, x[validation_mask])
        tcn_test_prob, tcn_test_reg_norm = torch_predict(tcn, torch, x[test_mask])
        tcn_validation_reg = (
            tcn_validation_reg_norm * normalization["targetStd"] + normalization["targetMean"]
        )
        tcn_test_reg = tcn_test_reg_norm * normalization["targetStd"] + normalization["targetMean"]
        thresholds = {
            "tcn": choose_threshold(dataset["y_hit"][validation_mask], tcn_validation_prob)
        }
        predictions = {
            "tcn": (tcn_test_prob, tcn_test_reg[:, 0]),
        }
        flattened = x.reshape(len(x), -1)
        tree_models = train_tree_baselines(
            flattened[train_mask],
            dataset["y_hit"][train_mask],
            dataset["y_reg"][train_mask, 0],
            seed,
        )
        for name, (classifier, regressor) in tree_models.items():
            validation_probability = classifier.predict_proba(flattened[validation_mask])[:, 1]
            test_probability = classifier.predict_proba(flattened[test_mask])[:, 1]
            thresholds[name] = choose_threshold(dataset["y_hit"][validation_mask], validation_probability)
            predictions[name] = (test_probability, regressor.predict(flattened[test_mask]))
        test_hit = dataset["y_hit"][test_mask]
        test_net = dataset["y_reg"][test_mask, 0]
        test_dates = dataset["dates"][test_mask]
        metrics = {}
        for name, (probability, predicted_net) in predictions.items():
            metrics[name] = evaluate_predictions(
                test_hit,
                test_net,
                test_dates,
                probability,
                predicted_net,
                thresholds[name],
            )
        metrics["tcn"]["multiTargetMae"] = {
            target: round(float(mean_absolute_error(dataset["y_reg"][test_mask, index], tcn_test_reg[:, index])), 5)
            for index, target in enumerate(REGRESSION_TARGETS)
        }
        metrics["tcn"]["trainingHistory"] = history
        gate = _daily_gate(
            metrics,
            sample_count=len(x),
            test_count=int(test_mask.sum()),
            universe_count=dataset["sampledSymbolCount"],
            train_positive_rate=float(dataset["y_hit"][train_mask].mean()),
            test_positive_rate=float(dataset["y_hit"][test_mask].mean()),
        )
        comparison = {
            "scope": "same samples, labels, execution costs, chronological split, and embargo",
            "primaryClassificationTarget": "target_hit_10d",
            "primaryRegressionTarget": "net_return_10d",
            "treeInput": "same normalized 60x12 sequence flattened without future data",
            "models": list(metrics),
            "winnerByAuc": max(
                metrics,
                key=lambda name: _safe_float(metrics[name].get("auc"), -1),
            ),
            "winnerByTop5Precision": max(
                metrics,
                key=lambda name: _safe_float(metrics[name].get("dailyTop5Precision"), -1),
            ),
            "winnerByTop5NetReturn": max(
                metrics,
                key=lambda name: _safe_float(metrics[name].get("dailyTop5AvgNetReturn"), -99),
            ),
        }
        artifact_dir = ARTIFACT_ROOT / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "experimentVersion": EXPERIMENT_VERSION,
                "stateDict": tcn.state_dict(),
                "config": config,
                "normalization": {key: value.tolist() for key, value in normalization.items()},
                "regressionTargets": list(REGRESSION_TARGETS),
                "threshold": thresholds["tcn"],
                "split": split,
            },
            artifact_dir / "daily_tcn.pt",
        )
        with (artifact_dir / "tree_baselines.pkl").open("wb") as handle:
            pickle.dump({"models": tree_models, "thresholds": thresholds}, handle)
        result_payload = {
            "runId": run_id,
            "status": "completed",
            "mode": "observation",
            "dataMaxDate": dataset["dataMaxDate"],
            "sampleCount": int(len(x)),
            "eligibleBeforeCap": int(dataset["eligibleBeforeCap"]),
            "universeCount": len(dataset["universe"]),
            "sampledSymbolCount": dataset["sampledSymbolCount"],
            "split": split,
            "metrics": metrics,
            "comparison": comparison,
            "gate": gate,
        }
        (artifact_dir / "result.json").write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with connect(db_path) as conn:
            ensure_experiment_schema(conn)
            _save_predictions(
                conn,
                run_id,
                dataset["symbols"][test_mask],
                test_dates,
                test_hit,
                test_net,
                predictions,
            )
            conn.execute(
                """
                UPDATE model_experiment_runs
                SET status='completed', completed_at=?, data_max_date=?,
                    sample_count=?, train_count=?, validation_count=?, test_count=?,
                    split_json=?, metrics_json=?, comparison_json=?, gate_json=?,
                    artifact_path=?, error=NULL
                WHERE run_id=?
                """,
                (
                    now_text(),
                    dataset["dataMaxDate"],
                    int(len(x)),
                    int(train_mask.sum()),
                    int(validation_mask.sum()),
                    int(test_mask.sum()),
                    _json(split),
                    _json(metrics),
                    _json(comparison),
                    _json(gate),
                    str(artifact_dir),
                    run_id,
                ),
            )
        return result_payload
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with connect(db_path) as conn:
            ensure_experiment_schema(conn)
            conn.execute(
                "UPDATE model_experiment_runs SET status='failed', completed_at=?, error=? WHERE run_id=?",
                (now_text(), error, run_id),
            )
        raise


def intraday_data_status(conn):
    ensure_experiment_schema(conn)
    row = conn.execute("""
        SELECT COUNT(*) AS bars, COUNT(DISTINCT date) AS sessions,
               COUNT(DISTINCT symbol) AS symbols, MIN(date) AS min_date,
               MAX(date) AS max_date, MAX(updated_at) AS updated_at
        FROM intraday_minute_bars
    """).fetchone()
    bars = int(row["bars"] or 0)
    sessions = int(row["sessions"] or 0)
    symbols = int(row["symbols"] or 0)
    ready = bool(
        sessions >= INTRADAY_MIN_SESSIONS
        and bars >= INTRADAY_MIN_BARS
        and symbols >= INTRADAY_MIN_SYMBOLS
    )
    return {
        "bars": bars,
        "sessions": sessions,
        "symbols": symbols,
        "minDate": row["min_date"],
        "maxDate": row["max_date"],
        "updatedAt": row["updated_at"],
        "requirements": {
            "sessions": INTRADAY_MIN_SESSIONS,
            "bars": INTRADAY_MIN_BARS,
            "symbols": INTRADAY_MIN_SYMBOLS,
        },
        "remaining": {
            "sessions": max(0, INTRADAY_MIN_SESSIONS - sessions),
            "bars": max(0, INTRADAY_MIN_BARS - bars),
            "symbols": max(0, INTRADAY_MIN_SYMBOLS - symbols),
        },
        "ready": ready,
        "barSchema": "real 5-minute OHLC plus active buy/sell/unknown volume and amount",
        "sourcePolicy": "broker streaming ticks only; no synthetic bars",
    }


def order_book_data_status(conn):
    ensure_experiment_schema(conn)
    row = conn.execute("""
        SELECT COUNT(*) AS feature_rows, COUNT(DISTINCT date) AS sessions,
               COUNT(DISTINCT symbol) AS symbols, MIN(date) AS min_date,
               MAX(date) AS max_date, MAX(updated_at) AS updated_at,
               COALESCE(SUM(observation_count), 0) AS observations
        FROM order_book_5m_features
    """).fetchone()
    feature_rows = int(row["feature_rows"] or 0)
    sessions = int(row["sessions"] or 0)
    symbols = int(row["symbols"] or 0)
    observations = int(row["observations"] or 0)
    ready = bool(
        sessions >= ORDER_BOOK_MIN_SESSIONS
        and feature_rows >= ORDER_BOOK_MIN_ROWS
        and symbols >= ORDER_BOOK_MIN_SYMBOLS
    )
    return {
        "featureRows": feature_rows,
        "observations": observations,
        "sessions": sessions,
        "symbols": symbols,
        "minDate": row["min_date"],
        "maxDate": row["max_date"],
        "updatedAt": row["updated_at"],
        "requirements": {
            "sessions": ORDER_BOOK_MIN_SESSIONS,
            "featureRows": ORDER_BOOK_MIN_ROWS,
            "symbols": ORDER_BOOK_MIN_SYMBOLS,
        },
        "remaining": {
            "sessions": max(0, ORDER_BOOK_MIN_SESSIONS - sessions),
            "featureRows": max(0, ORDER_BOOK_MIN_ROWS - feature_rows),
            "symbols": max(0, ORDER_BOOK_MIN_SYMBOLS - symbols),
        },
        "ready": ready,
        "featureSchema": "real Shioaji five-level BidAsk aggregated to five-minute depth, imbalance, spread, and microprice features",
        "sourcePolicy": "broker streaming BidAsk only; no synthetic order book",
    }


def next_weekly_tcn_run(now=None):
    current = now or dt.datetime.now()
    days_ahead = (4 - current.weekday()) % 7
    candidate = (current + dt.timedelta(days=days_ahead)).replace(
        hour=18, minute=40, second=0, microsecond=0
    )
    if candidate <= current:
        candidate += dt.timedelta(days=7)
    return candidate.strftime("%Y-%m-%d %H:%M:%S")


def repair_relocated_artifact_paths(conn, artifact_root=None):
    root = Path(artifact_root or ARTIFACT_ROOT)
    rows = conn.execute("""
        SELECT run_id, artifact_path
        FROM model_experiment_runs
        WHERE artifact_path IS NOT NULL AND TRIM(artifact_path) != ''
    """).fetchall()
    repaired = []
    for row in rows:
        stored = Path(str(row["artifact_path"] or ""))
        relocated = root / str(row["run_id"] or "")
        if stored.exists() or not relocated.exists():
            continue
        conn.execute(
            "UPDATE model_experiment_runs SET artifact_path = ? WHERE run_id = ?",
            (str(relocated), row["run_id"]),
        )
        repaired.append(str(row["run_id"]))
    return repaired


def experiment_status(db_path=DB_PATH):
    with connect(db_path) as conn:
        ensure_experiment_schema(conn)
        relocated_runs = repair_relocated_artifact_paths(conn)
        row = conn.execute(
            "SELECT * FROM model_experiment_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        intraday = intraday_data_status(conn)
        order_book = order_book_data_status(conn)
    latest = None
    if row:
        latest = {
            "runId": row["run_id"],
            "experimentVersion": row["experiment_version"],
            "status": row["status"],
            "mode": row["mode"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
            "dataMaxDate": row["data_max_date"],
            "sampleCount": int(row["sample_count"] or 0),
            "trainCount": int(row["train_count"] or 0),
            "validationCount": int(row["validation_count"] or 0),
            "testCount": int(row["test_count"] or 0),
            "config": _loads(row["config_json"], {}),
            "target": _loads(row["target_json"], {}),
            "split": _loads(row["split_json"], {}),
            "metrics": _loads(row["metrics_json"], {}),
            "comparison": _loads(row["comparison_json"], {}),
            "gate": _loads(row["gate_json"], {}),
            "artifactPath": row["artifact_path"],
            "error": row["error"],
        }
        if str(latest.get("runId") or "") in relocated_runs:
            latest["artifactPathRelocated"] = True
    daily_qualified = bool((latest or {}).get("gate", {}).get("dailyTcnQualified"))
    intraday_eligible = daily_qualified and intraday["ready"]
    if intraday_eligible:
        intraday_mode = "eligible_for_observation_training"
    elif not intraday["ready"]:
        intraday_mode = "collecting_real_data"
    else:
        intraday_mode = "blocked_by_daily_tcn_gate"
    intraday_branch = {
        "architecturePrepared": True,
        "trained": False,
        "validated": False,
        "mode": intraday_mode,
        "enabledInProduction": False,
        "eligible": intraday_eligible,
        "requirements": [
            "daily TCN must beat XGBoost and LightGBM out of sample",
            "at least 60 sessions, 100000 five-minute bars, and 100 symbols",
        ],
    }
    tft_data_ready = bool(
        intraday["sessions"] >= TFT_MIN_SESSIONS
        and intraday["bars"] >= INTRADAY_MIN_BARS
        and intraday["symbols"] >= INTRADAY_MIN_SYMBOLS
    )
    tft_eligible = bool(daily_qualified and tft_data_ready)
    order_book_eligible = bool(daily_qualified and order_book["ready"])
    return {
        "ok": True,
        "isolation": {
            "mode": "independent_observation_only",
            "usedByRadar": False,
            "usedByFormalBuySell": False,
        },
        "runtime": {
            "venvPython": str(PYTORCH_VENV_PYTHON),
            "installed": PYTORCH_VENV_PYTHON.exists(),
            "devicePolicy": "CPU baseline; GPU is not required",
        },
        "latestRun": latest,
        "schedule": {
            "dailyTcn": {
                "automatic": True,
                "weekday": "Friday",
                "time": "18:40",
                "nextRunAt": next_weekly_tcn_run(),
            },
            "intradayCollection": {
                "automatic": True,
                "tradingWindow": "08:45-13:35",
            },
            "orderBookCollection": {
                "automatic": True,
                "tradingWindow": "08:45-13:35",
                "startsWithNextCollectorSession": True,
            },
        },
        "intradayData": intraday,
        "orderBookData": order_book,
        "intradayBranch": intraday_branch,
        "tftGate": {
            "eligible": tft_eligible,
            "dataReady": tft_data_ready,
            "status": "collecting_real_data" if not tft_data_ready else (
                "blocked_by_daily_tcn_gate" if not daily_qualified else "eligible_for_separate_review"
            ),
            "minimumIntradaySessions": TFT_MIN_SESSIONS,
            "remainingSessions": max(0, TFT_MIN_SESSIONS - int(intraday["sessions"] or 0)),
            "enabledInProduction": False,
        },
        "orderBookGate": {
            "eligible": order_book_eligible,
            "dataReady": order_book["ready"],
            "status": "collecting_real_level5_data" if not order_book["ready"] else (
                "blocked_by_daily_tcn_gate" if not daily_qualified else "eligible_for_separate_review"
            ),
            "enabledInProduction": False,
        },
        "checkedAt": now_text(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("init-db")
    train_parser = subparsers.add_parser("train")
    train_parser.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    train_parser.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    train_parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    train_parser.add_argument("--stride", type=int, default=2)
    train_parser.add_argument("--seed", type=int, default=20260710)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-db":
            with connect() as conn:
                ensure_experiment_schema(conn)
            output = {"ok": True, "initialized": True}
        elif args.command == "status":
            output = experiment_status()
        else:
            output = train_experiment(
                max_symbols=args.max_symbols,
                max_samples=args.max_samples,
                epochs=args.epochs,
                stride=args.stride,
                seed=args.seed,
            )
        print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
