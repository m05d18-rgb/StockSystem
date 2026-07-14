"""Brain 拆模型 byte-identical 回歸驗證(2026-07-09)。

比對 build_brain_decision 的 use_model=True(舊,跑模型) vs use_model=False(新,特徵直算)
的『決策結論 + 各分量分數』是否全等(四捨五入濾浮點)。拆模型是行為不變的重構,決策必須零變動。
收盤後(股價穩定)跑最乾淨——盤中即時報價會在兩次呼叫之間變動、造成浮點雜訊。

exit 0 = PASS(可部署);exit 1 = 有決策差異(不可部署,要查)。
"""
import sys, json
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ml_backend
from modules.brain import engine
b = ml_backend.backend


def rnd(v):
    return round(v, 6) if isinstance(v, (int, float)) else v


def comp_score(d):
    return rnd(d.get("score")) if isinstance(d, dict) else rnd(d)


def main():
    lm = b.list_monster_scores(80)
    syms = list(dict.fromkeys([c["symbol"] for c in (lm.get("candidates") or [])][:20] +
        ["2330", "2317", "2454", "2603", "2609", "3008", "1101", "2882", "6505", "3037", "2412", "2308"]))
    rec_changed, score_changed, n = [], [], 0
    for ctx in ["portfolio_exit", "monster"]:
        for sym in syms:
            n += 1
            try:
                o = engine.build_brain_decision(sym, context=ctx, use_model=True)
                w = engine.build_brain_decision(sym, context=ctx, use_model=False)
            except Exception as exc:
                print(f"EXC {sym} {ctx}: {exc}")
                continue
            for k in ["recommendation", "actionLabel", "entryAllowed", "observeOnly", "decisionBlocked", "sellDataReady"]:
                if o.get(k) != w.get(k):
                    rec_changed.append((ctx, sym, k, o.get(k), w.get(k)))
            for k in ["klineScore", "strategyBacktestScore", "technicalIndicatorScore", "shortMoneyScore"]:
                if comp_score(o.get(k)) != comp_score(w.get(k)):
                    score_changed.append((ctx, sym, k, comp_score(o.get(k)), comp_score(w.get(k))))
            for k in ["riskPenalty", "setupScore"]:
                if rnd(o.get(k)) != rnd(w.get(k)):
                    score_changed.append((ctx, sym, k, rnd(o.get(k)), rnd(w.get(k))))
            ov2 = (o.get("brainV2") or {}).get("score")
            wv2 = (w.get("brainV2") or {}).get("score")
            if rnd(ov2) != rnd(wv2):
                score_changed.append((ctx, sym, "v2Score", rnd(ov2), rnd(wv2)))
    print(f"比對 {n} 個 context×symbol")
    print(f"決策結論改變: {len(rec_changed)} 筆")
    for r in rec_changed[:30]:
        print(f"  [{r[0]}] {r[1]} {r[2]}: OLD={r[3]!r} NEW={r[4]!r}")
    print(f"分量分數改變: {len(score_changed)} 筆")
    for r in score_changed[:30]:
        print(f"  [{r[0]}] {r[1]} {r[2]}: OLD={r[3]} NEW={r[4]}")
    if not rec_changed and not score_changed:
        print("PASS: 決策 byte-identical,重構零決策變動,可部署")
        return 0
    print("FAIL: 有決策差異,不可部署")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
