"""Rule-only walk-forward calibration for the monster-radar display score.

This script never loads or calls the independent ML model. It rebuilds historical
rule candidates from verified OHLCV, settles every signal with the same executable
cost/target/stop policy used by the live score track record, and writes an approved
configuration only when walk-forward precision improves without weaker net results.
"""

from collections import defaultdict
import hashlib
import json
import time

from ml_backend import (
    DEFAULT_RADAR_RULE_WEIGHTS,
    FEATURE_NAMES,
    MarketContext,
    MIN_MONSTER_AVG_VOLUME_LOTS,
    MIN_MONSTER_TURNOVER_MILLION,
    MONSTER_TARGET_HORIZON_DAYS,
    PAPER_BASE_SLIPPAGE_RATE,
    RADAR_LIVE_WEIGHT_MIN_SETTLED,
    RADAR_MIN_FORMAL_SCORE,
    RADAR_REGIME_LABELS,
    RADAR_RULE_CONFIG_PATH,
    RADAR_THRESHOLD_CANDIDATES,
    backend,
    compute_sector_theme_snapshot,
    precision_recall_thresholds,
    radar_rule_score_components,
    radar_trade_policy_payload,
    simulate_radar_trade_path,
)


TOP_N = 5
MIN_CANDIDATES_PER_DAY = 20
TRAIN_WINDOW_DAYS = 252
MIN_TRAIN_DAYS = 180
EMBARGO_DAYS = MONSTER_TARGET_HORIZON_DAYS
MIN_PRECISION_LIFT = 0.02
MIN_ENTRY_GUARDRAIL_TRADES = 100
UNIVERSE_SEED = 20260710
UNIVERSE_POLICY = "deterministic_non_return_sample_with_point_in_time_liquidity"

DEFAULT_ENTRY_GUARDRAIL_RULES = {
    "blockSurgeSetup": False,
    "maxRet5": None,
}
ENTRY_GUARDRAIL_CANDIDATES = (
    {
        "key": "baseline",
        "label": "基準（不限制）",
        "rules": dict(DEFAULT_ENTRY_GUARDRAIL_RULES),
    },
    {
        "key": "block_surge",
        "label": "排除完整追價型態",
        "rules": {"blockSurgeSetup": True, "maxRet5": None},
    },
    {
        "key": "ret5_lt_15",
        "label": "5日漲幅低於15%",
        "rules": {"blockSurgeSetup": False, "maxRet5": 15.0},
    },
    {
        "key": "no_chase_combined",
        "label": "排除完整追價且5日漲幅低於15%",
        "rules": {"blockSurgeSetup": True, "maxRet5": 15.0},
    },
)

IDX_MA = FEATURE_NAMES.index("ma_trend")
IDX_RSI = FEATURE_NAMES.index("rsi")
IDX_MACD = FEATURE_NAMES.index("macd_pct")
IDX_RET5 = FEATURE_NAMES.index("ret_5")
IDX_RET20 = FEATURE_NAMES.index("ret_20")
IDX_VR = FEATURE_NAMES.index("volume_ratio")
IDX_REL = FEATURE_NAMES.index("stock_vs_taiex_20")


def normalize_dynamic_weights(volume, month_high, surge, counter):
    dynamic_total = float(volume + month_high + surge + counter)
    if dynamic_total <= 0:
        return dict(DEFAULT_RADAR_RULE_WEIGHTS)
    available = 100.0 - float(DEFAULT_RADAR_RULE_WEIGHTS["market_strength"])
    scale = available / dynamic_total
    weights = {
        "volume": round(volume * scale, 4),
        "month_high": round(month_high * scale, 4),
        "market_strength": float(DEFAULT_RADAR_RULE_WEIGHTS["market_strength"]),
        "surge": round(surge * scale, 4),
        "counter": round(counter * scale, 4),
    }
    weights["volume"] = round(weights["volume"] + (100.0 - sum(weights.values())), 4)
    return weights


def generate_weight_candidates():
    candidates = [dict(DEFAULT_RADAR_RULE_WEIGHTS)]
    seen = {tuple(DEFAULT_RADAR_RULE_WEIGHTS[key] for key in DEFAULT_RADAR_RULE_WEIGHTS)}
    for volume in (24, 44, 64):
        for month_high in (14, 34, 54):
            for surge in (0, 15, 30):
                for counter in (0, 6, 18):
                    weights = normalize_dynamic_weights(volume, month_high, surge, counter)
                    key = tuple(weights[name] for name in DEFAULT_RADAR_RULE_WEIGHTS)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(weights)
    return candidates


def record_score(record, weights):
    result = radar_rule_score_components(
        record["volumeRatio"],
        record["monthHigh"],
        record["marketStrength"],
        record["surge"],
        record["counter"],
        weights=weights,
    )
    return result["rawScore"]


def entry_guardrail_allows(record, rules=None):
    """Apply one fixed rule-only entry guardrail to a historical candidate."""
    rules = rules or DEFAULT_ENTRY_GUARDRAIL_RULES
    if bool(rules.get("blockSurgeSetup")) and bool(record.get("surge")):
        return False
    max_ret5 = rules.get("maxRet5")
    if max_ret5 is not None:
        try:
            ret5 = float(record["ret5"])
            maximum = float(max_ret5)
        except (KeyError, TypeError, ValueError):
            # An active cap cannot be verified without the point-in-time value.
            return False
        if ret5 >= maximum:
            return False
    return True


def select_picks(
    records_by_date, dates, weights, top_n=TOP_N,
    minimum_score=None, market_regime=None, entry_guardrail_rules=None,
):
    picks = []
    score_floor = float(minimum_score) if minimum_score is not None else None
    for date in dates:
        rows = records_by_date.get(date) or []
        if len(rows) < MIN_CANDIDATES_PER_DAY:
            continue
        eligible = []
        for row in rows:
            if market_regime and row.get("marketRegime") != market_regime:
                continue
            if not entry_guardrail_allows(row, entry_guardrail_rules):
                continue
            score = record_score(row, weights)
            if score_floor is not None and score < score_floor:
                continue
            eligible.append((row, score))
        ranked = sorted(
            eligible,
            key=lambda pair: (pair[1], pair[0]["volumeRatio"], pair[0]["symbol"]),
            reverse=True,
        )
        picks.extend(row for row, _ in ranked[:top_n])
    return picks


def precision_recall_records(
    records_by_date, dates, weights, market_regime=None,
    entry_guardrail_rules=None,
):
    """Score every point-in-time candidate in the supplied training window."""
    output = []
    for date in dates:
        rows = records_by_date.get(date) or []
        if len(rows) < MIN_CANDIDATES_PER_DAY:
            continue
        for row in rows:
            if market_regime and row.get("marketRegime") != market_regime:
                continue
            if not entry_guardrail_allows(row, entry_guardrail_rules):
                continue
            output.append({
                "score": record_score(row, weights),
                "targetHit": bool(row.get("targetHit")),
                "netReturn": row.get("netReturn"),
            })
    return output


def aggregate(picks):
    if not picks:
        return {
            "trades": 0, "precision": None, "avgNetReturn": None,
            "profitFactor": None, "maxDrawdown": None, "positiveMonthRate": None,
        }
    hits = sum(1 for row in picks if row["targetHit"])
    nets = [float(row["netReturn"]) for row in picks]
    wins = [value for value in nets if value > 0]
    losses = [-value for value in nets if value <= 0]
    daily = defaultdict(list)
    monthly = defaultdict(list)
    for row, net in zip(picks, nets):
        daily[row["date"]].append(net)
        monthly[row["date"][:7]].append(net)
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for date in sorted(daily):
        equity *= 1 + sum(daily[date]) / len(daily[date])
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    positive_months = sum(1 for values in monthly.values() if sum(values) / len(values) > 0)
    return {
        "trades": len(picks),
        "precision": round(hits / len(picks), 4),
        "avgNetReturn": round(sum(nets) / len(nets), 4),
        "profitFactor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else None,
        "maxDrawdown": round(max_drawdown, 4),
        "positiveMonthRate": round(positive_months / len(monthly), 4) if monthly else None,
    }


def choose_weights(records_by_date, train_dates, candidates=None):
    candidates = candidates or generate_weight_candidates()
    baseline_picks = select_picks(records_by_date, train_dates, DEFAULT_RADAR_RULE_WEIGHTS)
    baseline = aggregate(baseline_picks)
    allowed_avg = (baseline.get("avgNetReturn") or 0) - 0.001
    best_weights = dict(DEFAULT_RADAR_RULE_WEIGHTS)
    best_metrics = baseline
    for weights in candidates:
        metrics = aggregate(select_picks(records_by_date, train_dates, weights))
        if not metrics["trades"] or metrics.get("precision") is None:
            continue
        if (metrics.get("avgNetReturn") or -99) < allowed_avg:
            continue
        if metrics.get("profitFactor") is not None and metrics["profitFactor"] < 0.95:
            continue
        best_key = (
            best_metrics.get("precision") or -1,
            best_metrics.get("avgNetReturn") or -99,
            best_metrics.get("profitFactor") or -1,
        )
        candidate_key = (
            metrics.get("precision") or -1,
            metrics.get("avgNetReturn") or -99,
            metrics.get("profitFactor") or -1,
        )
        if candidate_key > best_key:
            best_weights, best_metrics = dict(weights), metrics
    return {
        "weights": best_weights,
        "metrics": best_metrics,
        "baseline": baseline,
    }


def precision_lift(candidate, baseline):
    if candidate.get("precision") is None or baseline.get("precision") is None:
        return None
    return round(float(candidate["precision"]) - float(baseline["precision"]), 4)


def profit_factor_above_one(metrics):
    value = metrics.get("profitFactor")
    if value is None:
        return bool((metrics.get("avgNetReturn") or 0) > 0 and int(metrics.get("trades") or 0) > 0)
    return float(value) > 1.0


def profit_factor_not_weaker(candidate, baseline):
    candidate_value = candidate.get("profitFactor")
    baseline_value = baseline.get("profitFactor")
    if candidate_value is None or baseline_value is None:
        return profit_factor_above_one(candidate)
    return float(candidate_value) >= float(baseline_value)


def entry_guardrail_complexity(rules):
    return int(bool(rules.get("blockSurgeSetup"))) + int(rules.get("maxRet5") is not None)


def choose_entry_guardrail(records_by_date, dates):
    """Choose among fixed no-chase rules using only the supplied training dates."""
    baseline_picks = select_picks(
        records_by_date,
        dates,
        DEFAULT_RADAR_RULE_WEIGHTS,
        minimum_score=RADAR_MIN_FORMAL_SCORE,
        entry_guardrail_rules=DEFAULT_ENTRY_GUARDRAIL_RULES,
    )
    baseline = aggregate(baseline_picks)
    baseline_trades = int(baseline.get("trades") or 0)
    minimum_trades = max(20, int(baseline_trades * 0.60)) if baseline_trades else 0
    choices = []
    for candidate in ENTRY_GUARDRAIL_CANDIDATES:
        metrics = aggregate(select_picks(
            records_by_date,
            dates,
            DEFAULT_RADAR_RULE_WEIGHTS,
            minimum_score=RADAR_MIN_FORMAL_SCORE,
            entry_guardrail_rules=candidate["rules"],
        ))
        if not metrics.get("trades") or int(metrics["trades"]) < minimum_trades:
            continue
        choices.append({
            "key": candidate["key"],
            "label": candidate["label"],
            "rules": dict(candidate["rules"]),
            "metrics": metrics,
        })
    if not choices:
        return {
            "key": "baseline",
            "label": ENTRY_GUARDRAIL_CANDIDATES[0]["label"],
            "rules": dict(DEFAULT_ENTRY_GUARDRAIL_RULES),
            "metrics": baseline,
            "baseline": baseline,
            "choices": [],
        }

    def rank(choice):
        metrics = choice["metrics"]
        return (
            bool((metrics.get("avgNetReturn") or -99) > 0 and profit_factor_above_one(metrics)),
            float(metrics.get("precision") or -1),
            float(metrics.get("avgNetReturn") or -99),
            float(metrics.get("profitFactor") or -1),
            -entry_guardrail_complexity(choice["rules"]),
        )

    selected = max(choices, key=rank)
    return {
        **selected,
        "baseline": baseline,
        "choices": choices,
    }


def calibrate_entry_guardrails(records_by_date, fold_specs, final_dates):
    """Walk-forward a fixed entry guardrail and keep it inert until every gate passes."""
    fold_results = []
    pressure_baseline_picks = []
    pressure_selected_picks = []
    for spec in fold_specs:
        selection = choose_entry_guardrail(records_by_date, spec["trainDates"])
        baseline_test = select_picks(
            records_by_date,
            spec["testDates"],
            DEFAULT_RADAR_RULE_WEIGHTS,
            minimum_score=RADAR_MIN_FORMAL_SCORE,
            entry_guardrail_rules=DEFAULT_ENTRY_GUARDRAIL_RULES,
        )
        selected_test = select_picks(
            records_by_date,
            spec["testDates"],
            DEFAULT_RADAR_RULE_WEIGHTS,
            minimum_score=RADAR_MIN_FORMAL_SCORE,
            entry_guardrail_rules=selection["rules"],
        )
        pressure_baseline_picks.extend(baseline_test)
        pressure_selected_picks.extend(selected_test)
        fold_results.append({
            "month": spec["month"],
            "selectedKey": selection["key"],
            "selectedLabel": selection["label"],
            "selectedRules": dict(selection["rules"]),
            "baseline": aggregate(baseline_test),
            "selected": aggregate(selected_test),
        })

    recent_folds = fold_results[-12:]
    recent_months = {item["month"] for item in recent_folds}
    recent_baseline = aggregate([
        row for row in pressure_baseline_picks if row["date"][:7] in recent_months
    ])
    recent_selected = aggregate([
        row for row in pressure_selected_picks if row["date"][:7] in recent_months
    ])
    pressure_baseline = aggregate(pressure_baseline_picks)
    pressure_selected = aggregate(pressure_selected_picks)
    final_selection = choose_entry_guardrail(records_by_date, final_dates)
    recommended_key = str(final_selection["key"])
    recommended_rules = dict(final_selection["rules"])
    active_recent_folds = [
        item for item in recent_folds if int(item["baseline"].get("trades") or 0) > 0
    ]
    stability = (
        sum(item["selectedKey"] == recommended_key for item in active_recent_folds)
        / len(active_recent_folds)
        if active_recent_folds else 0.0
    )
    recent_lift = precision_lift(recent_selected, recent_baseline)
    pressure_lift = precision_lift(pressure_selected, pressure_baseline)
    final_lift = precision_lift(final_selection["metrics"], final_selection["baseline"])
    checks = {
        "ruleChanged": recommended_key != "baseline",
        "recentNetPositive": (recent_selected.get("avgNetReturn") or -99) > 0,
        "recentProfitFactorAboveOne": profit_factor_above_one(recent_selected),
        "recentPrecisionLiftAtLeast2pp": recent_lift is not None and recent_lift >= MIN_PRECISION_LIFT,
        "recentTradesEnough": int(recent_selected.get("trades") or 0) >= MIN_ENTRY_GUARDRAIL_TRADES,
        "recentMonthsEnough": len(active_recent_folds) >= 6,
        "pressureNetNotWeaker": (
            (pressure_selected.get("avgNetReturn") or -99)
            >= (pressure_baseline.get("avgNetReturn") or -99)
        ),
        "pressureProfitFactorNotWeaker": profit_factor_not_weaker(
            pressure_selected, pressure_baseline
        ),
        "pressureFoldsEnough": len(fold_results) >= 12,
        "finalTrainingNetPositive": (final_selection["metrics"].get("avgNetReturn") or -99) > 0,
        "finalTrainingProfitFactorAboveOne": profit_factor_above_one(final_selection["metrics"]),
        "finalTrainingPrecisionLiftAtLeast2pp": final_lift is not None and final_lift >= MIN_PRECISION_LIFT,
        "finalTrainingTradesEnough": (
            int(final_selection["metrics"].get("trades") or 0)
            >= MIN_ENTRY_GUARDRAIL_TRADES
        ),
        "ruleStable": stability >= 0.50,
    }
    approved = all(checks.values())
    return {
        "approved": approved,
        "policy": "fixed_rule_only_no_chase",
        "recommendedKey": recommended_key,
        "recommendedLabel": final_selection["label"],
        "recommendedRules": recommended_rules,
        "effectiveRules": (
            recommended_rules if approved else dict(DEFAULT_ENTRY_GUARDRAIL_RULES)
        ),
        "ruleStability": round(stability, 4),
        "adoptionChecks": checks,
        "adoptionGates": {
            "recentNetReturn": "> 0",
            "recentProfitFactor": "> 1",
            "recentPrecisionLift": f">= {MIN_PRECISION_LIFT:.2f}",
            "minimumRecentTrades": MIN_ENTRY_GUARDRAIL_TRADES,
            "pressureTest": "net return and profit factor not weaker",
            "minimumStability": 0.50,
        },
        "recentOos": {
            "months": len(active_recent_folds),
            "precisionLift": recent_lift,
            "baseline": recent_baseline,
            "recommended": recent_selected,
        },
        "pressureTestOos": {
            "folds": len(fold_results),
            "precisionLift": pressure_lift,
            "baseline": pressure_baseline,
            "recommended": pressure_selected,
        },
        "finalTraining": {
            "dateStart": final_dates[0] if final_dates else None,
            "dateEnd": final_dates[-1] if final_dates else None,
            "precisionLift": final_lift,
            "baseline": final_selection["baseline"],
            "recommended": final_selection["metrics"],
        },
        "folds": fold_results,
    }


def choose_regime_threshold(records_by_date, dates, regime_key):
    threshold_candidates = tuple(
        float(value) for value in RADAR_THRESHOLD_CANDIDATES
        if float(value) >= RADAR_MIN_FORMAL_SCORE
    )
    pr_comparison = precision_recall_thresholds(
        precision_recall_records(
            records_by_date,
            dates,
            DEFAULT_RADAR_RULE_WEIGHTS,
            market_regime=regime_key,
        ),
        threshold_candidates,
    )
    pr_by_threshold = {
        float(point["threshold"]): point
        for point in (pr_comparison.get("points") or [])
    }
    baseline_picks = select_picks(
        records_by_date, dates, DEFAULT_RADAR_RULE_WEIGHTS,
        minimum_score=RADAR_MIN_FORMAL_SCORE, market_regime=regime_key,
    )
    baseline = aggregate(baseline_picks)
    baseline_trades = int(baseline.get("trades") or 0)
    minimum_trades = max(20, int(baseline_trades * 0.60)) if baseline_trades else 0
    choices = []
    for threshold in threshold_candidates:
        picks = select_picks(
            records_by_date, dates, DEFAULT_RADAR_RULE_WEIGHTS,
            minimum_score=threshold, market_regime=regime_key,
        )
        metrics = aggregate(picks)
        if int(metrics.get("trades") or 0) < minimum_trades:
            continue
        choices.append({
            "threshold": float(threshold),
            "metrics": metrics,
            "precisionRecall": pr_by_threshold.get(float(threshold)) or {},
        })
    if not choices:
        return {
            "threshold": RADAR_MIN_FORMAL_SCORE,
            "metrics": baseline,
            "baseline": baseline,
            "choices": [],
            "precisionRecall": pr_comparison,
            "selectionBasis": "precision_recall_walk_forward",
        }

    def rank(choice):
        metrics = choice["metrics"]
        pr_point = choice.get("precisionRecall") or {}
        return (
            bool((metrics.get("avgNetReturn") or -99) > 0 and profit_factor_above_one(metrics)),
            float(pr_point.get("f1") if pr_point.get("f1") is not None else -1),
            float(pr_point.get("precision") if pr_point.get("precision") is not None else -1),
            float(pr_point.get("recall") if pr_point.get("recall") is not None else -1),
            float(metrics.get("avgNetReturn") or -99),
            float(metrics.get("profitFactor") or -1),
            -float(choice["threshold"]),
        )

    selected = max(choices, key=rank)
    return {
        "threshold": selected["threshold"],
        "metrics": selected["metrics"],
        "baseline": baseline,
        "choices": choices,
        "precisionRecall": pr_comparison,
        "selectionBasis": "precision_recall_walk_forward",
    }


def calibrate_regime_thresholds(records_by_date, fold_specs, final_dates):
    regimes = {}
    effective_thresholds = {}
    for regime_key, label in RADAR_REGIME_LABELS.items():
        fold_results = []
        pressure_baseline_picks = []
        pressure_selected_picks = []
        for spec in fold_specs:
            selection = choose_regime_threshold(records_by_date, spec["trainDates"], regime_key)
            baseline_test = select_picks(
                records_by_date, spec["testDates"], DEFAULT_RADAR_RULE_WEIGHTS,
                minimum_score=RADAR_MIN_FORMAL_SCORE, market_regime=regime_key,
            )
            selected_test = select_picks(
                records_by_date, spec["testDates"], DEFAULT_RADAR_RULE_WEIGHTS,
                minimum_score=selection["threshold"], market_regime=regime_key,
            )
            pressure_baseline_picks.extend(baseline_test)
            pressure_selected_picks.extend(selected_test)
            fold_results.append({
                "month": spec["month"],
                "selectedThreshold": selection["threshold"],
                "trainingPrecisionRecall": selection["precisionRecall"],
                "baseline": aggregate(baseline_test),
                "selected": aggregate(selected_test),
            })

        recent_folds = fold_results[-12:]
        recent_months = {item["month"] for item in recent_folds}
        recent_baseline_picks = [
            row for row in pressure_baseline_picks if row["date"][:7] in recent_months
        ]
        recent_selected_picks = [
            row for row in pressure_selected_picks if row["date"][:7] in recent_months
        ]
        pressure_baseline = aggregate(pressure_baseline_picks)
        pressure_selected = aggregate(pressure_selected_picks)
        recent_baseline = aggregate(recent_baseline_picks)
        recent_selected = aggregate(recent_selected_picks)
        final_selection = choose_regime_threshold(records_by_date, final_dates, regime_key)
        recent_lift = precision_lift(recent_selected, recent_baseline)
        pressure_lift = precision_lift(pressure_selected, pressure_baseline)
        final_lift = precision_lift(final_selection["metrics"], final_selection["baseline"])
        recommended_threshold = float(final_selection["threshold"])
        active_recent_folds = [
            item for item in recent_folds if int(item["baseline"].get("trades") or 0) > 0
        ]
        stability = (
            sum(item["selectedThreshold"] == recommended_threshold for item in active_recent_folds)
            / len(active_recent_folds)
            if active_recent_folds else 0.0
        )
        baseline_pressure_pf = pressure_baseline.get("profitFactor")
        selected_pressure_pf = pressure_selected.get("profitFactor")
        if selected_pressure_pf is None:
            pressure_pf_not_weaker = profit_factor_above_one(pressure_selected)
        elif baseline_pressure_pf is None:
            pressure_pf_not_weaker = profit_factor_above_one(pressure_selected)
        else:
            pressure_pf_not_weaker = float(selected_pressure_pf) >= float(baseline_pressure_pf)
        checks = {
            "thresholdChanged": recommended_threshold > RADAR_MIN_FORMAL_SCORE,
            "recentNetPositive": (recent_selected.get("avgNetReturn") or -99) > 0,
            "recentProfitFactorAboveOne": profit_factor_above_one(recent_selected),
            "recentPrecisionLiftAtLeast2pp": recent_lift is not None and recent_lift >= MIN_PRECISION_LIFT,
            "recentTradesEnough": int(recent_selected.get("trades") or 0) >= 100,
            "recentMonthsEnough": len(active_recent_folds) >= 6,
            "pressureNetNotWeaker": (
                (pressure_selected.get("avgNetReturn") or -99)
                >= (pressure_baseline.get("avgNetReturn") or -99)
            ),
            "pressureProfitFactorNotWeaker": pressure_pf_not_weaker,
            "pressureFoldsEnough": len(fold_results) >= 12,
            "finalTrainingNetPositive": (final_selection["metrics"].get("avgNetReturn") or -99) > 0,
            "finalTrainingProfitFactorAboveOne": profit_factor_above_one(final_selection["metrics"]),
            "finalTrainingPrecisionLiftAtLeast2pp": final_lift is not None and final_lift >= MIN_PRECISION_LIFT,
            "thresholdStable": stability >= 0.50,
        }
        approved = all(checks.values())
        effective_threshold = recommended_threshold if approved else RADAR_MIN_FORMAL_SCORE
        effective_thresholds[regime_key] = effective_threshold
        regimes[regime_key] = {
            "label": label,
            "approved": approved,
            "baseThreshold": RADAR_MIN_FORMAL_SCORE,
            "recommendedThreshold": recommended_threshold,
            "effectiveThreshold": effective_threshold,
            "thresholdStability": round(stability, 4),
            "adoptionChecks": checks,
            "recentOos": {
                "months": len(active_recent_folds),
                "precisionLift": recent_lift,
                "baseline": recent_baseline,
                "recommended": recent_selected,
            },
            "pressureTestOos": {
                "folds": len(fold_results),
                "precisionLift": pressure_lift,
                "baseline": pressure_baseline,
                "recommended": pressure_selected,
            },
            "finalTraining": {
                "dateStart": final_dates[0] if final_dates else None,
                "dateEnd": final_dates[-1] if final_dates else None,
                "precisionLift": final_lift,
                "baseline": final_selection["baseline"],
                "recommended": final_selection["metrics"],
                "precisionRecall": final_selection["precisionRecall"],
            },
            "foldThresholds": fold_results,
        }
    return {
        "approved": any(item["approved"] for item in regimes.values()),
        "policy": "threshold_only_score_unchanged",
        "selectionBasis": "precision_recall_walk_forward",
        "scoreChanged": False,
        "minimumFormalScorePreserved": True,
        "baseThreshold": RADAR_MIN_FORMAL_SCORE,
        "candidateThresholds": list(RADAR_THRESHOLD_CANDIDATES),
        "adoptionGates": {
            "recentNetReturn": "> 0",
            "recentProfitFactor": "> 1",
            "recentPrecisionLift": f">= {MIN_PRECISION_LIFT:.2f}",
            "pressureTest": "net return and profit factor not weaker",
        },
        "effectiveThresholds": effective_thresholds,
        "regimes": regimes,
    }


def walk_forward_calibrate(records, candidates=None):
    records_by_date = defaultdict(list)
    for record in records:
        records_by_date[record["date"]].append(record)
    dates = sorted(records_by_date)
    date_index = {date: index for index, date in enumerate(dates)}
    months = sorted({date[:7] for date in dates})
    candidates = candidates or generate_weight_candidates()
    folds = []
    fold_specs = []
    calibrated_picks = []
    baseline_picks = []
    for month in months:
        test_dates = [date for date in dates if date.startswith(month)]
        if not test_dates:
            continue
        test_start = date_index[test_dates[0]]
        train_end = test_start - EMBARGO_DAYS
        if train_end < MIN_TRAIN_DAYS:
            continue
        train_start = max(0, train_end - TRAIN_WINDOW_DAYS)
        train_dates = dates[train_start:train_end]
        if len(train_dates) < MIN_TRAIN_DAYS:
            continue
        selected = choose_weights(records_by_date, train_dates, candidates=candidates)
        selected_test = select_picks(records_by_date, test_dates, selected["weights"])
        baseline_test = select_picks(records_by_date, test_dates, DEFAULT_RADAR_RULE_WEIGHTS)
        selected_metrics = aggregate(selected_test)
        baseline_metrics = aggregate(baseline_test)
        calibrated_picks.extend(selected_test)
        baseline_picks.extend(baseline_test)
        folds.append({
            "month": month,
            "trainStart": train_dates[0],
            "trainEnd": train_dates[-1],
            "testStart": test_dates[0],
            "testEnd": test_dates[-1],
            "weights": selected["weights"],
            "trainMetrics": selected["metrics"],
            "testMetrics": selected_metrics,
            "baselineTestMetrics": baseline_metrics,
        })
        fold_specs.append({
            "month": month,
            "trainDates": train_dates,
            "testDates": test_dates,
        })
    if len(dates) > EMBARGO_DAYS:
        final_end = len(dates) - EMBARGO_DAYS
        final_start = max(0, final_end - TRAIN_WINDOW_DAYS)
        final_dates = dates[final_start:final_end]
    else:
        final_dates = dates
    final_selection = choose_weights(records_by_date, final_dates, candidates=candidates)
    regime_threshold_calibration = calibrate_regime_thresholds(
        records_by_date, fold_specs, final_dates
    )
    entry_guardrail_calibration = calibrate_entry_guardrails(
        records_by_date, fold_specs, final_dates
    )
    calibrated = aggregate(calibrated_picks)
    baseline = aggregate(baseline_picks)
    precision_lift = None
    if calibrated.get("precision") is not None and baseline.get("precision") is not None:
        precision_lift = round(calibrated["precision"] - baseline["precision"], 4)
    final_lift = None
    if final_selection["metrics"].get("precision") is not None and final_selection["baseline"].get("precision") is not None:
        final_lift = round(
            final_selection["metrics"]["precision"] - final_selection["baseline"]["precision"], 4
        )
    calibrated_profit_factor = calibrated.get("profitFactor")
    profit_factor_ok = bool(
        (calibrated_profit_factor is not None and calibrated_profit_factor >= 1.0)
        or (calibrated_profit_factor is None and (calibrated.get("avgNetReturn") or 0) > 0)
    )
    approved = bool(
        len(folds) >= 4
        and precision_lift is not None
        and precision_lift >= MIN_PRECISION_LIFT
        and (calibrated.get("avgNetReturn") or -99) >= (baseline.get("avgNetReturn") or -99)
        and profit_factor_ok
        and final_lift is not None
        and final_lift >= 0.01
    )
    reason = (
        "walk-forward precision and net-return gates passed"
        if approved else
        "calibration stayed in observation mode because precision/net-return gates did not all pass"
    )
    recommended = final_selection["weights"]
    return {
        "approved": approved,
        "approvalReason": reason,
        "baselineWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
        "recommendedWeights": recommended,
        "effectiveWeights": recommended if approved else dict(DEFAULT_RADAR_RULE_WEIGHTS),
        "walkForward": {
            "foldCount": len(folds),
            "precisionLift": precision_lift,
            "baseline": baseline,
            "calibrated": calibrated,
            "folds": folds,
        },
        "finalTraining": {
            "dateStart": final_dates[0] if final_dates else None,
            "dateEnd": final_dates[-1] if final_dates else None,
            "precisionLift": final_lift,
            "baseline": final_selection["baseline"],
            "recommended": final_selection["metrics"],
        },
        "regimeThresholdCalibration": regime_threshold_calibration,
        "entryGuardrailCalibration": entry_guardrail_calibration,
    }


def calibration_universe(limit=800):
    """Select symbols without looking at current/future price performance.

    Every historical record is still required to pass its own point-in-time
    liquidity gate in build_rule_records(). This avoids using today's hottest
    stocks to decide which historical stocks are allowed into the backtest.
    """
    with backend.connect() as conn:
        stocks = conn.execute("""
            SELECT p.symbol, COALESCE(si.name, ''), COALESCE(si.sector, ''),
                   COALESCE(si.market_type, ''), COUNT(*) AS sessions
            FROM prices p
            LEFT JOIN stock_info si ON si.symbol = p.symbol
            WHERE p.symbol GLOB '[0-9][0-9][0-9][0-9]'
              AND p.symbol NOT LIKE '00%'
              AND p.open > 0 AND p.high > 0 AND p.low > 0 AND p.close > 0
            GROUP BY p.symbol
            HAVING COUNT(*) >= 300
        """).fetchall()
    candidates = []
    for symbol, name, sector, market_type, _sessions in stocks:
        description = " ".join(str(value or "").lower() for value in (name, sector, market_type))
        if "etf" in description or "權證" in description or "受益" in description or "warrant" in description:
            continue
        candidates.append(str(symbol))
    candidates.sort(
        key=lambda symbol: hashlib.sha256(f"{UNIVERSE_SEED}:{symbol}".encode("ascii")).digest()
    )
    return candidates[:max(1, int(limit))]


def annotate_record_regimes(records, stock_info=None, market_context=None):
    """Attach point-in-time theme heat and market regime without changing scores."""
    stock_info = stock_info or {}
    context = market_context or MarketContext(backend.load_market_rows())
    by_date = defaultdict(list)
    for record in records:
        by_date[record["date"]].append(record)
    history = []
    for date in sorted(by_date):
        rows = by_date[date]
        snapshot = compute_sector_theme_snapshot(
            rows, stock_info=stock_info, history=history[-10:]
        )
        regime = backend.radar_market_regime_snapshot(
            date, snapshot, market_context=context
        )
        for row in rows:
            sector = row.get("sector") or "台股"
            sector_stat = (snapshot.get("sectors") or {}).get(sector) or {}
            row["marketRegime"] = regime["key"]
            row["marketRegimeLabel"] = regime["label"]
            row["themeHeat"] = float(sector_stat.get("themeHeat") or 0)
            row["sectorThemeStreak"] = int(sector_stat.get("streakDays") or 0)
        history.append({"date": date, "sectors": snapshot.get("sectors") or {}})
    return records


def build_rule_records(symbol_limit=800, progress=True):
    symbols = calibration_universe(symbol_limit)
    stock_info = backend.load_stock_info()
    records = []
    started = time.time()
    for position, symbol in enumerate(symbols):
        if progress and position % 50 == 0:
            print(f"[{position}/{len(symbols)}] {time.time() - started:.0f}s", flush=True)
        try:
            rows = backend.load_price_rows(symbol)
            if len(rows) < 150:
                continue
            for feature in backend.build_features_for_rows(rows):
                index = feature["index"]
                if index + 1 >= len(rows):
                    continue
                future_rows = rows[index + 1:index + 1 + MONSTER_TARGET_HORIZON_DAYS]
                raw_open = backend.safe_float(future_rows[0].get("open")) if future_rows else None
                if raw_open is None or raw_open <= 0:
                    continue
                outcome = simulate_radar_trade_path(raw_open * (1 + PAPER_BASE_SLIPPAGE_RATE), future_rows)
                if not outcome or not outcome.get("settled") or outcome.get("netReturn") is None:
                    continue
                x = feature["x"]
                latest = rows[index]
                previous_close = float(rows[index - 1].get("close") or 0) if index > 0 else 0.0
                latest_close = float(latest.get("close") or 0)
                latest_volume = float(latest.get("volume") or 0)
                change1 = ((latest_close - previous_close) / previous_close * 100) if previous_close > 0 else 0.0
                volume_window = rows[max(0, index - 19):index + 1]
                volumes = [float(row.get("volume") or 0) for row in volume_window]
                avg_volume = sum(volumes) / len(volumes) if volumes else 0.0
                avg_volume_lots = avg_volume / 1000
                volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 0.0
                turnover_million = latest_volume * latest_close / 1_000_000
                market_strength = x[IDX_REL] > 0
                liquidity_ok = (
                    avg_volume_lots >= MIN_MONSTER_AVG_VOLUME_LOTS
                    and turnover_million >= MIN_MONSTER_TURNOVER_MILLION
                )
                if not (liquidity_ok and volume_ratio >= 1.2 and market_strength):
                    continue
                recent_high = max(
                    (float(row.get("high") or 0) for row in rows[max(0, index - 20):index]),
                    default=float(latest.get("high") or 0),
                )
                month_high = bool(recent_high and latest_close >= recent_high * 0.995)
                ret5 = x[IDX_RET5] * 100
                ret20 = x[IDX_RET20] * 100
                counter = bool(market_strength and (
                    month_high or ret5 >= 5 or (change1 >= 3.5 and ret5 >= 0)
                ))
                surge = bool(
                    ret5 >= 7.5
                    and ret20 >= 10
                    and 1.5 <= x[IDX_VR] <= 5.5
                    and month_high
                    and market_strength
                    and x[IDX_MA] > 0
                    and x[IDX_MACD] > 0
                    and x[IDX_RSI] * 100 <= 82
                )
                records.append({
                    "symbol": symbol,
                    "sector": (stock_info.get(symbol) or {}).get("sector") or "台股",
                    "date": feature["date"],
                    "volumeRatio": volume_ratio,
                    "monthHigh": month_high,
                    "marketStrength": market_strength,
                    "surge": surge,
                    "counter": counter,
                    "change1": change1,
                    "ret5": ret5,
                    "ret20": ret20,
                    "rsi": x[IDX_RSI] * 100,
                    "entryGap": raw_open / latest_close - 1 if latest_close > 0 else None,
                    "turnoverMillion": turnover_million,
                    "targetHit": bool(outcome.get("targetHit")),
                    "netReturn": float(outcome["netReturn"]),
                })
        except Exception as exc:
            if progress:
                print(f"skip {symbol}: {exc}", flush=True)
    return annotate_record_regimes(records, stock_info=stock_info)


def run(output_path=RADAR_RULE_CONFIG_PATH, symbol_limit=800):
    records = build_rule_records(symbol_limit=symbol_limit)
    if not records:
        raise RuntimeError("no eligible rule records available for walk-forward calibration")
    result = walk_forward_calibrate(records)
    readiness = backend.current_radar_deployment_readiness()
    live = readiness.get("live") or {}
    live_settled = int(live.get("settled") or 0)
    live_avg_net = live.get("avgNetReturn")
    live_profit_factor = live.get("profitFactor")
    live_validation_approved = bool(
        str(live.get("entryMode") or "") == "intraday_confirmed"
        and live_settled >= RADAR_LIVE_WEIGHT_MIN_SETTLED
        and live_avg_net is not None and float(live_avg_net) > 0
        and live_profit_factor is not None and float(live_profit_factor) > 1
    )
    walk_forward_approved = bool(result.get("approved"))
    result["walkForwardApproved"] = walk_forward_approved
    result["liveValidation"] = {
        "approved": live_validation_approved,
        "entryMode": "intraday_confirmed",
        "settled": live_settled,
        "minimumSettled": RADAR_LIVE_WEIGHT_MIN_SETTLED,
        "avgNetReturn": live_avg_net,
        "profitFactor": live_profit_factor,
        "readinessDate": readiness.get("readinessDate"),
    }
    result["approved"] = bool(walk_forward_approved and live_validation_approved)
    result["effectiveWeights"] = (
        result["recommendedWeights"]
        if result["approved"] else dict(DEFAULT_RADAR_RULE_WEIGHTS)
    )
    if walk_forward_approved and not live_validation_approved:
        result["approvalReason"] = (
            "walk-forward passed but weights remain frozen until at least "
            f"{RADAR_LIVE_WEIGHT_MIN_SETTLED} profitable intraday-confirmed settled samples"
        )
    payload = {
        "version": 1,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "method": "rule_only_walk_forward",
        "independentModelUsed": False,
        "recordCount": len(records),
        "dateRange": [min(row["date"] for row in records), max(row["date"] for row in records)],
        "topN": TOP_N,
        "trainingWindowDays": TRAIN_WINDOW_DAYS,
        "embargoDays": EMBARGO_DAYS,
        "minimumPrecisionLift": MIN_PRECISION_LIFT,
        "universePolicy": UNIVERSE_POLICY,
        "universeSeed": UNIVERSE_SEED,
        "targetPolicy": radar_trade_policy_payload(),
        **result,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "approved": payload["approved"],
        "approvalReason": payload["approvalReason"],
        "recordCount": payload["recordCount"],
        "effectiveWeights": payload["effectiveWeights"],
        "walkForward": payload["walkForward"],
        "finalTraining": payload["finalTraining"],
        "regimeThresholdCalibration": payload["regimeThresholdCalibration"],
        "entryGuardrailCalibration": payload["entryGuardrailCalibration"],
    }, ensure_ascii=False, indent=2))
    print(f"saved {output_path}")
    return payload


if __name__ == "__main__":
    run()
