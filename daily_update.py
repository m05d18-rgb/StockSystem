from pathlib import Path
import datetime as dt
import json
import os
import sqlite3
import sys
import traceback

import data_integrity_check
from line_notify import send_line_message
from ml_backend import DEFAULT_SYMBOLS, RETIRED_SYMBOLS, backend, now_text
from modules.brain import build_brain_decision
from sinopac_backend import sinopac_backend


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "daily_update_logs"
INTEGRITY_LATEST_PATH = LOG_DIR / "integrity_latest.json"
DAILY_UPDATE_LOG_RETENTION = 60
# 資料庫備份目的地：這個專案沒有git，正式資料庫(600MB+的價格歷史/預測
# 紀錄/Brain快照)又放在OneDrive同步資料夾裡——OneDrive同步衝突弄壞SQLite
# 檔案是真實風險，一壞就全沒了。備份刻意放在OneDrive資料夾**外面**的
# 本機路徑：(1)避免每天600MB的備份檔吃掉OneDrive雲端額度，(2)避免備份檔
# 本身也被同一個OneDrive同步機制波及。保留最近3份輪替(~1.8GB)。
DB_BACKUP_DIR = Path.home() / "StockAI_Backups"
DB_BACKUP_KEEP = 3


def normalize_code(value):
    # 跟 server.py 的 normalize_symbol() 用同一套規則：只萃取數字字元後再
    # 截斷成4碼。這裡原本沒有[:4]截斷，跟server.py對「合法股票代碼」的
    # 定義不一致——如果未來DEFAULT_SYMBOLS或liquid_monster_universe()意外
    # 混進非4碼字串(權證/債券代碼、格式異常)，這裡會把畸形代碼原樣塞進
    # 訓練池清單，下游對應不到股票時默默失敗，不會有明確錯誤訊息。
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:4]


def unique_codes(codes, *, filter_inactive=True):
    output = []
    seen = set()
    for code in codes:
        code = normalize_code(code)
        if code and code not in RETIRED_SYMBOLS and code not in seen:
            output.append(code)
            seen.add(code)
    if filter_inactive:
        try:
            return backend.filter_active_symbols(output)
        except Exception:
            pass
    return output


def build_daily_training_symbols(portfolio_symbols):
    sources = {
        "sector_diversified": unique_codes(DEFAULT_SYMBOLS),
        "holdings": unique_codes(portfolio_symbols),
        "monster_liquid": [],
    }
    errors = []
    try:
        # 系統現在以妖股短線為主，妖股候選提高到 800（liquid_monster_universe
        # 目前實際約 600~700 檔可用），讓候選成為訓練池的主體。
        monster_limit = int(os.environ.get("DAILY_TRAIN_MONSTER_LIMIT", "800") or 800)
        sources["monster_liquid"] = unique_codes(backend.liquid_monster_universe(monster_limit))
    except Exception as exc:
        errors.append(f"monster_liquid: {exc}")
    # 訓練池 = 全產業多樣化基礎（DEFAULT_SYMBOLS）∪ 目前持股 ∪ 動態熱門候選，
    # 三者取聯集而非互相取代。DEFAULT_SYMBOLS 是手動維護、涵蓋全部產業分類
    # 的固定基礎樣本，若跟動態候選互相取代，每日重訓時容易被洗掉，讓異常
    # 偵測等模型又縮回只認得少數熱門股的樣子——即使現在以妖股候選為主，
    # 這批基礎樣本仍要保留，只是候選比重大幅提高。
    symbols = unique_codes(
        sources["sector_diversified"] + sources["holdings"] + sources["monster_liquid"]
    )
    return {
        "symbols": symbols,
        "sources": sources,
        "errors": errors,
    }


def save_daily_brain_v2_snapshots(symbols=None):
    """對全產業基礎樣本(DEFAULT_SYMBOLS)存一份 Brain v2 分量快照，累積到足夠
    天數後，才有真實資料可以回頭驗證目前的權重配置是否合理，而不是永遠只能
    憑經驗設數字。用固定的 "monster" 情境(系統目前唯一的策略腦袋)，確保跨
    股票、跨日期比較時，權重設定是同一套。"""
    symbols = symbols or DEFAULT_SYMBOLS
    saved = 0
    errors = []
    for symbol in unique_codes(symbols):
        try:
            decision = build_brain_decision(symbol, context="monster")
            if backend.save_brain_v2_snapshot(decision):
                saved += 1
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
    return {"saved": saved, "total": len(symbols), "errors": errors[:10]}


def backup_database(source_path=None, backup_dir=None, keep=DB_BACKUP_KEEP):
    """把正式SQLite資料庫備份到OneDrive外的本機資料夾，每天一份、保留最近
    keep份輪替。用sqlite3官方backup API(不是直接複製檔案)：backup API會
    以page為單位一致性複製，就算正式伺服器同時在WAL模式下讀寫也能得到
    一份完整一致的快照，直接copy檔案則可能複製到寫到一半的狀態。
    同一天重複呼叫直接跳過(冪等)，備份失敗不往外拋(呼叫端的每日更新
    不能因為備份失敗而中斷)，結果記錄在回傳值供log/告警使用。"""
    source_path = Path(source_path) if source_path else Path(backend.db_path)
    backup_dir = Path(backup_dir) if backup_dir else DB_BACKUP_DIR
    today_tag = dt.date.today().strftime("%Y%m%d")
    target_path = backup_dir / f"stock_system_{today_tag}.sqlite3"
    if target_path.exists():
        return {"ok": True, "skipped": True, "path": str(target_path), "note": "今天已備份過"}
    if not source_path.exists():
        # sqlite3.connect()對不存在的路徑會直接建立空資料庫，不會報錯——
        # 不先擋掉的話，來源檔路徑錯誤時會「成功」產出一個空備份，比
        # 沒備份更危險(未來還原時才發現是空的)。
        return {"ok": False, "error": f"來源資料庫不存在：{source_path}", "path": str(target_path)}
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        # 先寫到.tmp再rename：備份過程被中斷(關機/當機)不會留下一個看起來
        # 完整、實際上只寫到一半的備份檔誤導未來的還原操作。
        temp_path = backup_dir / f"stock_system_{today_tag}.sqlite3.tmp"
        source_conn = sqlite3.connect(source_path, timeout=60)
        try:
            dest_conn = sqlite3.connect(temp_path)
            try:
                source_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source_conn.close()
        os.replace(temp_path, target_path)
        removed = []
        backups = sorted(backup_dir.glob("stock_system_*.sqlite3"), reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
                removed.append(old.name)
            except OSError:
                pass
        return {
            "ok": True,
            "skipped": False,
            "path": str(target_path),
            "sizeBytes": target_path.stat().st_size,
            "removedOld": removed,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(target_path)}


def data_integrity_status_payload(result):
    """建立可覆寫的「目前狀態」，與不可覆寫的每日執行紀錄分開。"""
    problems = [item for item in (result.get("problems") or []) if isinstance(item, dict)]
    model_ineligible = [
        item for item in (result.get("modelIneligible") or []) if isinstance(item, dict)
    ]
    total = max(0, int(result.get("total") or 0))
    return {
        "schemaVersion": 1,
        "checkedAt": str(result.get("checkedAt") or now_text()),
        "expectedLatestDate": result.get("expectedLatestDate"),
        "ok": bool(result.get("ok")),
        "dataPipelineOk": not problems,
        "total": total,
        "healthyCount": max(0, total - len(problems)),
        "problemCount": len(problems),
        "problems": problems,
        "modelIneligibleCount": len(model_ineligible),
        "modelIneligible": model_ineligible,
        "modelFreshness": result.get("modelFreshness") or {},
        "modelGate": result.get("modelGate") or {},
        "predictionSettlement": result.get("predictionSettlement") or {},
    }


def persist_data_integrity_repair_queue(result):
    """保存真正缺口的修復佇列，並另存一份不會被舊每日紀錄混淆的現況。"""
    status = data_integrity_status_payload(result)
    problem_symbols = unique_codes(
        item.get("symbol") for item in status["problems"]
    )
    try:
        with backend.connect() as conn:
            backend.set_meta(
                conn,
                "last_data_integrity_problem_symbols_json",
                json.dumps(problem_symbols, ensure_ascii=False, separators=(",", ":")),
            )
            backend.set_meta(conn, "last_data_integrity_problem_count", str(len(problem_symbols)))
            backend.set_meta(conn, "last_data_integrity_checked_at", status["checkedAt"])
            backend.set_meta(
                conn,
                "last_data_integrity_model_ineligible_count",
                str(status["modelIneligibleCount"]),
            )
            backend.set_meta(
                conn,
                "last_data_integrity_result_json",
                json.dumps(status, ensure_ascii=False, separators=(",", ":"), default=str),
            )
        LOG_DIR.mkdir(exist_ok=True)
        _atomic_write_text(
            INTEGRITY_LATEST_PATH,
            json.dumps(status, ensure_ascii=False, indent=2, default=str),
        )
        return {
            "ok": True,
            "symbols": problem_symbols,
            "count": len(problem_symbols),
            "checkedAt": status["checkedAt"],
            "snapshotPath": str(INTEGRITY_LATEST_PATH),
        }
    except Exception as exc:
        return {"ok": False, "symbols": problem_symbols, "count": len(problem_symbols), "error": str(exc)}


def load_latest_data_integrity_status():
    """讀取最新完整性現況；資料庫優先，檔案只作為故障備援。"""
    errors = []
    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?",
                ("last_data_integrity_result_json",),
            ).fetchone()
        if row and row[0]:
            payload = json.loads(row[0])
            if isinstance(payload, dict):
                return {**payload, "available": True, "statusSource": "model_meta"}
    except (json.JSONDecodeError, TypeError, sqlite3.Error, OSError) as exc:
        errors.append(f"model_meta: {exc}")

    try:
        payload = json.loads(INTEGRITY_LATEST_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {**payload, "available": True, "statusSource": "integrity_latest.json"}
    except (json.JSONDecodeError, TypeError, OSError) as exc:
        errors.append(f"snapshot: {exc}")

    return {
        "ok": False,
        "available": False,
        "dataPipelineOk": False,
        "problemCount": None,
        "problems": [],
        "modelIneligibleCount": None,
        "modelIneligible": [],
        "error": " | ".join(errors) or "尚未執行完整性檢查",
    }


def run_daily_data_integrity_check(symbols, stale_days=2):
    """每天排程跑一次資料完整性檢查(data_integrity_check.py 的邏輯)，有問題
    就用 LINE 通知，讓資料缺口在排程階段就被發現，不用等使用者自己在畫面上
    看到「無資料」才回頭一個個查。LINE 沒設定或推播失敗都不能讓整個每日更新
    掛掉，所以檢查本身的錯誤、通知的錯誤都各自吞掉，記錄在回傳結果裡。"""
    try:
        result = data_integrity_check.run(symbols=symbols, stale_days=stale_days)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "notified": False}

    repair_queue = persist_data_integrity_repair_queue(result)
    result = {
        **result,
        "repairQueue": repair_queue,
        "repairQueuedCount": repair_queue.get("count", 0),
    }

    if result["ok"]:
        return {**result, "notified": False}

    # 2026-07-08 依需求關閉「個股資料品質」LINE 通知(使用者要 LINE 保持安靜,只留
    # 賣出訊號與真正的系統故障)。個股資料品質問題(chip/margin 覆蓋不足、少數非持股
    # 斷更)屬維護資訊,仍完整記在回傳結果與「資料新鮮度」面板/每日 log 供檢視;只有
    # 系統級故障(模型沒重訓/重訓品質閘門/結算異常)才發 LINE——這些是「系統壞了、
    # 不會再產生新預測」等級,漏看代價高,保留告警。
    system_alerts = []
    model_freshness = result.get("modelFreshness") or {}
    if not model_freshness.get("ok"):
        system_alerts.append(f"模型新鮮度：{model_freshness.get('issue')}")
    for extra_key in ("modelGate", "predictionSettlement"):
        extra_issue = (result.get(extra_key) or {}).get("issue")
        if extra_issue:
            system_alerts.append(extra_issue)

    if not system_alerts:
        # 只有個股資料品質問題、沒有系統級故障 → 不發 LINE(記在結果/面板即可)
        return {**result, "notified": False, "notifySkipped": "data_quality_only"}

    lines = [
        f"⚠️ 系統告警（另有 {result['problemCount']}/{result['total']} 檔資料品質待補，詳見資料新鮮度面板）"
    ]
    lines.extend(system_alerts)

    try:
        # 系統級故障=真警示，額度保留池內仍要送
        notify_result = send_line_message("\n".join(lines), priority="critical")
    except Exception as exc:
        notify_result = {"sent": False, "error": str(exc)}
    return {**result, "notified": bool(notify_result.get("sent")), "notifyResult": notify_result}


def backfill_candidate_advanced_flow(payload, force_refresh=False):
    """候選股主力分點(進階資金流)自動補齊。持股已在 full_daily_update 內用
    include_extended 抓過,這裡補「妖股候選」那批,讓面板的候選分點不用再手動按
    「補齊候選股資金流」。純資料補齊(非動特徵/模型);量小(預設40檔×當日,遠低於
    5000/hr safe cap)。註:advanced_flow/realtime_money_flow 目前是死特徵,這步只
    餵 UI 面板,不改 model_prob。抽成獨立函式讓 run() 的測試能一次 mock 掉(比照
    save_daily_brain_v2_snapshots),避免測試真的打 FinMind。即使主更新失敗也要獨立
    嘗試這條補抓路徑；若資料庫或來源仍不可用就明確回報失敗，不能用 skipped=true
    把缺口偽裝成已處理。"""
    try:
        cand_limit = int(os.environ.get("DAILY_ADVANCED_FLOW_CANDIDATES", "40") or 40)
        holding_set = set(unique_codes(payload.get("symbols") or []))
        scored = backend.list_monster_scores(cand_limit)
        cand_symbols = [
            s for s in unique_codes(item.get("symbol") for item in scored.get("candidates", []))
            if s not in holding_set
        ]
        if not cand_symbols:
            return {"ok": True, "symbols": 0, "note": "無候選或候選皆為持股，略過"}
        rows = backend.update_prices(
            cand_symbols,
            refresh_info=False,
            include_extended=True,
            force_refresh=force_refresh,
        )
        return {"ok": True, "symbols": len(cand_symbols), "updatedRows": rows}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def backfill_pending_prediction_prices(payload, force_refresh=False):
    """讓命中率戰績「又快又真」累積。每日更新只補持股+當日候選的價格,但預測是對液態
    宇宙前 600 檔存的——掉出名單的已預測股價格會斷更,結算需要的「預測後 10 個交易日股價」
    永遠補不齊→hit 永久 NULL 殭屍(實測 7030 筆預測僅 1 筆結算的主因)。這裡把「還有未結算、
    且未逾期(price_date 在 25 天內,排除結構性下市老殭屍)」的預測 symbol 也補近期日K
    (結算只需收盤,include_extended=False 壓 FinMind 額度),再跑一次 update_outcomes 讓
    補進的價格當場結算可成熟的那批。純補資料+結算,完全不碰 compute_prediction_outcomes 的
    窗內達標定義/target_return/校準門檻(那些已對齊訓練標籤,不可動)。即使主更新失敗仍獨立
    嘗試補價；失敗要明確記錄，不能回 ok=true/skipped。抽獨立函式讓 run() 測試好 mock。上限 SETTLEMENT_BACKFILL_MAX_
    SYMBOLS 防連假後爆量(按 price_date 新到舊截斷,優先保住最可能快成熟的)。"""
    try:
        # 預設上限 150(原為 900,2026-07-07 使用者回報「太久了」——900 檔序列補價把每日
        # 更新拖成龜速、盤中 force 跑還會撞下單)。改成每天補「最舊、最可能已成熟」的 150 檔,
        # 每日更新維持輕快;~800 檔積壓在幾天內由舊到新輪流補完+結算(舊的先補=最可能湊滿
        # 10 日、當場結算,不會卡在補了也還沒成熟的新預測上)。
        cap = int(os.environ.get("SETTLEMENT_BACKFILL_MAX_SYMBOLS", "150") or 150)
        # price_date 25 天內:排除結構性下市/長期停更的老殭屍(補了也沒新價、白費額度)。
        # 這些老殭屍由 compute_prediction_outcomes 的 structurallyUnsettleable 另外吸收、不進分母。
        cutoff = (dt.date.today() - dt.timedelta(days=25)).isoformat()
        # 這段必須維持單一資料庫連線；失效期間直接併入下方 SQL，避免每次
        # normalize 都額外開一次連線，也讓「查待結算預測 + 排除失效股票」
        # 使用同一個資料庫快照。
        holding_set = set(unique_codes(payload.get("symbols") or [], filter_inactive=False))
        with backend.connect() as conn:
            rows = conn.execute(
                "SELECT p.symbol, MIN(p.price_date) AS earliest FROM predictions p "
                "WHERE p.hit IS NULL AND p.price_date > ? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM market_symbol_inactive_periods i "
                "WHERE i.symbol = p.symbol AND date('now', 'localtime') >= i.inactive_from "
                "AND (i.inactive_to IS NULL OR i.inactive_to = '' "
                "OR date('now', 'localtime') <= i.inactive_to)) "
                "GROUP BY p.symbol ORDER BY earliest ASC",   # 最舊(最可能已成熟可結算)先補
                (cutoff,),
            ).fetchall()
        pending = [
            s for s in unique_codes(
                (str(r[0] or "").strip() for r in rows), filter_inactive=False
            )
            if s and s not in holding_set
        ][:cap]
        if not pending:
            return {"ok": True, "symbols": 0, "note": "無未結算(未逾期)預測，略過"}
        updated = backend.update_prices(
            pending,
            refresh_info=False,
            include_extended=False,   # 結算只需日K收盤,關擴充資料集壓低 FinMind 額度
            force_refresh=force_refresh,
        )
        settled = backend.update_outcomes()   # 補進新價格後,當場結算可成熟的那批
        return {"ok": True, "symbols": len(pending), "updatedRows": updated, "settlement": settled}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def load_sinopac_symbols():
    result = sinopac_backend.holdings()
    codes = unique_codes(result.get("codes") or [])
    if not codes:
        raise RuntimeError("永豐 API 庫存沒有股票代碼")
    return {
        "source": "sinopac_api",
        "accountMasked": result.get("accountMasked"),
        "symbols": codes,
        "holdingsCount": result.get("count", len(codes)),
    }


def _atomic_write_text(path, text):
    # write_text() 對既有檔案是 open(mode='w') 先截斷再寫入，若寫入耗時被拉長
    # (大 payload 或 OneDrive 同步鎖檔延遲)，讀取端可能讀到空檔/半截 JSON。
    # 改成寫暫存檔再 os.replace()，讀取端要嘛看到完整舊檔、要嘛看到完整新檔。
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _prune_old_run_logs(keep=DAILY_UPDATE_LOG_RETENTION):
    files = sorted(LOG_DIR.glob("daily_update_*.json"), reverse=True)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def save_run_log(payload):
    LOG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"daily_update_{stamp}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write_text(path, text)
    _atomic_write_text(LOG_DIR / "latest.json", text)
    _prune_old_run_logs()
    return path


def set_daily_meta(status, payload, log_path):
    with backend.connect() as conn:
        backend.set_meta(conn, "last_daily_job_status", status)
        backend.set_meta(conn, "last_daily_job_at", now_text())
        backend.set_meta(conn, "last_daily_job_symbols", ",".join(payload.get("symbols", [])))
        backend.set_meta(conn, "last_daily_job_source", payload.get("source", ""))
        backend.set_meta(conn, "last_daily_job_log", str(log_path))
        backend.set_meta(conn, "last_daily_training_symbols", ",".join(payload.get("trainingSymbols", [])))
        backend.set_meta(conn, "last_daily_training_symbol_count", str(len(payload.get("trainingSymbols", []))))
        backend.set_meta(conn, "last_daily_training_sources", json.dumps(payload.get("trainingSources", {}), ensure_ascii=False))
        backend.set_meta(conn, "last_daily_training_errors", " | ".join(payload.get("trainingErrors", [])))
        if payload.get("error"):
            backend.set_meta(conn, "last_daily_job_error", payload["error"])
        elif status == "success":
            conn.execute("DELETE FROM model_meta WHERE key = ?", ("last_daily_job_error",))


def run(force_refresh=False, train=True, scan_monster=None):
    payload = {
        "ok": False,
        "startedAt": now_text(),
        "finishedAt": None,
        "source": None,
        "symbols": [],
        "sinopacError": None,
        "result": None,
        "error": None,
        "forceRefresh": force_refresh,
        "trained": train,
        "scanMonster": scan_monster,
    }
    try:
        try:
            payload["inactiveSymbolRefresh"] = backend.refresh_twse_delisted_periods()
        except Exception as exc:
            payload["inactiveSymbolRefresh"] = {
                "ok": False,
                "error": str(exc),
                "note": "沿用資料庫內已核對的停止交易期間，不放行未知行情。",
            }
        payload["invalidDataCleanup"] = backend.cleanup_invalid_production_data()
        try:
            portfolio = load_sinopac_symbols()
        except Exception as exc:
            portfolio = {
                "source": "default_symbols",
                "symbols": unique_codes(DEFAULT_SYMBOLS),
                "sinopacError": str(exc),
            }
            payload["sinopacError"] = str(exc)

        payload["source"] = portfolio["source"]
        payload["symbols"] = portfolio["symbols"]
        if portfolio.get("accountMasked"):
            payload["accountMasked"] = portfolio["accountMasked"]
        if portfolio.get("holdingsCount") is not None:
            payload["holdingsCount"] = portfolio["holdingsCount"]

        training_scope = build_daily_training_symbols(payload["symbols"])
        payload["trainingSymbols"] = training_scope["symbols"]
        payload["trainingSources"] = training_scope["sources"]
        payload["trainingErrors"] = training_scope["errors"]
        if training_scope["errors"]:
            # monster_liquid 抓取失敗(FinMind額度用盡/DB鎖死)時，訓練池會
            # 靜默退化成只剩 sector_diversified∪holdings(通常遠少於預期的
            # 800+檔妖股候選)，模型訓練仍會「成功」跑完，不會觸發任何失敗
            # 類告警。比照 run_daily_data_integrity_check 的LINE通知模式，
            # 讓使用者至少知道今天的訓練池組成不完整，不用自己回頭查log。
            try:
                send_line_message(
                    "每日訓練池組成有錯誤，可能已縮水：\n" + "\n".join(training_scope["errors"][:5]),
                    priority="critical",
                )
            except Exception:
                pass

        try:
            result = backend.full_daily_update(
                payload["symbols"],
                force_refresh=force_refresh,
                training_symbols=training_scope["symbols"],
                train=train,
            )
        except Exception as exc:
            # full_daily_update()（抓價/訓練/結算）失敗不能連帶讓下面的
            # 資料完整性檢查/LINE通知也一起被跳過——run_daily_data_integrity_
            # check 是這支腳本唯一會主動用 LINE 通知使用者「今天資料有問題」
            # 的地方，之前這裡沒有獨立 try/except，一旦 full_daily_update
            # 拋例外(實際發生過：database is locked)，就會直接跳到最外層
            # except，使用者完全不會收到任何通知，只能自己開網頁才會發現
            # 今天完全沒更新。
            payload["error"] = str(exc)
            payload["traceback"] = traceback.format_exc()
            result = {"ok": False, "error": str(exc)}
        if result.get("ok") is False and not payload.get("error"):
            # full_daily_update 可在不拋例外的情況下回報「官方快照只有部分
            # 覆蓋」；這也必須標記每日更新失敗，否則盤中會誤信資料已完整。
            payload["error"] = str(result.get("error") or "每日官方日K同步未完成")
        run_monster_scan = (
            bool(scan_monster)
            if scan_monster is not None
            else os.environ.get("MONSTER_SCAN_ON_DAILY") == "1"
        )
        if run_monster_scan and not payload.get("error"):
            monster_limit = int(os.environ.get("MONSTER_SCAN_LIMIT", "120") or 120)
            try:
                result["monsterScan"] = backend.scan_monster_scores(limit=monster_limit)
            except Exception as exc:
                result["monsterScan"] = {
                    "ok": False,
                    "error": str(exc),
                    "note": "妖股掃描失敗，已略過每日更新的附加掃描。",
                }
        else:
            result["monsterScan"] = {
                "ok": True,
                "skipped": True,
                "note": "本次未啟用妖股掃描。",
            }
        try:
            result["brainV2Snapshots"] = save_daily_brain_v2_snapshots()
        except Exception as exc:
            # 跟上面 full_daily_update() 同一種道理：這個呼叫本身沒有獨立
            # try/except 的話，例外會直接跳到最外層 except，讓下面唯一會
            # 主動用LINE通知使用者「今天資料有問題」的 run_daily_data_
            # integrity_check 整段被跳過不執行。
            result["brainV2Snapshots"] = {"saved": 0, "total": 0, "errors": [str(exc)]}
        result["dbBackup"] = backup_database()
        result["dataIntegrityCheck"] = run_daily_data_integrity_check(training_scope["symbols"])
        result["candidateAdvancedFlow"] = backfill_candidate_advanced_flow(payload, force_refresh)
        try:
            # 注意/處置股名單(TWSE 免費 OpenAPI)——存快取供妖股雷達 list_monster_scores
            # 貼風險旗標(已進處置/注意股)。fail-soft,抓失敗不覆蓋舊快取、不中斷排程。
            _ad = backend.refresh_attention_disposition()
            result["attentionDisposition"] = {
                "ok": _ad is not None,
                "disposition": len((_ad or {}).get("disposition") or []),
                "attention": len((_ad or {}).get("attention") or []),
            }
        except Exception as exc:
            result["attentionDisposition"] = {"ok": False, "error": str(exc)}
        # 只有明確執行模型循環時才補價/結算；早上 train=False 的正式資料工作
        # 不執行任何模型維護，避免模型再次黏回雷達與持股資料管線。
        result["pendingPredictionBackfill"] = (
            backfill_pending_prediction_prices(payload, force_refresh)
            if train else {
                "ok": True,
                "skipped": True,
                "note": "模型補價與結算由收盤後獨立模型循環處理。",
            }
        )
        payload["ok"] = not payload.get("error")
        payload["result"] = result
        return payload
    except Exception as exc:
        payload["error"] = str(exc)
        payload["traceback"] = traceback.format_exc()
        return payload
    finally:
        payload["finishedAt"] = now_text()
        try:
            log_path = save_run_log(payload)
        except Exception as exc:
            # save_run_log 本身沒有 try/except 保護的話，磁碟滿/OneDrive 鎖檔
            # 等問題會讓這個例外從 finally 往外傳播、蓋掉 try/except 已經決定
            # 的 payload 返回值，導致下面的 set_daily_meta 完全沒機會執行，讓
            # latest.json 跟 DB 的 last_daily_job_at 同時卡在舊值，觸發每日
            # 更新無限重跑。
            payload["logWriteError"] = str(exc)
            log_path = ""
        try:
            set_daily_meta("success" if payload["ok"] else "failed", payload, log_path)
        except Exception as exc:
            # DB 寫入失敗(常見原因：database is locked)不能讓這個例外蓋掉
            # 原本的 payload 結果，否則 server.py 的「今天是否已嘗試過」判斷會
            # 一直讀不到今天的紀錄，導致每 5 分鐘重新整個跑一次每日更新。
            payload["metaWriteError"] = str(exc)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    # 強制忽略當日快取重新抓取+重訓：python3 daily_update.py --force-refresh
    # (或設環境變數 FORCE_REFRESH=1)，不用等隔天 load_cached_symbol_rows_if_fresh
    # 的「今天已抓過」短路判斷自然過期。
    force_refresh_flag = "--force-refresh" in sys.argv or os.environ.get("FORCE_REFRESH") == "1"
    output = run(force_refresh=force_refresh_flag)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(0 if output.get("ok") else 1)
