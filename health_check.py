import argparse
import importlib
import json
import sys
import time

from ml_backend import (
    MODEL_PACKAGE_IMPORTS,
    REQUIRED_MODEL_PACKAGES,
    backend,
    compare_model_environment,
    current_model_environment,
    now_text,
)


def package_checks():
    runtime = current_model_environment()
    checks = {}
    for package_name, expected_version in REQUIRED_MODEL_PACKAGES.items():
        import_name = MODEL_PACKAGE_IMPORTS.get(package_name, package_name)
        installed_version = runtime["packages"].get(package_name) or ""
        item = {
            "package": package_name,
            "importName": import_name,
            "expected": expected_version,
            "installed": installed_version,
            "importOk": False,
            "versionOk": installed_version == expected_version,
            "ok": False,
            "error": "",
        }
        try:
            importlib.import_module(import_name)
            item["importOk"] = True
        except Exception as exc:
            item["error"] = str(exc)
        item["ok"] = item["importOk"] and item["versionOk"]
        checks[package_name] = item
    return checks


# 預測管線健康檢查的跨產業備援股票——分散在半導體/電子製造/金融，跟主要
# 測試股票(預設 2330)剛好不同產業，降低「同一個抓取/資料源剛好同時出包」
# 巧合命中全部候選的機率。
HEALTH_CHECK_FALLBACK_SYMBOLS = ["2317", "2882", "2454"]


def _run_prediction_health_check(primary_symbol):
    # 舊版只測單一寫死的股票(2330)，那檔股票自己的資料一次性缺口(不是
    # 模型/程式碼壞掉)就會讓 predictionTest 判定失敗，連帶讓 ok=False、
    # decisionsEnabled=False 波及「全部股票」的 Brain Engine 判斷——我們要
    # 偵測的是預測管線本身(模型/程式碼)是不是真的壞了，不是單一股票的
    # 資料品質。改成：主要股票失敗時換幾檔跨產業股票再試，只要有一檔測
    # 得出結果就代表管線是通的，不是系統性故障；全部失敗才是真的有問題。
    candidates = [primary_symbol] + [s for s in HEALTH_CHECK_FALLBACK_SYMBOLS if s != primary_symbol]
    attempts = []
    for candidate in candidates:
        try:
            result = backend.predict_symbol(candidate, save=False, repair=False)
            attempts.append({"symbol": candidate, "ok": bool(result), "error": ""})
            if result:
                return result, attempts, None
        except Exception as exc:
            attempts.append({"symbol": candidate, "ok": False, "error": str(exc)})
    tried = "、".join(a["symbol"] for a in attempts)
    detail = "；".join(f"{a['symbol']}: {a['error'] or '無結果'}" for a in attempts)
    return None, attempts, f"預測管線對 {tried} 全部失敗，疑似模型/程式碼本身故障（{detail}）"


def run_system_health(symbol="2330", include_prediction=True):
    started = time.time()
    packages = package_checks()
    model_env = backend.read_model_env()
    env_check = compare_model_environment(model_env)
    model = None
    model_public = None
    prediction = None
    prediction_attempts = []
    errors = []

    if not all(item["ok"] for item in packages.values()):
        for item in packages.values():
            if not item["ok"]:
                if not item["importOk"]:
                    errors.append(f"{item['package']} import failed: {item['error'] or 'missing'}")
                elif not item["versionOk"]:
                    errors.append(f"{item['package']} version {item['installed'] or 'missing'} != {item['expected']}")

    if not env_check["ok"]:
        errors.extend(env_check["issues"])

    model_load_error = ""
    if not backend.model_path.exists():
        errors.append("model.pkl not found")
    else:
        try:
            model, model_load_error = backend.load_model_with_error()
            if not model:
                errors.append(model_load_error or "model.pkl load failed")
            else:
                model_public = backend.public_model(model)
        except Exception as exc:
            errors.append(f"model load failed: {exc}")

    if include_prediction and model:
        # repair=False：健康檢查只驗證現有 DB 資料是否足夠，不觸發網路補抓
        # （補抓由每日排程負責），避免 health check 因無限等待 FinMind/Yahoo 而卡住
        prediction, prediction_attempts, prediction_error = _run_prediction_health_check(symbol)
        if prediction_error:
            errors.append(prediction_error)

    ok = not errors
    return {
        "ok": ok,
        "mode": "normal" if ok else "observe_only",
        "reason": "" if ok else "正式模型不可用，只觀察，不通知買賣",
        "errors": errors,
        "checkedAt": now_text(),
        "elapsedMs": int((time.time() - started) * 1000),
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "majorMinor": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "packages": packages,
        "model": {
            "path": str(backend.model_path),
            "exists": backend.model_path.exists(),
            "loadOk": bool(model),
            "loadError": model_load_error,
            "version": model_public.get("version") if model_public else "",
            "trainedAt": model_public.get("trainedAt") if model_public else "",
            "type": model_public.get("type") if model_public else "",
            "samples": model_public.get("samples") if model_public else 0,
        },
        "modelEnv": {
            "path": str(backend.model_env_path),
            "exists": backend.model_env_path.exists(),
            "content": model_env,
            "check": env_check,
        },
        "predictionTest": {
            "symbol": symbol,
            "ok": bool(prediction) if include_prediction else None,
            "skipped": not include_prediction,
            "prediction": prediction,
            "attempts": prediction_attempts,
        },
        "decisionsEnabled": ok,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="2330")
    parser.add_argument("--no-predict", action="store_true")
    args = parser.parse_args()
    health = run_system_health(symbol=args.symbol, include_prediction=not args.no_predict)
    print(json.dumps(health, ensure_ascii=False, indent=2))
    raise SystemExit(0 if health["ok"] else 1)


if __name__ == "__main__":
    main()
