"""用付費 FinMind 額度回補「當沖(TaiwanStockDayTrading)+借券(TaiwanStockSecuritiesLending)」
歷史,活化目前全 NULL 的 3 個死特徵:#21 daytrade_risk、#22 lending_risk、#37 daytrade_imbalance。

背景:fetch_optional 的白名單 FINMIND_OPTIONAL_SHORT_TERM_DATASETS 沒有這兩個 dataset,
導致 fetch_symbol_rows(1642/1645)呼叫它們時被白名單擋掉回 []=從沒真的抓過→特徵長期死。
資料其實完整(2603 當沖有757筆)。這裡直接 fetch_finmind_dataset 繞過白名單回補歷史。

只補這兩個 dataset(各 1 call/symbol),不重抓 price/chip/財報(那些 bulk OpenAPI 已新鮮)。
upsert_price_rows 用 COALESCE 只填新欄位、不動既有 price/chip。純資料回補,不改任何程式碼/模型。
回補完再做 recompute→retrain→backtest 決定要不要採用(先證後改)。
"""
import sys
import time
import ml_backend

b = ml_backend.backend
TOK = ml_backend.read_finmind_token()

uni = list(b.liquid_monster_universe(800))
default = [str(s) for s in ml_backend.DEFAULT_SYMBOLS]
watch = [str(s) for s in getattr(ml_backend, "MONSTER_WATCH_SYMBOLS", [])]
SYMS = list(dict.fromkeys([*default, *uni, *watch]))  # 訓練池優先 + 掃描宇宙 + 觀察清單
print(f"回補宇宙 {len(SYMS)} 檔(訓練{len(default)}+掃描{len(uni)} 去重)", flush=True)

done = 0
dt_hit = ln_hit = 0
failed = []
for i, sym in enumerate(SYMS):
    try:
        raw = b.load_price_rows(sym)
        if not raw or len(raw) < 60:
            continue
        rows = {r["date"]: dict(r) for r in raw}
        dates = sorted(rows)
        start, end = dates[0], dates[-1]
        # 直接抓(繞過白名單);fetch_finmind_dataset 內部會 reserve 額度、額度守門
        try:
            dt_rows = b.fetch_finmind_dataset("TaiwanStockDayTrading", sym, start, end, TOK)
        except Exception:
            dt_rows = []
        try:
            ln_rows = b.fetch_finmind_dataset("TaiwanStockSecuritiesLending", sym, start, end, TOK)
        except Exception:
            ln_rows = []
        if dt_rows:
            b.merge_day_trading(rows, dt_rows)
            dt_hit += 1
        if ln_rows:
            b.merge_securities_lending(rows, ln_rows)
            ln_hit += 1
        if dt_rows or ln_rows:
            b.upsert_price_rows(list(rows.values()))
            done += 1  # 只把「真的補到資料」的算成功;額度耗盡抓不到不該灌水成 done
    except Exception as exc:
        failed.append((sym, str(exc)[:80]))
    if (i + 1) % 50 == 0:
        u = b.read_finmind_usage()
        print(f"...{i+1}/{len(SYMS)}  完成{done} 當沖命中{dt_hit} 借券命中{ln_hit} "
              f"失敗{len(failed)}  FinMind={u.get('calls')}/{u.get('safeLimit')}", flush=True)
    time.sleep(0.05)

u = b.read_finmind_usage()
print(f"\n=== 回補完成 ===", flush=True)
print(f"處理 {done}/{len(SYMS)} 檔;當沖命中 {dt_hit}、借券命中 {ln_hit};失敗 {len(failed)}", flush=True)
print(f"FinMind 用量 {u.get('calls')}/{u.get('safeLimit')}", flush=True)
if failed[:10]:
    print("失敗前10:", failed[:10], flush=True)

# 驗證覆蓋率
import sqlite3
uni_set = set(uni)
with b.connect() as conn:
    conn.row_factory = sqlite3.Row
    px = conn.execute("SELECT MAX(date) d FROM prices").fetchone()["d"]
    # 用全市場最近 20 個交易日界線,不用最後一檔股票的日曆(停牌/新上市不具代表性,
    # 且宇宙全空時迴圈變數 dates 未定義會 NameError)。
    _cut = conn.execute("SELECT DISTINCT date FROM prices ORDER BY date DESC LIMIT 20").fetchall()
    cutoff = _cut[-1]["date"] if _cut else px
    for f in ("day_trade_ratio", "day_trade_buy_sell_imbalance", "securities_lending_volume"):
        rows = conn.execute(f"SELECT symbol,{f} FROM prices WHERE date>=?", (cutoff,)).fetchall()
        have = len(set(r["symbol"] for r in rows if r["symbol"] in uni_set and r[f] is not None))
        print(f"  近20日 universe 有 {f}: {have}/{len(uni_set)}", flush=True)
