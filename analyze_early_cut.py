"""
零成本停損統計實驗(先證後改)——回答加強roadmap的核心問題:
「盤中/日線跌到 -4/-5/-6% 且量縮時提早砍,是躲掉重摔、還是砍在反彈前的阱底?」

純讀取分析,不改任何策略/標籤/出場邏輯。用 backtest_top10 已建好的回測基建
(short_term_target 新增的 early_dip 診斷欄),重建 model_prob 的 OOS 前10名候選,
對「曾跌到 -X% 且量縮」的那組,比較:
  - 續抱到系統實際出場的平均 net(held)
  - vs 當下就砍在 -X%(鎖定固定小虧)的 net(cut)
held < cut ⇒ 提早砍省錢(躲掉重摔); held > cut ⇒ 提早砍是砍在反彈前(砍掉贏家)。

執行:python analyze_early_cut.py
"""
import backtest_top10 as bt

# 與 short_term_target 完全相同的成本常數,用來算「砍在 -X%」的固定 net
COMMISSION, TAX, ENTRY_SLIP = 0.001425, 0.003, 0.001


def cut_net(x):
    """在 gain=-x 當下賣出的 net(扣完進出成本),用來當提早砍的對照基準。"""
    return (1 - x) * (1 - COMMISSION - TAX) / (1 + ENTRY_SLIP + COMMISSION) - 1


def main():
    records = bt.build_records()
    by_date = {}
    for rec in records:
        by_date.setdefault(rec["date"], []).append(rec)
    dates = sorted(by_date)
    all_dates_expanded = sorted(rec["date"] for rec in records)
    oos_boundary = all_dates_expanded[int(len(all_dates_expanded) * 0.8)]

    # 重建 model_prob 的每日前 TOP_N 名候選(與 run() 完全一致)
    scorer = bt.RANKINGS["model_prob"]
    picks = []
    for date in dates:
        day_records = by_date[date]
        if len(day_records) < bt.MIN_CANDIDATES_PER_DAY:
            continue
        scored = [(scorer(rec), rec) for rec in day_records]
        scored = [p for p in scored if p[0] >= 0]
        scored.sort(key=lambda p: p[0], reverse=True)
        picks.extend(rec for _, rec in scored[:bt.TOP_N])
    oos = [t for t in picks if t["date"] >= oos_boundary]
    print(f"model_prob OOS picks: {len(oos)} 筆 (OOS 邊界 {oos_boundary})", flush=True)
    print()

    for thr in (4, 5, 6):
        key = f"earlyDip{thr}"
        group = [t for t in oos if t.get(key) is not None]
        if not group:
            print(f"-- 跌到 -{thr}% 且量縮:0 筆")
            continue
        held_nets = [t["net"] for t in group]
        held_avg = sum(held_nets) / len(held_nets)
        rebound = sum(1 for n in held_nets if n > 0) / len(group)   # 續抱後翻正比率
        to_stop = sum(1 for t in group if t.get("exitReason") == "stop_loss") / len(group)
        cn = cut_net(thr / 100.0)
        verdict = "提早砍省錢(躲掉重摔)" if held_avg < cn else "提早砍是砍在反彈前(砍掉贏家)"
        edge = (held_avg - cn) * 100
        print(f"-- 跌到 -{thr}% 且量縮:{len(group)} 筆 (佔 OOS picks {len(group)/len(oos)*100:.0f}%)")
        print(f"   續抱到實際出場   平均 net = {held_avg*100:+.2f}%")
        print(f"   當下砍在 -{thr}%    固定 net = {cn*100:+.2f}%")
        print(f"   續抱後翻正比率 = {rebound*100:.0f}%   最終走到 -7% 停損比率 = {to_stop*100:.0f}%")
        print(f"   >>> {verdict}(續抱比砍多 {edge:+.2f} 個百分點)")
        print()

    # === Step A：組合層 exit-only 模擬(picks 不變、不重訓)===
    # 對每個門檻 X：曾跌到 -X% 且量縮的單改成「當下就砍(net=cut_net(X)、y=0 因為
    # 提早砍就不可能是 +10% 正例)」，其餘維持續抱。比較 baseline vs early-cut 的
    # OOS avgNet/PF/hit10 + 成本敏感度。這隔離「只改出場」的效果,決定值不值得進 Step B 重訓。
    def _pf(nets):
        w = sum(n for n in nets if n > 0); l = -sum(n for n in nets if n <= 0)
        return round(w / l, 3) if l > 0 else None

    def _report(label, nets, ys):
        print(f"  {label}: avgNet={sum(nets)/len(nets)*100:+.3f}%  PF={_pf(nets)}  "
              f"hit10={sum(ys)/len(ys):.4f}  "
              f"|保守1.2%成本 avgNet={sum(n-0.0065 for n in nets)/len(nets)*100:+.3f}% PF={_pf([n-0.0065 for n in nets])}")

    print("=" * 70)
    print("Step A：組合層 exit-only 模擬(model_prob OOS picks，不重訓)")
    base_nets = [t["net"] for t in oos]
    base_ys = [t["y"] for t in oos]
    _report("baseline(現行,無early-cut)", base_nets, base_ys)
    for thr in (4, 5, 6):
        key = f"earlyDip{thr}"
        cn = cut_net(thr / 100.0)
        ec_nets = [cn if t.get(key) is not None else t["net"] for t in oos]
        ec_ys = [0 if t.get(key) is not None else t["y"] for t in oos]
        _report(f"early-cut -{thr}%", ec_nets, ec_ys)
    print("=" * 70)


if __name__ == "__main__":
    main()
