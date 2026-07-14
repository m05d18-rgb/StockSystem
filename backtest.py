#!/usr/bin/env python3
"""
backtest.py — 妖股短線策略歷史回測
基於 SQLite 日線資料，驗證妖股雷達「飆股型態」進場基準的真實勝率（無未來資料洩漏）

策略邏輯（進場對應 ml_backend.monster_score_for_symbol 的 surge_setup 妖股型態，
出場對應訓練 label short_term_target 的短線出場規則，讓回測、模型 label、
雷達判斷三者用同一套妖股基準）：
  買進條件（surge_setup + 流動性 + 不過熱）：
    接近/突破 20 日高點（收盤 >= 前 20 日最高 * 0.995）
    5 日漲幅 >= 7.5%、20 日漲幅 >= 10%
    量比 1.5~5.5 倍
    MA20 > MA60 且 MACD > 0（trend_ok，同雷達 surge_setup）
    ATR% 1.2~8.5（波動帶，同雷達 risk_ok）
    RSI <= 82、單日漲幅 <= 9%、5 日漲幅 <= 22%（不追過熱）
    強於大盤（個股 20 日報酬 > 加權指數 20 日報酬）
    流動性：20 日均量 >= 1000 張、成交金額 >= 3000 萬
    隔日開盤跳空 > +5% 不追價（對應雷達盤中「禁止追價」規則）
  出場條件（與模型訓練 label 相同）：
    停損：    買進後跌 -7%（立即出）
    規則一：  10 日內漲 +10% → 出場鎖利
    規則二：  10 日內漲 +5~10%，量弱 → 出場
    時間停損：持有 > 10 日，獲利 < 5% → 換下一檔
    最長持有：20 交易日強制出場

用法：
  python backtest.py                        # 全部股票，從 2023-06-01
  python backtest.py --symbols 2330 3481    # 指定股票
  python backtest.py --start 2024-01-01     # 指定起始日
  python backtest.py --out result.csv       # 輸出明細
"""

import argparse
import sqlite3
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")

DB_PATH = Path(__file__).parent / "stock_system.sqlite3"

# ─── 交易成本 ──────────────────────────────────────────────
COMMISSION  = 0.001425   # 手續費 0.1425%（買賣各一次）
TAX         = 0.003      # 交易稅 0.3%（賣出時）
ENTRY_SLIP  = 0.001      # 進場滑點 0.1%

# ─── 策略參數 ──────────────────────────────────────────────
STOP_LOSS_PCT      = -0.07   # -7% 停損
TAKE_PROFIT_10     =  0.10   # 短線規則一：+10% 出場
TIME_STOP_DAYS     = 10      # 時間窗口（交易日）
TIME_STOP_MIN_PCT  =  0.05   # 超過時間窗口後，獲利未達此值就出場
MAX_HOLD_DAYS      = 20      # 最長持有天數

# ─── 妖股進場參數（鏡射 ml_backend.monster_score_for_symbol 的 surge_setup）───
SURGE_RET5_MIN      = 7.5    # 5 日漲幅下限 %
SURGE_RET20_MIN     = 10.0   # 20 日漲幅下限 %
SURGE_VOL_RATIO_MIN = 1.5    # 量比下限
SURGE_VOL_RATIO_MAX = 5.5    # 量比上限（再高視為過熱）
SURGE_RSI_MAX       = 82.0   # RSI 上限
OVERHEAT_RET5_MAX   = 22.0   # 5 日漲幅過熱上限 %
OVERHEAT_CHANGE1_MAX = 9.0   # 單日漲幅過熱上限 %
ENTRY_GAP_MAX       = 0.05   # 隔日開盤跳空 > +5% 不追價
MIN_AVG_VOLUME_LOTS = 1000   # 20 日均量下限（張），同 ml_backend.MIN_MONSTER_AVG_VOLUME_LOTS
MIN_TURNOVER_MILLION = 30    # 成交金額下限（百萬），同 ml_backend.MIN_MONSTER_TURNOVER_MILLION
SURGE_ATR_PCT_MIN   = 1.2    # ATR% 波動帶下限，同 ml_backend risk_ok
SURGE_ATR_PCT_MAX   = 8.5    # ATR% 波動帶上限（雷達 buy_allowed 的無條件必要項）


# ──────────────────────────────────────────────────────────────
# 資料載入
# ──────────────────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True)


def load_prices(symbols=None, start=None):
    params, where = [], []
    if symbols:
        where.append(f"symbol IN ({','.join('?'*len(symbols))})")
        params.extend(symbols)
    if start:
        where.append("date >= ?")
        params.append(start)
    sql = f"""
        SELECT symbol, date, open, high, low, close, volume,
               foreign_buy_sell, trust_buy_sell, margin_balance
        FROM prices
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY symbol, date
    """
    conn = _conn()
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


def load_market(start=None):
    where = "WHERE market_key='TAIEX'" + (f" AND date >= '{start}'" if start else "")
    conn = _conn()
    df = pd.read_sql_query(
        f"SELECT date, close FROM market_prices {where} ORDER BY date", conn
    )
    conn.close()
    ser = df.set_index("date")["close"]
    ma20 = ser.rolling(20).mean()
    return ser, ma20


# ──────────────────────────────────────────────────────────────
# 技術指標計算（與前端 calcIndicators 一致）
# ──────────────────────────────────────────────────────────────

def calc_indicators(df):
    df = df.copy()
    c, v = df["close"], df["volume"]

    df["ma5"]  = c.rolling(5).mean()
    df["ma20"] = c.rolling(20).mean()
    df["ma60"] = c.rolling(60).mean()

    # RSI(14)
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # MACD (12,26,9)：macd 欄位要存柱狀圖(DIF-DEA)不是DIF本身，下面
    # buy_signal() 用 macd>0 當「trend_ok，同雷達 surge_setup」，跟
    # ml_backend.py build_features_for_rows() 的 macd = (dif-dea)*2、
    # trend_ok = macd>0(即DIF>DEA多頭排列)口徑一致；只算DIF-EMA26的話，
    # DIF在0軸附近盤整或剛從高檔反轉初期會判斷相反(誤把已經死叉的股票當
    # trend_ok，或漏掉剛黃金交叉但DIF仍<0的股票)。
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    df["macd"] = (dif - dea) * 2

    # ATR(14)
    prev = c.shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"]  - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # 量比：進場用的 vol_ratio 含當日(20根含今天)，鏡射 ml_backend 進場端
    # (quick_monster_filter/build_features_for_rows 的 volume_ratio 同樣
    # 含當日)。出場的量縮判斷則是另一套口徑——ml_backend.short_term_target
    # 的 vol_weak 明確用「前20日」均量(不含當日)，這裡另外算一欄
    # vol_ratio_exit 給出場邏輯用，不能共用 vol_ratio，否則兩套進出場邏輯
    # 各自對應到錯的口徑。
    df["vol20"]          = v.rolling(20).mean()
    df["vol_ratio"]      = v / df["vol20"].replace(0, np.nan)
    df["vol20_exit"]     = v.shift(1).rolling(20).mean()
    df["vol_ratio_exit"] = v / df["vol20_exit"].replace(0, np.nan)

    # 妖股動能欄位
    df["ret5"]    = (c / c.shift(5) - 1) * 100
    df["ret20"]   = (c / c.shift(20) - 1) * 100
    df["change1"] = (c / c.shift(1) - 1) * 100
    df["high20"]  = df["high"].shift(1).rolling(20).max()   # 前 20 日最高（不含當日）

    return df


# ──────────────────────────────────────────────────────────────
# 買進訊號（妖股飆股型態，鏡射 ml_backend 的 surge_setup + 流動性 + 不過熱）
# ──────────────────────────────────────────────────────────────

def buy_signal(row, mkt_ret20):
    required = ("ma20", "ma60", "macd", "atr", "vol_ratio", "ret5", "ret20", "change1", "high20", "rsi")
    if any(pd.isna(row[key]) for key in required):
        return False
    c = row["close"]
    if c <= 0:
        return False
    volume = row["volume"] or 0
    avg_lots = (row["vol20"] / 1000) if not pd.isna(row["vol20"]) else 0
    turnover_million = c * volume / 1_000_000
    stock_stronger = (row["ret20"] / 100) > (mkt_ret20 if not pd.isna(mkt_ret20) else 0)
    atr_pct = row["atr"] / c * 100
    overheated = (
        row["rsi"] > SURGE_RSI_MAX or
        row["vol_ratio"] > SURGE_VOL_RATIO_MAX or
        row["ret5"] > OVERHEAT_RET5_MAX or
        row["change1"] > OVERHEAT_CHANGE1_MAX
    )
    return (
        avg_lots >= MIN_AVG_VOLUME_LOTS                     and   # 流動性：均量
        turnover_million >= MIN_TURNOVER_MILLION            and   # 流動性：成交金額
        c >= row["high20"] * 0.995                          and   # 接近/突破 20 日高
        row["ret5"] >= SURGE_RET5_MIN                       and   # 5 日急漲
        row["ret20"] >= SURGE_RET20_MIN                     and   # 20 日趨勢啟動
        SURGE_VOL_RATIO_MIN <= row["vol_ratio"] <= SURGE_VOL_RATIO_MAX and  # 放量但不失控
        row["ma20"] > row["ma60"] and row["macd"] > 0       and   # trend_ok，同雷達 surge_setup
        SURGE_ATR_PCT_MIN <= atr_pct <= SURGE_ATR_PCT_MAX   and   # 波動帶，同雷達 risk_ok
        stock_stronger                                      and   # 強於大盤
        not overheated                                            # 不追過熱
    )


# ──────────────────────────────────────────────────────────────
# 單股逐日回測
# ──────────────────────────────────────────────────────────────

def simulate_trades(sdf, mkt_ser, mkt_ma20):
    trades = []
    rows = sdf.reset_index(drop=True)
    n = len(rows)
    in_pos = False
    entry_price = entry_date = entry_i = None
    # 大盤 20 日報酬（判斷個股是否強於大盤，對應 surge_setup 的 stock_stronger）
    mkt_ret20_ser = mkt_ser / mkt_ser.shift(20) - 1

    # 迴圈起點對齊 ml_backend.py build_features_for_rows()：正式系統要至少
    # 80 筆歷史資料才開始產生特徵(len(rows)<120整批拒絕是另一層更保守的
    # 全域門檻)，起點若比它早(原本是60)，會在正式系統邏輯上不可能出現
    # surge_setup 判斷的期間，混入回測樣本讓統計比真實策略寬鬆。
    for i in range(80, n - 1):
        row      = rows.iloc[i]
        next_row = rows.iloc[i + 1]
        date     = row["date"]

        mkt_ret20 = np.nan
        if date in mkt_ret20_ser.index:
            mkt_ret20 = mkt_ret20_ser[date]

        if in_pos:
            curr = row["close"]
            if curr <= 0:
                # 當天收盤價資料異常（例如來源缺漏被存成 0），跳過出場判斷，
                # 不能拿壞資料算報酬率，避免除以極小值/0 產生 inf。
                continue
            # hold_days 用絕對索引差(i - entry_i)算，不是遞增計數器：後者在
            # 上面 continue 跳過的那幾天不會累加，導致本次持倉的 hold_days
            # 永久比實際經過的交易日數少，且會持續累積誤差，讓
            # TIME_STOP_DAYS/MAX_HOLD_DAYS 這些以 hold_days 為準的出場條件
            # 被延後觸發。絕對索引差不受中途 continue 影響。
            # +1 讓進場當日收盤 hold_days=1(1-based),對齊 ml_backend.short_term_target 的
            # hold_days=j-index(j 從 index+1 起 → 1..20)。原本 0-based 使 time_stop/max_hold
            # 的邊界日比模型 label 整整晚一個交易日觸發,與「回測/label/雷達同一套出場」不符。
            hold_days = i - entry_i + 1
            gain_pct = (curr - entry_price) / entry_price
            # vol_weak 用 vol_ratio_exit(前20日均量，不含當日)，跟
            # ml_backend.short_term_target 的量縮判斷口徑一致(見 calc_indicators)。
            vol_weak = (row["vol_ratio_exit"] < 0.85) if not pd.isna(row["vol_ratio_exit"]) else True

            # ── 出場邏輯（順序很重要）──
            reason = None

            if gain_pct <= STOP_LOSS_PCT:
                reason = "stop_loss"

            elif hold_days <= TIME_STOP_DAYS and gain_pct >= TAKE_PROFIT_10:
                reason = "take_profit_10pct"

            elif hold_days <= TIME_STOP_DAYS and 0.05 <= gain_pct < TAKE_PROFIT_10 and vol_weak:
                reason = "take_profit_5pct_vol_weak"

            elif hold_days > TIME_STOP_DAYS and gain_pct < TIME_STOP_MIN_PCT:
                reason = "time_stop"

            elif hold_days >= MAX_HOLD_DAYS:
                reason = "max_hold"

            if reason:
                exit_price = curr
                # entry_price 進場時已乘 (1+ENTRY_SLIP)（見下方進場處），成本只需
                # 再算手續費；先前又多乘一次 ENTRY_SLIP＝把進場滑點重複計了兩次，
                # 使 net% 系統性偏低約 0.1%。改為單次滑點，對齊 short_term_target
                # 的成本口徑（entry_open×(1+ENTRY_SLIP+COMMISSION)＝滑點只算一次）。
                cost    = entry_price * (1 + COMMISSION)
                proceeds = exit_price * (1 - COMMISSION - TAX)
                net_pct = proceeds / cost - 1

                trades.append({
                    "symbol":       row["symbol"],
                    "entry_date":   entry_date,
                    "exit_date":    date,
                    "entry_price":  round(entry_price, 2),
                    "exit_price":   round(exit_price, 2),
                    "hold_days":    hold_days,
                    "gain_pct_raw": round(gain_pct * 100, 2),
                    "gain_pct_net": round(net_pct * 100, 2),
                    "exit_reason":  reason,
                    "win":          net_pct > 0,
                })
                in_pos = False

        else:
            if (
                buy_signal(row, mkt_ret20)
                and next_row["open"] > 0
                and (next_row["open"] / row["close"] - 1) <= ENTRY_GAP_MAX
            ):
                # 訊號在今日收盤後，明日開盤進場。隔日開盤價異常（<=0）不進場，
                # 避免用接近 0 的進場價把報酬率算成 inf；跳空超過 +5% 也不追價，
                # 對應妖股雷達盤中的「禁止追價」規則。
                entry_price = next_row["open"] * (1 + ENTRY_SLIP)
                entry_date  = next_row["date"]
                entry_i     = i + 1  # next_row 在 rows 裡的絕對索引，供 hold_days 計算用
                in_pos      = True

    return trades


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def run_backtest(symbols=None, start="2023-06-01", min_rows=200):
    print(f"載入日線資料（起始：{start}）...")
    raw = load_prices(symbols, start)
    mkt_ser, mkt_ma20 = load_market(start)

    sym_count = raw["symbol"].nunique()
    print(f"股票數：{sym_count}，總筆數：{len(raw):,}")

    all_trades = []
    syms = raw["symbol"].unique()

    for idx, sym in enumerate(syms, 1):
        sdf = raw[raw["symbol"] == sym].copy()
        if len(sdf) < min_rows:
            continue
        sdf = calc_indicators(sdf)
        trades = simulate_trades(sdf, mkt_ser, mkt_ma20)
        all_trades.extend(trades)

        if idx % 200 == 0:
            print(f"  已處理 {idx}/{sym_count} 支...")

    if not all_trades:
        print("回測期間無訊號產生。")
        return pd.DataFrame()

    df = pd.DataFrame(all_trades)
    print(f"回測完成，共 {len(df)} 筆交易")
    return df


# ──────────────────────────────────────────────────────────────
# 報告輸出
# ──────────────────────────────────────────────────────────────

def report(df):
    if df.empty:
        return

    # 最後一道防線：不管上游資料有多乾淨，都不該讓單一筆非有限值
    # （inf/nan，通常來自資料異常）拖垮整份報告的統計指標。
    finite_mask = np.isfinite(df["gain_pct_net"])
    dropped = (~finite_mask).sum()
    if dropped:
        print(f"警告：{dropped} 筆交易報酬率非有限值（inf/nan），已從統計中排除。")
        df = df[finite_mask].reset_index(drop=True)
    if df.empty:
        print("排除異常交易後已無可用資料。")
        return

    total = len(df)
    wins  = df["win"].sum()
    wr    = wins / total
    rets  = df["gain_pct_net"].values / 100   # 轉成小數
    avg_hold = df["hold_days"].mean()

    win_rets  = df[df["win"]]["gain_pct_net"].mean()
    loss_rets = df[~df["win"]]["gain_pct_net"].mean()
    gross_win  = df[df["win"]]["gain_pct_net"].sum()
    gross_loss = abs(df[~df["win"]]["gain_pct_net"].sum()) or 1
    pf = gross_win / gross_loss

    # Sharpe（以每筆報酬率估算年化）
    sharpe = (rets.mean() / (rets.std() or 1)) * np.sqrt(252 / max(avg_hold, 1))

    # 最大回撤：all_trades 是按股票代碼逐檔迴圈組裝進來的(load_prices 用
    # ORDER BY symbol, date)，不是照實際發生時間排序，直接對 df 原始順序
    # 連乘等於把不同股票、時間上完全交錯的報酬率隨機接在一起算路徑依賴的
    # 回撤，算出來的數字沒有對應到任何真實可能發生的資金軌跡。先依
    # exit_date 排序再連乘，才是「如果照時間序列持有這些部位」的正確算法
    # (仍隱含每次只單一部位、上一筆出場才進下一筆的簡化假設，不是真正
    # 多檔同時持倉的權益曲線，只能當粗略參考)。
    df_time_ordered = df.sort_values("exit_date").reset_index(drop=True)
    ordered_rets = df_time_ordered["gain_pct_net"].values / 100
    cumval = pd.Series((1 + ordered_rets).cumprod())
    dd = (cumval - cumval.cummax()) / cumval.cummax()
    max_dd = dd.min() * 100

    sep = "=" * 56
    print(f"\n{sep}")
    print("  歷史回測報告（妖股飆股型態進場策略，含手續費與交易稅）")
    print(sep)
    print(f"  總交易次數    ：{total:>8,} 筆")
    print(f"  勝率          ：{wr:>8.1%}")
    print(f"  平均報酬（淨）：{df['gain_pct_net'].mean():>+7.2f}%")
    # 這批交易全勝或全敗時，虧損/獲利那一側是空 Series，.mean() 回傳 nan，
    # 沒有防護會印出語意不通的 "+nan%"。
    win_rets_text = "   N/A" if pd.isna(win_rets) else f"{win_rets:>+7.2f}%"
    loss_rets_text = "   N/A" if pd.isna(loss_rets) else f"{loss_rets:>+7.2f}%"
    print(f"  平均獲利      ：{win_rets_text}")
    print(f"  平均虧損      ：{loss_rets_text}")
    print(f"  獲利因子      ：{pf:>8.2f}  （> 1.5 為佳）")
    print(f"  平均持有天數  ：{avg_hold:>8.1f} 天")
    print(f"  Sharpe Ratio  ：{sharpe:>8.2f}  （> 1.0 為佳）")
    print(f"  最大回撤      ：{max_dd:>+7.2f}%")
    print("-" * 56)

    print("\n  出場原因分布：")
    grp = df.groupby("exit_reason").agg(
        count    = ("win", "count"),
        win_rate = ("win", "mean"),
        avg_ret  = ("gain_pct_net", "mean"),
    ).sort_values("count", ascending=False)
    for reason, r in grp.iterrows():
        print(f"    {reason:<30} {r['count']:>5} 次  "
              f"勝率 {r['win_rate']:.0%}  均報酬 {r['avg_ret']:>+.2f}%")

    print("\n  各年度績效：")
    df["year"] = pd.to_datetime(df["exit_date"]).dt.year
    by_year = df.groupby("year").agg(
        trades   = ("win", "count"),
        win_rate = ("win", "mean"),
        avg_ret  = ("gain_pct_net", "mean"),
        total_ret= ("gain_pct_net", "sum"),
    )
    for yr, r in by_year.iterrows():
        print(f"    {yr}  {r['trades']:>4} 筆  勝率 {r['win_rate']:.0%}  "
              f"均報酬 {r['avg_ret']:>+.2f}%  累計 {r['total_ret']:>+.1f}%")

    print("\n  績效最佳前 10 支股票（≥ 3 筆交易）：")
    top = (
        df.groupby("symbol")
        .agg(trades=("win","count"), win_rate=("win","mean"), total_ret=("gain_pct_net","sum"))
        .query("trades >= 3")
        .sort_values("total_ret", ascending=False)
        .head(10)
    )
    for sym, r in top.iterrows():
        print(f"    {sym}  {r['trades']:>3} 筆  勝率 {r['win_rate']:.0%}  累計 {r['total_ret']:>+.1f}%")

    print(sep)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="股票系統歷史回測")
    parser.add_argument("--symbols", nargs="*", help="指定股票代號（不填 = 全部）")
    parser.add_argument("--start",    default="2023-06-01", help="回測起始日")
    parser.add_argument("--min-rows", type=int, default=200,  help="最少需要幾天歷史（預設 200）")
    parser.add_argument("--out",      default=None,           help="輸出明細 CSV 路徑")
    args = parser.parse_args()

    result = run_backtest(
        symbols  = args.symbols,
        start    = args.start,
        min_rows = args.min_rows,
    )
    report(result)

    if args.out and not result.empty:
        result.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\n明細已輸出至：{args.out}")
