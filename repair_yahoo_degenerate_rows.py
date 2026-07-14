#!/usr/bin/env python3
"""
repair_yahoo_degenerate_rows.py — 一次性清理 Yahoo Finance fallback 髒資料

背景：調查 0050/6919 疑似除權息跳空異常時發現兩種 Yahoo Finance fallback
相關的資料品質 bug：
  1. fetch_yahoo_chart_rows() 過去沒有過濾「開高低收全部相等、成交量為 0」
     的假交易日（Yahoo 資料本身在缺資料時的填補瑕疵，不是真的停牌），導致
     這種假資料被當成真實價格寫進 prices 表。
  2. fetch_symbol_rows() 補 FinMind 單日壞資料時，沒檢查 Yahoo 補值的尺度
     是否跟前後 FinMind 原始股價一致——Yahoo 的歷史股價會因除權息/分割回溯
     調整，混進單一天會造成憑空跳空又跳回(例如 6919 在 2024-03-21 附近、
     或其他股票尺度剛好差了 2.35 倍的案例)。
兩種 bug 都已經在 ml_backend.py 修好(防止未來繼續產生)，這支腳本負責清理
已經寫進資料庫的歷史髒資料。

修復策略（每個受影響的股票代號）：
  1. 只打 FinMind TaiwanStockPrice 一個資料集（不用完整 fetch_symbol_rows 的
     全部資料集，省下大量 API 額度），涵蓋從最早一筆髒資料日期到今天。
  2. FinMind 有該日期的正常資料就用 FinMind 覆蓋（COALESCE upsert，只動
     OHLCV+price_source，不動籌碼/財務等其他欄位）。
  3. FinMind 也沒有的日期，改用新版(已加過濾)的 Yahoo fallback 重抓一次，
     這次如果還是回傳假資料會被新的過濾規則擋掉，等於自動確認「真的抓不到」。
  4. 兩種來源都拿不到有效資料的日期，直接刪除該筆髒資料，保留缺口――缺口
     比留著錯誤數字安全，下游計算本來就有處理缺資料的邏輯。

用法：
  python repair_yahoo_degenerate_rows.py            # 全部受影響股票
  python repair_yahoo_degenerate_rows.py --limit 20  # 先跑前 20 檔試水溫
  python repair_yahoo_degenerate_rows.py --symbols 6919,0050  # 只跑指定股票
"""
import argparse
import datetime as dt
import json
import sys
import time

from ml_backend import backend, price_scale_is_plausible, read_finmind_token, today_key

LOG_PATH = "repair_yahoo_degenerate_rows.log"
DELETE_BACKUP_PATH = "repair_yahoo_deleted_rows_backup.jsonl"


def backup_rows_before_delete(conn, symbol, dates):
    """DELETE 是不可逆操作，刪除前先把完整列內容備份成一行一筆的 JSON，
    之後如果 find_degenerate_rows() 的偵測邏輯誤判，還能從備份還原。"""
    if not dates:
        return
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"SELECT symbol, date, open, high, low, close, volume, price_source "
        f"FROM prices WHERE symbol = ? AND date IN ({placeholders}) AND price_source LIKE '%Yahoo%fallback%'",
        [symbol, *dates],
    ).fetchall()
    if not rows:
        return
    with open(DELETE_BACKUP_PATH, "a", encoding="utf-8") as backup_file:
        for row in rows:
            record = {
                "deletedAt": dt.datetime.now().isoformat(),
                "symbol": row[0], "date": row[1],
                "open": row[2], "high": row[3], "low": row[4], "close": row[5],
                "volume": row[6], "price_source": row[7],
            }
            backup_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_degenerate_rows():
    """找出兩類受影響的列：(a) 開高低收全部相等+成交量0 的假交易日，
    (b) Yahoo fallback 來源、但跟前後鄰居收盤價比對呈現尺度不一致的孤立列。"""
    with backend.connect() as conn:
        conn.row_factory = None
        degenerate = conn.execute("""
            SELECT symbol, date
            FROM prices
            WHERE price_source LIKE '%Yahoo%fallback%'
              AND open = high AND high = low AND low = close
              AND (volume IS NULL OR volume <= 0)
        """).fetchall()

        yahoo_symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM prices WHERE price_source LIKE '%Yahoo%fallback%'"
        ).fetchall()]

    scale_mismatch = []
    for symbol in yahoo_symbols:
        with backend.connect() as conn:
            conn.row_factory = None
            symbol_rows = conn.execute(
                "SELECT date, close, price_source FROM prices WHERE symbol = ? ORDER BY date", (symbol,)
            ).fetchall()
        for i in range(1, len(symbol_rows) - 1):
            date, close, source = symbol_rows[i]
            if "Yahoo" not in (source or "") or "fallback" not in (source or ""):
                continue
            prev_close = symbol_rows[i - 1][1]
            next_close = symbol_rows[i + 1][1]
            if not prev_close or not next_close or prev_close <= 0 or next_close <= 0:
                continue
            # 不要求前後兩天彼此接近才判定可疑——即使前後之間真的有一段真實的
            # 大漲/大跌(例如減資、重整)，中間這筆 Yahoo 補值只要跟前後任何一邊
            # 都對不上尺度，就代表它既不是轉折前、也不是轉折後的合理價格，
            # 单純是尺度錯誤，不是真的反映了哪一天的行情。
            matches_prev = price_scale_is_plausible(close, prev_close)
            matches_next = price_scale_is_plausible(close, next_close)
            if not matches_prev and not matches_next:
                scale_mismatch.append((symbol, date))

    rows = list(degenerate) + scale_mismatch
    by_symbol = {}
    for symbol, date in rows:
        dates = by_symbol.setdefault(symbol, [])
        if date not in dates:
            dates.append(date)
    return by_symbol


def repair_symbol(symbol, bad_dates, token):
    earliest = min(bad_dates)
    fixed_via_finmind = 0
    fixed_via_yahoo = 0
    deleted = 0
    errors = []

    finmind_by_date = {}
    try:
        data = backend.fetch_finmind_dataset("TaiwanStockPrice", symbol, earliest, today_key(), token)
        for item in data:
            date = item.get("date")
            if date:
                finmind_by_date[date] = item
    except Exception as exc:
        errors.append(f"FinMind TaiwanStockPrice: {exc}")

    still_bad = []
    finmind_rows_to_write = []
    for date in bad_dates:
        item = finmind_by_date.get(date)
        if not item:
            still_bad.append(date)
            continue
        open_v = backend.safe_float(item.get("open"))
        high_v = backend.safe_float(item.get("max"))
        low_v = backend.safe_float(item.get("min"))
        close_v = backend.safe_float(item.get("close"))
        volume_v = backend.safe_float(item.get("Trading_Volume"))
        if not all(v is not None and v > 0 for v in (open_v, high_v, low_v, close_v)):
            still_bad.append(date)
            continue
        finmind_rows_to_write.append({
            "symbol": symbol, "date": date,
            "open": open_v, "high": high_v, "low": low_v, "close": close_v,
            "volume": volume_v, "price_source": "FinMind TaiwanStockPrice",
        })
        fixed_via_finmind += 1

    # 找最接近的有效 FinMind 收盤價當尺度比對基準，避免 Yahoo 重抓回來的值
    # 又是同一種尺度不一致的髒資料(直接照收就等於沒修到)。
    sorted_finmind_dates = sorted(
        date for date, item in finmind_by_date.items()
        if all((backend.safe_float(item.get(f)) or 0) > 0 for f in ("open", "max", "min", "close"))
    )

    def _reference_close(date):
        before = [d for d in sorted_finmind_dates if d < date]
        if before:
            return backend.safe_float(finmind_by_date[before[-1]].get("close"))
        after = [d for d in sorted_finmind_dates if d > date]
        if after:
            return backend.safe_float(finmind_by_date[after[0]].get("close"))
        return None

    yahoo_rows_to_write = []
    if still_bad:
        try:
            days_needed = (dt.date.today() - dt.date.fromisoformat(earliest)).days + 30
            yahoo_rows = {row["date"]: row for row in backend.fetch_yahoo_fallback_price_rows(symbol, days_needed)}
        except Exception as exc:
            yahoo_rows = {}
            errors.append(f"Yahoo fallback: {exc}")
        remaining = []
        for date in still_bad:
            row = yahoo_rows.get(date)
            reference_close = _reference_close(date)
            if row and reference_close and price_scale_is_plausible(row["close"], reference_close):
                yahoo_rows_to_write.append({
                    "symbol": symbol, "date": date,
                    "open": row.get("open") or row["close"], "high": row.get("high") or row["close"],
                    "low": row.get("low") or row["close"], "close": row["close"],
                    "volume": row.get("volume") or 0, "price_source": row.get("price_source"),
                })
                fixed_via_yahoo += 1
            else:
                remaining.append(date)
        still_bad = remaining

    if finmind_rows_to_write or yahoo_rows_to_write:
        with backend.connect() as conn:
            backend.upsert_price_rows(finmind_rows_to_write + yahoo_rows_to_write, conn)

    if still_bad:
        with backend.connect() as conn:
            backup_rows_before_delete(conn, symbol, still_bad)
            conn.executemany(
                "DELETE FROM prices WHERE symbol = ? AND date = ? AND price_source LIKE '%Yahoo%fallback%'",
                [(symbol, date) for date in still_bad],
            )
            conn.commit()
        deleted = len(still_bad)

    return {
        "symbol": symbol, "total": len(bad_dates),
        "fixedViaFinmind": fixed_via_finmind, "fixedViaYahoo": fixed_via_yahoo,
        "deleted": deleted, "errors": errors,
    }


def run(symbols=None, limit=None):
    by_symbol = find_degenerate_rows()
    target_symbols = symbols or sorted(by_symbol.keys())
    if limit:
        target_symbols = target_symbols[:limit]

    token = read_finmind_token()
    summary = {
        "totalSymbols": len(target_symbols), "processed": 0,
        "fixedViaFinmind": 0, "fixedViaYahoo": 0, "deleted": 0,
        "symbolErrors": [],
    }
    with open(LOG_PATH, "a", encoding="utf-8") as log_file:
        started_at = dt.datetime.now().isoformat()
        log_file.write(f"\n=== 開始清理：{started_at}，共 {len(target_symbols)} 檔 ===\n")
        for index, symbol in enumerate(target_symbols, 1):
            bad_dates = by_symbol.get(symbol) or []
            if not bad_dates:
                continue
            try:
                result = repair_symbol(symbol, bad_dates, token)
            except Exception as exc:
                result = {"symbol": symbol, "total": len(bad_dates), "fixedViaFinmind": 0, "fixedViaYahoo": 0, "deleted": 0, "errors": [str(exc)]}
            summary["processed"] += 1
            summary["fixedViaFinmind"] += result["fixedViaFinmind"]
            summary["fixedViaYahoo"] += result["fixedViaYahoo"]
            summary["deleted"] += result["deleted"]
            if result["errors"]:
                summary["symbolErrors"].append({"symbol": symbol, "errors": result["errors"]})
            log_file.write(
                f"[{index}/{len(target_symbols)}] {symbol}: total={result['total']} "
                f"finmind={result['fixedViaFinmind']} yahoo={result['fixedViaYahoo']} "
                f"deleted={result['deleted']} errors={result['errors']}\n"
            )
            log_file.flush()
            time.sleep(0.05)
    return summary


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="清理 Yahoo Finance fallback 假交易日歷史髒資料")
    parser.add_argument("--limit", type=int, default=None, help="只處理前 N 檔（測試用）")
    parser.add_argument("--symbols", type=str, default=None, help="只處理指定股票代號，逗號分隔")
    args = parser.parse_args()

    target_symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    result = run(symbols=target_symbols, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
