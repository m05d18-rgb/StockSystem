"""
buy_signal_score 整合權重的回測驗證：把不同權重配置當成不同排名法，
用「每日前10名模擬買進」(同 backtest_top10.py 的方法論)比較 OOS 績效，
讓 0.22/0.22/0.34/0.22 與 0.44/0.34/0.14/0.08 這些寫死的數字有回測依據。

忠實複製生產邏輯(ml_backend.buy_signal_score)：
  win = Σ(模型機率×win權重)/權重和 → core = clamp(win×c1 + rank×c2 +
  anomaly×c3 + (setup-0.5)×c4 - risk_penalty, .01, .99) →
  final = core×(1-0.2×adv_cov) + adv_signal×0.2×adv_cov
adjust_probability_for_market 的三個乘數：前兩個(taiex_ma_gap<0→×0.85、
market_regime<0→×0.88)是同日全市場共用的常數，不改變當日排序；第三個
(stock_vs_taiex_20<=0→×0.92)是個股層級(個股20日報酬-大盤20日報酬)，會
改變排序，套用在最終 blend 分數上(對抗式驗證抓到的缺口，已補上)。

各模型機率用「跟生產相同的輸入」批次計算(正規化後特徵矩陣)。
限制與 backtest_top10.py 相同：存活者偏差、OOS=2025-11之後(模型80/20按日期
切分的驗證段)才算數。

執行：python backtest_ensemble_weights.py (結果寫 backtest_ensemble_result.json)
"""
import json
import sys
import time
import warnings

if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

from ml_backend import (
    FEATURE_NAMES,
    MIN_MONSTER_AVG_VOLUME_LOTS,
    MIN_MONSTER_TURNOVER_MILLION,
    backend,
    sigmoid,
    smooth_clamp_ratio,
    clamp,
)

TOP_N = 10
MIN_CANDIDATES_PER_DAY = 30
IDX_RET5 = FEATURE_NAMES.index("ret_5")
IDX_RET20 = FEATURE_NAMES.index("ret_20")
IDX_VR = FEATURE_NAMES.index("volume_ratio")
IDX_REL = FEATURE_NAMES.index("stock_vs_taiex_20")


def build_component_records():
    symbols = backend.liquid_monster_universe(800)
    model = backend.load_model()
    if not model:
        raise RuntimeError("model.pkl 不可用")
    means = np.array(model["means"], dtype=float)
    stdevs = np.array(model["stdevs"], dtype=float)
    weights = model["weights"]
    extra = model.get("extra_models") or {}

    metas = []       # (date, y, net, setup, risk, adv_cov, adv_signal)
    z_rows = []
    started = time.time()
    for i, symbol in enumerate(symbols):
        if i % 50 == 0:
            print(f"[{i}/{len(symbols)}] {time.time() - started:.0f}s", flush=True)
        try:
            rows = backend.rows_with_verified_sources(backend.load_price_rows(symbol))
            if len(rows) < 150:
                continue
            features = backend.build_features_for_rows(rows)
            for item in features:
                index = item["index"]
                target = backend.short_term_target(rows, index)
                if not target:
                    continue
                x = item["x"]
                market = item.get("market") or {}
                setup_score, setup_parts = backend.setup_score_from_features(x, market)
                risk_penalty, _ = backend.risk_penalty_from_features(x)
                latest = rows[index]
                previous = rows[index - 1] if index > 0 else latest
                latest_close = float(latest.get("close") or 0)
                latest_volume = float(latest.get("volume") or 0)
                previous_close = float(previous.get("close") or 0)
                change1 = ((latest_close - previous_close) / previous_close * 100) if previous_close > 0 else 0.0
                volume_window = rows[max(0, index - 19):index + 1]
                volume_values = [float(row.get("volume") or 0) for row in volume_window]
                avg_volume20_lots = (sum(volume_values) / len(volume_values)) / 1000 if volume_values else 0.0
                avg_volume_incl = sum(volume_values) / len(volume_values) if volume_values else 0.0
                vr_incl = latest_volume / avg_volume_incl if avg_volume_incl > 0 else 0.0
                turnover_million = latest_volume * latest_close / 1_000_000
                recent_high = max(
                    (float(row.get("high") or 0) for row in rows[max(0, index - 20):index]),
                    default=float(latest.get("high") or 0),
                )
                month_high_strength = bool(recent_high > 0 and latest_close >= recent_high * 0.995)
                ret5 = float(x[IDX_RET5]) * 100
                ret20 = float(x[IDX_RET20]) * 100
                volume_ratio = float(x[IDX_VR])
                market_gate = backend.market_gate(market)
                stock_stronger = bool(market_gate.get("stockStrongerThanTaiex"))
                market_ok = bool(market_gate.get("allowBuy"))
                hot_market = bool(market_gate.get("hotMarket"))
                liquidity_ok = bool(
                    avg_volume20_lots >= MIN_MONSTER_AVG_VOLUME_LOTS
                    and turnover_million >= MIN_MONSTER_TURNOVER_MILLION
                )
                rsi = float(x[1]) * 100
                atr_pct = float(x[3]) * 100
                trend_ok = bool(x[0] > 0 and x[2] > 0)
                risk_ok = 1.2 <= atr_pct <= 8.5 and rsi <= 82
                taiex_ret20 = float(market.get("taiex_ret_20") or 0)
                taiex_ma_gap = float(market.get("taiex_ma_gap") or 0)
                weak_market = bool(not market_ok or taiex_ret20 < -0.02 or taiex_ma_gap < -0.02)
                limit_up_strength = bool(change1 >= 8.5 and stock_stronger and volume_ratio >= 1.2)
                counter_strength = bool(
                    weak_market
                    and stock_stronger
                    and liquidity_ok
                    and risk_ok
                    and volume_ratio >= 1.15
                    and (month_high_strength or limit_up_strength or change1 >= 3.5 or ret5 >= 5)
                    and ret20 >= -3
                    and rsi <= 86
                    and volume_ratio <= 6.5
                )
                overheated = bool(
                    (rsi > 82 or volume_ratio > 5.5 or ret5 > 22 or change1 > 9)
                    and not counter_strength
                )
                surge_setup = bool(
                    ret5 >= 7.5
                    and ret20 >= 10
                    and 1.5 <= volume_ratio <= 5.5
                    and month_high_strength
                    and stock_stronger
                    and trend_ok
                    and rsi <= 82
                )
                quick_score = clamp(
                    min(vr_incl / 4, 1) * 44
                    + (34 if month_high_strength else 0)
                    + (16 if stock_stronger else 0)
                    + (6 if counter_strength else 0),
                    0,
                    100,
                )
                danger_risk = any(
                    flag.get("severity") == "danger"
                    for flag in backend._monster_risk_flags(rows, index, volume_ratio)
                )
                counter_override = bool(counter_strength and (month_high_strength or limit_up_strength))
                radar_override = bool(
                    stock_stronger
                    and quick_score >= 70
                    and (surge_setup or counter_override or month_high_strength)
                )
                setup_ok = bool(
                    (market_ok or stock_stronger or counter_strength)
                    and (trend_ok or counter_strength)
                    and (1.15 if hot_market else 1.4) <= volume_ratio <= 5.5
                    and risk_ok
                    and liquidity_ok
                    and not overheated
                )
                buy_allowed = bool(
                    setup_ok
                    and not danger_risk
                    and (radar_override or surge_setup or counter_override or month_high_strength)
                )
                radar_gate = bool(liquidity_ok and vr_incl >= 1.2 and float(x[IDX_REL]) > 0)
                metas.append({
                    "symbol": symbol, "date": item["date"], "y": target["y"], "net": target["net_return"],
                    "setup": setup_score, "risk": risk_penalty,
                    "radarGate": radar_gate, "buyAllowed": buy_allowed,
                    "hardRiskSafe": bool(liquidity_ok and risk_ok and not overheated and not danger_risk),
                    "trendSafe": bool(trend_ok), "setupReady": setup_ok,
                    "quickScore": float(quick_score), "surgeSetup": surge_setup,
                    "monthHighStrength": month_high_strength,
                    "advCov": clamp(float(setup_parts.get("advancedFlowCoverage") or 0), 0, 1),
                    "advSignal": clamp(float(setup_parts.get("advancedSignal") or 0.5), 0, 1),
                    # adjust_probability_for_market 的個股層級乘數要用到這三個。
                    "taiexMaGap": float(market.get("taiex_ma_gap", 0) or 0),
                    "marketRegime": float(market.get("market_regime", 0) or 0),
                    "stockVsTaiex20": float(market.get("stock_vs_taiex_20", 0) or 0),
                })
                z_rows.append((np.array(x, dtype=float) - means) / stdevs)
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)

    Z = np.array(z_rows, dtype=float)
    print(f"records={len(metas)}，開始批次模型推論", flush=True)

    # logistic (純 python 權重 → 向量化)
    w = np.array(weights, dtype=float)
    logit = w[0] + Z @ w[1:]
    p_log = 1 / (1 + np.exp(-np.clip(logit, -35, 35)))

    def batch_proba(key):
        est = (extra.get(key) or {}).get("estimator")
        if est is None:
            return None
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            return est.predict_proba(Z)[:, 1]

    p_xgb = batch_proba("xgboost")
    p_lgb = batch_proba("lightgbm")
    p_gb = batch_proba("gradient_boosting")

    # 校準版：把原始機率映射到「驗證集輸出分佈的百分位」(消除positive_weight
    # 加權造成的機率系統性偏高)。calibration欄位只在新版model.pkl存在，舊版
    # 模型跑這個腳本時校準配置會退回用原始機率(等同current)。
    def batch_calibrated(raw, key):
        if raw is None:
            return None
        calibration = (extra.get(key) or {}).get("calibration") or []
        if not calibration:
            return None
        return np.array([backend.rank_probability(float(v), calibration) for v in raw])

    p_xgb_cal = batch_calibrated(p_xgb, "xgboost")
    p_lgb_cal = batch_calibrated(p_lgb, "lightgbm")
    p_gb_cal = batch_calibrated(p_gb, "gradient_boosting")

    iso = (extra.get("isolation_forest") or {})
    if iso.get("estimator") is not None:
        raw = iso["estimator"].score_samples(Z)
        lo = float(iso.get("score_low", float(raw.min())))
        hi = float(iso.get("score_high", float(raw.max()) + 1e-9))
        p_anom = np.array([smooth_clamp_ratio((s - lo) / max(hi - lo, 1e-9)) for s in raw])
    else:
        p_anom = np.full(len(metas), 0.5)

    ranker = (extra.get("learning_to_rank") or {})
    if ranker.get("estimator") is not None:
        predicted = ranker["estimator"].predict(Z)
        calibration = ranker.get("calibration") or []
        p_rank = np.array([backend.rank_probability(float(v), calibration) for v in predicted])
    else:
        p_rank = None  # 生產 fallback 是 win_probability，逐配置時處理

    for idx, meta in enumerate(metas):
        meta["pLog"] = float(p_log[idx])
        meta["pXgb"] = float(p_xgb[idx]) if p_xgb is not None else None
        meta["pLgb"] = float(p_lgb[idx]) if p_lgb is not None else None
        meta["pGb"] = float(p_gb[idx]) if p_gb is not None else None
        meta["pXgbCal"] = float(p_xgb_cal[idx]) if p_xgb_cal is not None else None
        meta["pLgbCal"] = float(p_lgb_cal[idx]) if p_lgb_cal is not None else None
        meta["pGbCal"] = float(p_gb_cal[idx]) if p_gb_cal is not None else None
        meta["pAnom"] = float(p_anom[idx])
        meta["pRank"] = float(p_rank[idx]) if p_rank is not None else None
    return metas


# (win權重: logistic/xgb/lgb/gb, core權重: win/rank/anomaly/setup調整)
# calibrated=True 的配置：win分量的xgb/lgb/gb改用「驗證集輸出分佈百分位」
# 校準後的機率(消除positive_weight加權的機率偏高)，其餘邏輯完全相同——
# 可以直接跟同權重的原始版配置對照，分離「校準本身」的效果。
CONFIGS = {
    "current":        {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.44, 0.34, 0.14, 0.08)},
    "win_equal":      {"win": (0.25, 0.25, 0.25, 0.25), "core": (0.44, 0.34, 0.14, 0.08)},
    "lgbm_heavy_win": {"win": (0.10, 0.10, 0.70, 0.10), "core": (0.44, 0.34, 0.14, 0.08)},
    "logistic_only":  {"win": (1.0, 0.0, 0.0, 0.0),     "core": (0.44, 0.34, 0.14, 0.08)},
    "win_heavy_core": {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.62, 0.20, 0.10, 0.08)},
    "rank_heavy_core": {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.28, 0.52, 0.12, 0.08)},
    "no_anomaly":     {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.52, 0.40, 0.0, 0.08)},
    "pure_win":       {"win": (0.22, 0.22, 0.34, 0.22), "core": (1.0, 0.0, 0.0, 0.0)},
    "pure_rank":      {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.0, 1.0, 0.0, 0.0)},
    "current_calibrated":   {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.44, 0.34, 0.14, 0.08), "calibrated": True},
    "win_equal_calibrated": {"win": (0.25, 0.25, 0.25, 0.25), "core": (0.44, 0.34, 0.14, 0.08), "calibrated": True},
    "win_heavy_calibrated": {"win": (0.22, 0.22, 0.34, 0.22), "core": (0.62, 0.20, 0.10, 0.08), "calibrated": True},
    "pure_win_calibrated":  {"win": (0.22, 0.22, 0.34, 0.22), "core": (1.0, 0.0, 0.0, 0.0), "calibrated": True},
}


def score_record(meta, config):
    ww = config["win"]
    use_calibrated = bool(config.get("calibrated"))

    def pick(raw_key, cal_key):
        if use_calibrated and meta.get(cal_key) is not None:
            return meta[cal_key]
        return meta.get(raw_key)

    parts = [(meta["pLog"], ww[0])]
    p_xgb = pick("pXgb", "pXgbCal")
    p_lgb = pick("pLgb", "pLgbCal")
    p_gb = pick("pGb", "pGbCal")
    if p_xgb is not None:
        parts.append((p_xgb, ww[1]))
    if p_lgb is not None:
        parts.append((p_lgb, ww[2]))
    if p_gb is not None:
        parts.append((p_gb, ww[3]))
    weight_sum = sum(weight for _, weight in parts) or 1
    win = sum(value * weight for value, weight in parts) / weight_sum
    rank = meta["pRank"] if meta["pRank"] is not None else win
    c = config["core"]
    core = clamp(win * c[0] + rank * c[1] + meta["pAnom"] * c[2] + (meta["setup"] - 0.5) * c[3] - meta["risk"], 0.01, 0.99)
    adv_weight = 0.20 * meta["advCov"]
    blended = clamp(core * (1 - adv_weight) + meta["advSignal"] * adv_weight, 0.01, 0.99)
    return apply_market_adjustment(blended, meta)


def apply_market_adjustment(probability, meta):
    # 逐行複製 ml_backend.py adjust_probability_for_market：前兩個乘數是同日
    # 全市場共用常數(不影響排序，但為了絕對數值精確仍套用)，第三個是個股層級
    # (改變排序，這是回測第一版遺漏、對抗式驗證抓到的缺口)。
    adjusted = probability
    if meta["taiexMaGap"] < 0:
        adjusted *= 0.85
    if meta["marketRegime"] < 0:
        adjusted *= 0.88
    if meta["stockVsTaiex20"] <= 0:
        adjusted *= 0.92
    return clamp(adjusted, 0.01, 0.99)


def aggregate(trades):
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n <= 0]
    return {
        "trades": len(trades),
        "avgNetPct": round(sum(nets) / len(nets) * 100, 3),
        "hit10Rate": round(sum(t["y"] for t in trades) / len(trades), 4),
        "winRate": round(len(wins) / len(trades), 4),
        "profitFactor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else None,
    }


def _rank_daily(records, scorer, gate=lambda _meta: True, top_n=TOP_N, production_preselect=False):
    by_date = {}
    for meta in records:
        by_date.setdefault(meta["date"], []).append(meta)
    picks = []
    for day in by_date.values():
        if len(day) < MIN_CANDIDATES_PER_DAY:
            continue
        if production_preselect:
            day = sorted(
                (meta for meta in day if meta["radarGate"]),
                key=lambda meta: meta["quickScore"],
                reverse=True,
            )[:100]
        eligible = [meta for meta in day if gate(meta)]
        picks.extend(sorted(eligible, key=scorer, reverse=True)[:top_n])
    return picks


def aggregate_with_costs(trades):
    base = aggregate(trades)
    if not trades:
        return base
    base["costScenarios"] = {}
    for name, extra_cost in (("base", 0.0), ("conservative_1p2pct", 0.0065), ("pessimistic_1p8pct", 0.0125)):
        adjusted = [{**trade, "net": trade["net"] - extra_cost} for trade in trades]
        base["costScenarios"][name] = aggregate(adjusted)
    return base


def build_monster_quality_report(metas, oos_boundary):
    """同候選池比較型態分與正式 ensemble，另留最後半段 OOS 當 holdout。"""
    oos_dates = sorted({meta["date"] for meta in metas if meta["date"] >= oos_boundary})
    holdout_boundary = oos_dates[len(oos_dates) // 2]
    ensemble_scores = {id(meta): score_record(meta, CONFIGS["current"]) for meta in metas}

    rankings = {
        "pattern_radar_gate": (
            lambda meta: meta["quickScore"],
            lambda meta: meta["radarGate"],
        ),
        "pattern_safe": (
            lambda meta: meta["quickScore"],
            lambda meta: meta["buyAllowed"],
        ),
        "ensemble_radar_gate": (
            lambda meta: ensemble_scores[id(meta)],
            lambda meta: meta["radarGate"],
        ),
        "ensemble_safe": (
            lambda meta: ensemble_scores[id(meta)],
            lambda meta: meta["buyAllowed"],
        ),
        "ensemble_hard_risk_safe": (
            lambda meta: ensemble_scores[id(meta)],
            lambda meta: meta["hardRiskSafe"],
        ),
        "ensemble_hard_risk_trend_safe": (
            lambda meta: ensemble_scores[id(meta)],
            lambda meta: meta["hardRiskSafe"] and meta["trendSafe"],
        ),
        "ensemble_setup_ready": (
            lambda meta: ensemble_scores[id(meta)],
            lambda meta: meta["setupReady"] and meta["hardRiskSafe"],
        ),
        "hybrid_25pattern_75ensemble_safe": (
            lambda meta: meta["quickScore"] / 100 * 0.25 + ensemble_scores[id(meta)] * 0.75,
            lambda meta: meta["buyAllowed"],
        ),
    }
    report = {"holdoutBoundary": holdout_boundary, "rankings": {}}
    for name, (scorer, gate) in rankings.items():
        report["rankings"][name] = {}
        for top_n in (1, 3, 5, 10):
            picks = _rank_daily(
                metas,
                scorer,
                gate,
                top_n=top_n,
                production_preselect=True,
            )
            oos = [meta for meta in picks if meta["date"] >= oos_boundary]
            holdout = [meta for meta in picks if meta["date"] >= holdout_boundary]
            report["rankings"][name][f"top{top_n}"] = {
                "allOos": aggregate_with_costs(oos),
                "lateHoldout": aggregate_with_costs(holdout),
            }
    return report


def run():
    metas = build_component_records()
    by_date = {}
    for meta in metas:
        by_date.setdefault(meta["date"], []).append(meta)
    dates = sorted(by_date)
    expanded = sorted(m["date"] for m in metas)
    oos_boundary = expanded[int(len(expanded) * 0.8)]
    print(f"日期 {dates[0]} ~ {dates[-1]}，OOS 邊界 {oos_boundary}", flush=True)

    result = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "oosBoundary": oos_boundary,
              "topN": TOP_N, "configs": {}, "oosMonthly": {}}
    for name, config in CONFIGS.items():
        picks = []
        for date in dates:
            day = by_date[date]
            if len(day) < MIN_CANDIDATES_PER_DAY:
                continue
            ranked = sorted(day, key=lambda meta: score_record(meta, config), reverse=True)
            picks.extend(ranked[:TOP_N])
        oos = [t for t in picks if t["date"] >= oos_boundary]
        result["configs"][name] = {"all": aggregate(picks), "oos": aggregate(oos)}
        monthly = {}
        for t in oos:
            monthly.setdefault(t["date"][:7], []).append(t)
        result["oosMonthly"][name] = {month: aggregate(ts) for month, ts in sorted(monthly.items())}
        print(f"{name}: OOS {result['configs'][name]['oos']}", flush=True)

    result["monsterQuality"] = build_monster_quality_report(metas, oos_boundary)
    for name, stats in result["monsterQuality"]["rankings"].items():
        print(f"monster {name}: {stats}", flush=True)

    with open("backtest_ensemble_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("完整結果已寫入 backtest_ensemble_result.json", flush=True)


if __name__ == "__main__":
    run()
