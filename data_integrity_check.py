#!/usr/bin/env python3
"""
data_integrity_check.py — 每日資料完整性檢查

檢查訓練清單裡每檔股票的價格/籌碼/財務資料是否正常匯入、模型本身是否
夠新鮮，讓資料缺口在排程階段就被發現，而不是等使用者在畫面上看到
「無資料」才回頭一個個查。

用法：
  python data_integrity_check.py                  # 檢查 DEFAULT_SYMBOLS(全產業基礎樣本)
  python data_integrity_check.py --full            # 檢查完整每日訓練池(含持股與動態候選)
  python data_integrity_check.py --stale-days 2    # 模型超過幾天沒重訓算過期(預設2天)
  python data_integrity_check.py --json            # 輸出 JSON，方便排程/其他程式解析
"""
import argparse
import datetime as dt
import json
import sys

from ml_backend import DEFAULT_SYMBOLS, MARKET_DATA_MAX_STALE_DAYS, backend, calendar_days_between, now_text, today_key

PRICE_ZERO_LOOKBACK_DAYS = 30


def check_symbol(symbol, expected_latest_date=""):
    issues = []
    raw_rows = backend.load_price_rows(symbol)
    rows = backend.rows_with_verified_sources(raw_rows)
    if not rows:
        # 區分「真的完全沒資料」跟「有資料但來源都不是官方」——兩者的
        # 修復方式不同(前者要補抓、後者要查來源為什麼不是官方)，訊息
        # 含糊會讓人誤判成沒抓過資料。
        message = "完全沒有價格資料" if not raw_rows else f"有 {len(raw_rows)} 筆價格資料，但來源皆非官方(price_source 未通過驗證)"
        return {
            "symbol": symbol,
            "ok": False,
            "latestDate": None,
            "issues": [message],
            "modelEligible": False,
            "eligibilityIssues": ["沒有可驗證的官方價格資料"],
        }

    latest = rows[-1]
    for field in ("open", "high", "low", "close"):
        value = latest.get(field)
        if value is None or value <= 0:
            issues.append(f"最新一筆 {field}={value} 異常")

    # 新鮮度檢查：這個模組存在的目的就是「讓資料缺口在排程階段被發現」，
    # 但只看「陣列最後 N 筆」或「總筆數/覆蓋率」完全抓不到「抓取管線持續
    # 故障、資料停在很久以前，但歷史累積筆數夠多、欄位齊全」這種最典型的
    # 情境——實測：250筆完整歷史但最新一筆停在31天前，舊版邏輯回傳
    # ok=True。跟 market_data_quality 的大盤過期偵測同一套原則，門檻沿用
    # 同一個常數，容忍週末/連續假期。
    latest_date = latest.get("date")
    expected_latest_date = str(expected_latest_date or "")[:10]
    stale_days = calendar_days_between(latest_date, today_key()) if latest_date else None
    if expected_latest_date and latest_date and latest_date < expected_latest_date:
        issues.append(
            f"最新資料日期 {latest_date}，落後全市場最後完整交易日 "
            f"{expected_latest_date}，疑似抓取管線斷更或標的已停止交易"
        )
    elif stale_days is not None and stale_days > MARKET_DATA_MAX_STALE_DAYS:
        # 全市場完整日期暫時不可用時才退回日曆天門檻；正常情況以上面的
        # 交易日基準判斷，避免週末、連假、颱風休市被算成資料斷更天數。
        issues.append(f"最新資料日期 {latest_date} 距今 {stale_days} 天，疑似抓取管線斷更")

    recent = rows[-PRICE_ZERO_LOOKBACK_DAYS:]
    zero_days = [
        row["date"] for row in recent
        if all((row.get(field) or 0) <= 0 for field in ("open", "high", "low", "close"))
    ]
    if zero_days:
        issues.append(f"近 {PRICE_ZERO_LOOKBACK_DAYS} 天有 {len(zero_days)} 天 OHLC 全零：{zero_days[:3]}")

    quality = backend.model_data_quality(symbol, rows)
    eligibility_issues = list(quality.get("missing") or [])

    # model_data_quality 是「獨立模型是否可納入訓練」的嚴格資格，不等同
    # 價格/資料管線故障。新上市、創新版或尚未具融資券資格的股票，真實日K
    # 可以完整且規則雷達可正常分析，但因缺少 120 日融資券歷史而不應進模型。
    # 兩者混成 issues 會把正常但樣本尚未成熟的股票誤報成系統壞掉。
    return {
        "symbol": symbol,
        "ok": not issues,
        "latestDate": latest.get("date"),
        "issues": issues,
        "modelEligible": bool(quality.get("ok")),
        "eligibilityIssues": eligibility_issues,
        "modelDataQuality": quality,
    }


# 殭屍預測稽核的「存量基準」meta key：首次上線時歷史遺留的逾期筆數直接
# 全報會是一大串假警報，先記成基準、只警示「相對基準的新增惡化」；
# 逾期數下降時基準跟著下修(棘輪)，讓警示對「重新開始惡化」保持敏感。
PREDICTION_OVERDUE_BASELINE_KEY = "prediction_overdue_baseline"
PREDICTION_OVERDUE_NEW_ALERT_THRESHOLD = 20  # 比基準多出超過20筆才列為問題


def check_prediction_settlement():
    """殭屍預測：逾期未結算的 prediction 會被靜默排除在戰績分母外，
    讓命中率偏樂觀。回傳 ok/issue + 明細。"""
    health = backend.prediction_settlement_health()
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (PREDICTION_OVERDUE_BASELINE_KEY,)
        ).fetchone()
    baseline = None
    if row:
        try:
            baseline = int(json.loads(row[0]).get("overdue"))
        except (json.JSONDecodeError, TypeError, ValueError):
            baseline = None
    if baseline is None or health["overdue"] < baseline:
        with backend.connect() as conn:
            backend.set_meta(conn, PREDICTION_OVERDUE_BASELINE_KEY, json.dumps({
                "overdue": health["overdue"], "recordedAt": now_text(),
            }))
        baseline = health["overdue"]
    new_overdue = health["overdue"] - baseline
    ok = new_overdue <= PREDICTION_OVERDUE_NEW_ALERT_THRESHOLD
    top_text = "、".join(f"{t['symbol']}×{t['count']}" for t in health["topSymbols"][:3])
    return {
        **health,
        "baseline": baseline,
        "newOverdue": new_overdue,
        "ok": ok,
        "issue": None if ok else (
            f"逾期未結算預測比基準多 {new_overdue} 筆(共 {health['overdue']} 筆，"
            f"最多的：{top_text})，結算管線可能故障，戰績命中率已開始偏斜"
        ),
    }


def check_model_gate():
    """重訓品質閘門狀態：今天有拒絕就要讓使用者知道(模型沒更新、正在用舊版)。"""
    state = backend.read_model_gate_state()
    rejected_today = str(state.get("lastRejectedAt") or "")[:10] == today_key()
    ok = not rejected_today
    return {
        **state,
        "ok": ok,
        "issue": None if ok else (
            f"今日重訓被品質閘門拒絕(連續第 {state['consecutiveRejects']} 次)，"
            f"沿用前一版模型：{state['lastRejectReason']}"
        ),
    }


def check_model_freshness(stale_days):
    model, load_error = backend.load_model_with_error()
    if not model:
        return {"ok": False, "issue": f"model.pkl 無法載入：{load_error}"}
    trained_at = model.get("trained_at") or ""
    try:
        trained_dt = dt.datetime.strptime(trained_at.replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return {"ok": False, "issue": f"trained_at 格式無法解析：{trained_at}"}
    # 2026-07-08：改用「交易日」而非日曆天算模型齡。重訓自今日起固定在收盤後 14:30
    # 且只在交易日執行(early 早上的完整性檢查跑在今天 14:30 重訓之前)。用日曆天的
    # (now-trained_dt).days 會讓「週一早上」誤判：模型是上週五 14:30 訓的、差約 66h→
    # days==2→門檻 2 天→假報「模型 2 天沒重訓」發 critical LINE(連假後更嚴重)，但模型
    # 其實新鮮。改成數 prices 表裡「比 trained_at 日期更新的交易日」有幾個：週末/連假
    # 沒有交易日,所以早上檢查通常=0;真正重訓停擺(累積 >= stale_days 個未訓的交易日)
    # 才示警。查 DB 失敗才 fail-safe 退回日曆天(寧可偶爾誤報也不漏報真故障)。
    trained_date = trained_at[:10]
    try:
        with backend.connect() as conn:
            trading_days_since = conn.execute(
                "SELECT COUNT(DISTINCT date) FROM prices WHERE date > ?", (trained_date,)
            ).fetchone()[0]
    except Exception:
        trading_days_since = (dt.datetime.now() - trained_dt).days
    ok = trading_days_since < stale_days
    return {
        "ok": ok,
        "trainedAt": trained_at,
        "ageDays": trading_days_since,
        "symbolCount": len(model.get("symbols") or []),
        "issue": None if ok else f"模型已經 {trading_days_since} 個交易日沒重訓（門檻 {stale_days} 交易日）",
    }


def run(symbols=None, stale_days=2):
    symbols = symbols or DEFAULT_SYMBOLS
    try:
        expected_latest_date = backend.latest_complete_price_date()
    except Exception:
        expected_latest_date = ""
    # check_symbol() 本身沒有防護，任何一檔股票的例外(DB連線瞬斷/資料型別
    # 異常)沒接住的話，list comprehension 會整個中斷，讓當天所有股票的檢查
    # 結果全部消失——呼叫端 daily_update.py 的 run_daily_data_integrity_check
    # 又是整包 try/except，最後只會回報一個籠統的 error，完全不知道是哪一檔
    # 出問題，其他檔案明明正常也拿不到結果，LINE 也不會收到「有問題」通知。
    results = []
    for symbol in symbols:
        try:
            results.append(check_symbol(symbol, expected_latest_date=expected_latest_date))
        except Exception as exc:
            results.append({"symbol": symbol, "ok": False, "latestDate": None, "issues": [f"檢查時發生例外：{exc}"]})
    bad = [r for r in results if not r["ok"]]
    ineligible = [r for r in results if not r.get("modelEligible", True)]
    model_check = check_model_freshness(stale_days)
    # 兩個系統級檢查各自防護：稽核函式本身出錯不能拖垮整包檢查結果
    try:
        gate_check = check_model_gate()
    except Exception as exc:
        gate_check = {"ok": True, "issue": None, "error": f"閘門狀態檢查失敗：{exc}"}
    try:
        settlement_check = check_prediction_settlement()
    except Exception as exc:
        settlement_check = {"ok": True, "issue": None, "error": f"殭屍預測稽核失敗：{exc}"}

    print(f"檢查時間：{now_text()}")
    print(f"檢查股票數：{len(symbols)}，正常：{len(results) - len(bad)}，有問題：{len(bad)}")
    print()
    if bad:
        print("=== 有問題的股票 ===")
        for r in bad:
            print(f"  {r['symbol']}（最新資料日期 {r['latestDate'] or '無'}）：")
            for issue in r["issues"]:
                print(f"    - {issue}")
        print()
    if ineligible:
        print("=== 獨立模型暫不納入（非資料管線故障） ===")
        for r in ineligible:
            print(f"  {r['symbol']}：{r.get('eligibilityIssues') or ['樣本資格不足']}")
        print()
    print("=== 模型新鮮度 ===")
    if model_check["ok"]:
        print(f"  OK，訓練於 {model_check['trainedAt']}（{model_check['ageDays']} 天前），涵蓋 {model_check['symbolCount']} 檔股票")
    else:
        print(f"  警告：{model_check['issue']}")
    if not gate_check["ok"]:
        print(f"=== 重訓品質閘門 ===\n  警告：{gate_check['issue']}")
    if not settlement_check["ok"]:
        print(f"=== 殭屍預測稽核 ===\n  警告：{settlement_check['issue']}")

    return {
        "ok": not bad and model_check["ok"] and gate_check["ok"] and settlement_check["ok"],
        "checkedAt": now_text(),
        "expectedLatestDate": expected_latest_date or None,
        "total": len(symbols),
        "problemCount": len(bad),
        "problems": bad,
        "modelIneligibleCount": len(ineligible),
        "modelIneligible": ineligible,
        "modelFreshness": model_check,
        "modelGate": gate_check,
        "predictionSettlement": settlement_check,
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="每日資料完整性檢查")
    parser.add_argument("--full", action="store_true", help="檢查完整每日訓練池(含持股與動態候選)，而非只檢查 DEFAULT_SYMBOLS")
    parser.add_argument("--stale-days", type=int, default=2, help="模型超過幾天沒重訓算過期")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 格式(方便排程/其他程式解析)")
    args = parser.parse_args()

    check_symbols = None
    if args.full:
        from daily_update import build_daily_training_symbols, load_sinopac_symbols, unique_codes
        try:
            portfolio_symbols = load_sinopac_symbols()["symbols"]
        except Exception:
            portfolio_symbols = unique_codes(DEFAULT_SYMBOLS)
        check_symbols = build_daily_training_symbols(portfolio_symbols)["symbols"]

    result = run(symbols=check_symbols, stale_days=args.stale_days)
    if args.full:
        # 手動全範圍檢查也代表「目前狀態」已刷新；不可只把結果印在終端，
        # 否則 API 仍會顯示早上排程留下的舊警告。
        from daily_update import persist_data_integrity_repair_queue
        result["statusPersistence"] = persist_data_integrity_repair_queue(result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
