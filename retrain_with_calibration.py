"""一次性重訓腳本：讓新加的分類模型校準欄位(calibration)進到 model.pkl。

訓練池直接沿用今天每日更新存在 model_meta 的 last_daily_training_symbols
(714檔，全產業基礎∪持股∪妖股候選)，保證跟正式排程用同一個池——之前發生
過手動驗證時誤用 137 檔窄池重訓的事故，這裡不重蹈。

執行：python retrain_with_calibration.py
"""
import sys
import time

if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ml_backend import backend


def main():
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = 'last_daily_training_symbols'"
        ).fetchone()
    if not row or not row[0]:
        raise RuntimeError("meta 裡沒有 last_daily_training_symbols，不知道正式訓練池是哪一份，中止")
    symbols = [s.strip() for s in row[0].split(",") if s.strip()]
    if len(symbols) < 400:
        raise RuntimeError(f"訓練池只有 {len(symbols)} 檔，遠低於正式規模(700+)，可能讀錯 meta，中止")
    print(f"訓練池 {len(symbols)} 檔(沿用今天每日更新的正式清單)", flush=True)
    started = time.time()
    result = backend.train_model(symbols)
    elapsed = time.time() - started
    metrics = (result or {}).get("metrics") or {}
    print(f"重訓完成，耗時 {elapsed/60:.1f} 分鐘", flush=True)
    print(f"metrics: accuracy={metrics.get('accuracy')} precision={metrics.get('precision')} auc={metrics.get('auc')}", flush=True)
    model = backend.load_model()
    extra = (model or {}).get("extra_models") or {}
    for key in ("xgboost", "lightgbm", "gradient_boosting"):
        entry = extra.get(key) or {}
        calibration = entry.get("calibration") or []
        print(f"{key}: calibration樣本數={len(calibration)}", flush=True)


if __name__ == "__main__":
    main()
