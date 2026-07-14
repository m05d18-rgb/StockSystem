"""
每日前10名推薦回測：驗證「妖股雷達分數」是不是真的推薦分數。

方法：
  1. 對液態妖股池(~700檔)的每一個歷史交易日，用「當天以前」的資料重建排名
     (無前視)，取前 10 名模擬買進。
  2. 每筆交易用 short_term_target 模擬(進場次日開盤+滑點手續費、+10%/10日
     鎖利、-7%停損、量縮出場、時間停損、最長20日)——跟訓練標籤/實際策略
     同一套規則。
  3. 比較四種排名法的前10名績效，找出「真的會漲」的排名做為推薦分數依據：
     - radar_like：現行快篩分數(量能/動能/相對強度) + 現行入選閘門
     - model_prob：正式模型 logistic 機率 P(10日內+10%)
     - blend：閘門 + 快篩與模型機率各半
     - random：隨機基準(對照組)
     另附 universe_avg(當日全體候選平均)當市場基準。
  4. 誠實區分 in-sample / out-of-sample：模型是用 80/20 按日期切分訓練的，
     只有最近 ~20% 日期的樣本是模型沒看過的(OOS)——模型相關排名的真實
     能力以 OOS 段為準。

限制(誠實聲明)：
  - 候選池用「今天的」liquid_monster_universe 回放過去(存活者偏差：已下市
    股票不在池內，績效可能高估)。
  - radar_gate/quick_score 已對齊 ml_backend.py 的 quick_monster_filter 全部
    條件(含 liquidity_ok 均量/成交額門檻、month_high_strength 接近月高判斷腿)。

執行：python backtest_top10.py  (結果寫到 backtest_top10_result.json)
"""
import hashlib
import json
import math
import sys
import time

if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ml_backend import (
    FEATURE_NAMES,
    MIN_MONSTER_AVG_VOLUME_LOTS,
    MIN_MONSTER_TURNOVER_MILLION,
    backend,
    sigmoid,
)

IDX_RET5 = FEATURE_NAMES.index("ret_5")
IDX_RET20 = FEATURE_NAMES.index("ret_20")
IDX_VR = FEATURE_NAMES.index("volume_ratio")
IDX_REL = FEATURE_NAMES.index("stock_vs_taiex_20")

TOP_N = 10
MIN_CANDIDATES_PER_DAY = 30


def deterministic_noise(symbol, date):
    # 隨機基準要可重現：用 hashlib(不受 PYTHONHASHSEED 隨機化影響)當偽隨機分數
    digest = hashlib.md5(f"{symbol}:{date}".encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 100000) / 100000


def build_records():
    symbols = backend.liquid_monster_universe(800)
    model = backend.load_model()
    if not model:
        raise RuntimeError("model.pkl 不可用")
    means, stdevs, weights = model["means"], model["stdevs"], model["weights"]
    n_features = len(FEATURE_NAMES)
    records = []
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
                previous_close = float(rows[index - 1].get("close") or 0) if index > 0 else 0
                change1 = ((float(rows[index]["close"]) - previous_close) / previous_close * 100) if previous_close > 0 else 0.0
                latest_close = float(rows[index].get("close") or 0)
                latest_volume = float(rows[index].get("volume") or 0)
                volume_window = rows[max(0, index - 19):index + 1]
                volume_values = [float(row.get("volume") or 0) for row in volume_window]
                avg_volume20_lots = (sum(volume_values) / len(volume_values)) / 1000 if volume_values else 0.0
                # 舊語意的量能比(分母含當日,即volume_ratio修復前的定義)：
                # 決定性實驗用——radar快篩分數的排序品質可能依賴舊語意，
                # 而模型特徵已實證偏好新語意(排除當日)，兩個消費端可以
                # 各用各的定義，不必二選一。
                avg_volume_incl = sum(volume_values) / len(volume_values) if volume_values else 0.0
                vr_incl = (latest_volume / avg_volume_incl) if avg_volume_incl > 0 else 0.0
                turnover_million = (latest_volume * latest_close) / 1_000_000
                recent_high = max((row["high"] for row in rows[max(0, index - 20):index]), default=rows[index]["high"])
                month_high_strength = latest_close >= float(recent_high or 0) * 0.995 if recent_high else False
                z = [(x[j] - means[j]) / stdevs[j] for j in range(n_features)]
                prob = sigmoid(weights[0] + sum(z[j] * weights[j + 1] for j in range(n_features)))
                records.append({
                    "symbol": symbol,
                    "date": item["date"],
                    "ret5": x[IDX_RET5] * 100,
                    "ret20": x[IDX_RET20] * 100,
                    "vr": x[IDX_VR],
                    "rel": x[IDX_REL],
                    "change1": change1,
                    "prob": prob,
                    "y": target["y"],
                    "net": target["net_return"],
                    # 回測基建：短線出場模擬器算好的出場原因與持有天數，之前沒帶進
                    # record，導致「淨報酬的洞在哪個出場路徑」量不出來。純新增診斷欄，
                    # 不改任何策略/標籤/排序數學。
                    "exitReason": target.get("exit_reason"),
                    "holdDays": target.get("hold_days"),
                    "earlyDip5": target.get("early_dip5"),
                    "earlyDip4": target.get("early_dip4"),
                    "earlyDip6": target.get("early_dip6"),
                    "avgVolume20Lots": avg_volume20_lots,
                    "turnoverMillion": turnover_million,
                    "monthHighStrength": month_high_strength,
                    "vrIncl": vr_incl,
                })
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)
    return records


def radar_gate(rec, vr_min=1.2, vr_key="vrIncl"):
    # 2026-07-07 妖股重定義：純型態/量能(流動性+量能放大+比大盤強),拿掉所有 ret5
    # %-gain 選股條件。回測 radar_pure OOS avgNet 0.742% vs 舊 0.720%(持平微升)。
    # 與 ml_backend.quick_monster_filter 的 ok 同步——改任一邊都要重跑本檔。
    stronger = rec["rel"] > 0
    liquidity_ok = (
        rec["avgVolume20Lots"] >= MIN_MONSTER_AVG_VOLUME_LOTS
        and rec["turnoverMillion"] >= MIN_MONSTER_TURNOVER_MILLION
    )
    return liquidity_ok and rec[vr_key] >= vr_min and stronger


def quick_score(rec, vr_cap=4.0, log_scale=False, vr_key="vrIncl"):
    # 2026-07-07 改用 pattern_strict：拿掉 ret5/ret20 漲幅 magnitude，只留妖股型態
    # (量能/突破月高/比大盤強/逆勢)。OOS avgNet 0.522%→0.720%、PF 1.11→1.16、
    # 命中率 33.2%→32.9%(改良在少賠不在多命中)。與 ml_backend.quick_monster_filter
    # 的 rawScore 及 monster_score_for_symbol 的顯示分同步——改任一邊都要重跑本檔。
    # 舊動能版(量能34/ret5 30/ret20 14/強14/月高12/紅6/逆勢4)見 git 與 #165 註解。
    # vr_cap/log_scale 保留簽名相容;pattern_strict 不用 log_scale。
    counter_strength = rec["rel"] > 0 and (
        rec["monthHighStrength"] or rec["ret5"] >= 5 or (rec["change1"] >= 3.5 and rec["ret5"] >= 0)
    )
    vol_component = min(rec[vr_key] / vr_cap, 1) * 44
    return (
        vol_component
        + (34 if rec["monthHighStrength"] else 0)
        + (16 if rec["rel"] > 0 else 0)
        + (6 if counter_strength else 0)
    )


RANKINGS = {
    "radar_like": lambda rec: quick_score(rec) if radar_gate(rec) else -1,
    # 2026-07-09 追高門檻實驗結論(使用者反映近期推薦追高後回檔,測「排除5日已漲>=cap%的追高股」):
    #   radar_like(無 cap) OOS avgNet 0.717% / 命中33.0% / PF1.158。
    #   cap18=0.655% / cap15=0.579% / cap12=0.639% —— 全部「更差」(命中也降到29.5%),
    #   收緊追高門檻是 logical-but-worse 陷阱(近期弱市的追高回檔是行情beta+薄edge噪音,
    #   非可修的系統性偏差)。cap22=0.790% 微幅較好但≈現行 overheat(ret5>22)已在做的事,
    #   等於「維持現狀」。且扣保守成本1.2%後全部逼近0(baseline 0.067%)、悲觀1.8%全負。
    #   → 決定:**不改 buy_allowed 的追高/過熱門檻**(現行 22% 已近最佳),改動已 revert。
    "model_prob": lambda rec: rec["prob"],
    "blend": lambda rec: (quick_score(rec) / 100 * 0.5 + rec["prob"] * 0.5) if radar_gate(rec) else -1,
    "random": lambda rec: deterministic_noise(rec["symbol"], rec["date"]),
    # 2026-07-03 #163實驗結論：radar_like/blend 的快篩分數+閘門改用舊語意
    # 量能比(vrIncl，分母含當日)——OOS avgNet 0.794% vs 新語意 0.551%、
    # 逐月6/8個月較好；調飽和上限(/6、/8、log)與閘門(1.5)的5組變體全部
    # 無效(0.49-0.55%)。模型特徵(rec["vr"]=values[7])維持新語意(model_prob
    # 實證較好)。radar_gate/quick_score 的 vr_key 預設已改為 "vrIncl"，
    # 與 ml_backend.quick_monster_filter 的正式邏輯對齊。
    # 2026-07-03 #165實驗結論：兩組「更激進偏飆股」變體(A:ret5上限20%權重34
    # 拿掉逆勢加分；B:極端動能導向vr38+ret5 38)OOS avgNet 0.658%/0.661%，
    # 都輸現行 radar_like 0.794%(A逐月只贏3/8)。現行 quick_score 的偏飆股
    # 權重(量能34/ret5 30/月高12)已是驗證過的最佳配置，變體已移除。
}


def _cost_layer(nets, extra):
    """把每筆 net 再扣一層額外來回成本(extra，小數)後重算 avgNet/PF/win。
    net 本身已含 short_term_target 的 ~0.55% 基礎成本，這裡疊加更保守的假設。"""
    adj = [n - extra for n in nets]
    w = [n for n in adj if n > 0]
    l = [-n for n in adj if n <= 0]
    return {
        "avgNetPct": round(sum(adj) / len(adj) * 100, 3),
        "winRate": round(len(w) / len(adj), 4),
        "profitFactor": round(sum(w) / sum(l), 3) if l and sum(l) > 0 else None,
    }


def aggregate(trades):
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n <= 0]
    result = {
        "trades": len(trades),
        "avgNetPct": round(sum(nets) / len(nets) * 100, 3),
        "hit10Rate": round(sum(t["y"] for t in trades) / len(trades), 4),
        "winRate": round(len(wins) / len(trades), 4),
        "profitFactor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else None,
    }
    # === 回測基建(加強 do-first)：以下皆純新增診斷，不改上面任何既有指標 ===
    # 出場原因分佈：一眼看出「淨報酬的洞在哪個出場路徑」——stop_loss 佔比高/
    # avgNet 很負 = 輸家重災區(對應評估的『命中率高但輸家更重』悖論)。
    reason_stats = {}
    for t in trades:
        r = t.get("exitReason") or "unknown"
        s = reason_stats.setdefault(r, {"count": 0, "netSum": 0.0})
        s["count"] += 1
        s["netSum"] += t["net"]
    if any(t.get("exitReason") for t in trades):
        result["exitReasonBreakdown"] = {
            r: {
                "count": s["count"],
                "share": round(s["count"] / len(trades), 4),
                "avgNetPct": round(s["netSum"] / s["count"] * 100, 3),
            }
            for r, s in sorted(reason_stats.items(), key=lambda kv: -kv[1]["count"])
        }
    hold_vals = [t.get("holdDays") for t in trades if t.get("holdDays") is not None]
    if hold_vals:
        result["avgHoldDays"] = round(sum(hold_vals) / len(hold_vals), 2)
    # 成本敏感度：net 已含 ~0.55% 基礎成本，再疊保守(~1.2%總)/悲觀(~1.8%總)來回
    # 成本看 avgNet/PF 掉多少——把「edge 薄」變成進場前能擋真金白銀的硬數字。
    result["costScenarios"] = {
        "base_0p55pct": _cost_layer(nets, 0.0),
        "conservative_1p2pct": _cost_layer(nets, 0.0065),
        "pessimistic_1p8pct": _cost_layer(nets, 0.0125),
    }
    return result


def run():
    records = build_records()
    print(f"共 {len(records)} 筆 (symbol, day) 紀錄", flush=True)
    by_date = {}
    for rec in records:
        by_date.setdefault(rec["date"], []).append(rec)
    dates = sorted(by_date)
    # OOS 邊界＝訓練 80/20 按日期切分的分界(樣本第80百分位的日期)
    all_dates_expanded = sorted(rec["date"] for rec in records)
    oos_boundary = all_dates_expanded[int(len(all_dates_expanded) * 0.8)]
    print(f"日期範圍 {dates[0]} ~ {dates[-1]}，OOS 邊界 {oos_boundary}", flush=True)

    picks = {name: [] for name in RANKINGS}
    universe_all = []
    for date in dates:
        day_records = by_date[date]
        if len(day_records) < MIN_CANDIDATES_PER_DAY:
            continue
        universe_all.extend(day_records)
        for name, scorer in RANKINGS.items():
            scored = [(scorer(rec), rec) for rec in day_records]
            scored = [pair for pair in scored if pair[0] >= 0]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            picks[name].extend(rec for _, rec in scored[:TOP_N])

    result = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "oosBoundary": oos_boundary,
              "dateRange": [dates[0], dates[-1]], "topN": TOP_N, "rankings": {}}
    for name in RANKINGS:
        all_trades = picks[name]
        oos_trades = [t for t in all_trades if t["date"] >= oos_boundary]
        result["rankings"][name] = {"all": aggregate(all_trades), "oos": aggregate(oos_trades)}
    result["rankings"]["universe_avg"] = {
        "all": aggregate(universe_all),
        "oos": aggregate([t for t in universe_all if t["date"] >= oos_boundary]),
    }
    # OOS 逐月表(看穩定性，不是只看單一平均)
    monthly = {}
    for name in (key for key in RANKINGS if key != "random"):
        for t in picks[name]:
            if t["date"] < oos_boundary:
                continue
            month = t["date"][:7]
            monthly.setdefault(month, {}).setdefault(name, []).append(t)
    result["oosMonthly"] = {
        month: {name: aggregate(trades) for name, trades in by_name.items()}
        for month, by_name in sorted(monthly.items())
    }
    with open("backtest_top10_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result["rankings"], ensure_ascii=False, indent=2), flush=True)
    print("完整結果已寫入 backtest_top10_result.json", flush=True)


if __name__ == "__main__":
    run()
