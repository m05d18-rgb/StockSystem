from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import bisect
import copy
import datetime as dt
import hashlib
import ssl
import json
import math
import os
import pickle
import re
import sqlite3
import statistics
import sys
import threading
import time
import warnings
from collections import Counter

from portfolio_exit import (
    BUY_COMMISSION_RATE,
    EXIT_SLIPPAGE_RATE,
    SELL_COMMISSION_RATE,
    SELL_TAX_RATE,
    build_position_exit,
    exit_policy_payload,
    normalize_strategy_horizon,
)
from market_calendar import load_market_session_overrides, planned_market_day

try:
    from importlib import metadata as importlib_metadata
except Exception:
    importlib_metadata = None

try:
    import numpy as np
    from sklearn.ensemble import (
        GradientBoostingRegressor,
        HistGradientBoostingClassifier,
        HistGradientBoostingRegressor,
        IsolationForest,
    )
except Exception:
    np = None
    GradientBoostingRegressor = None
    HistGradientBoostingClassifier = None
    HistGradientBoostingRegressor = None
    IsolationForest = None

try:
    from xgboost import XGBClassifier
except Exception:
    XGBClassifier = None

try:
    from lightgbm import LGBMClassifier
except Exception:
    LGBMClassifier = None


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "stock_system.sqlite3"
MODEL_PATH = ROOT / "model.pkl"
MODEL_ENV_PATH = ROOT / "model_env.json"
FINMIND_USAGE_PATH = ROOT / "finmind_usage.json"
RADAR_RULE_CONFIG_PATH = ROOT / "radar_rule_weights.json"
MARKET_SESSION_OVERRIDES_PATH = ROOT / "market_session_overrides.json"
TWSE_DELISTED_URL = "https://www.twse.com.tw/rwd/zh/company/suspendListing?response=json"
TWSE_ACTIVE_COMPANIES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_ACTIVE_COMPANIES_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
RADAR_COMPLETE_DAILY_MIN_ROWS = 1500
TWSE_AFTER_TRADING_MIN_ROWS = 700
TPEX_AFTER_TRADING_MIN_ROWS = 400
TPEX_EMERGING_MIN_ROWS = 100
DATA_POLICY_VERSION = "official-licensed-real-data-v15-multitf"
DEFAULT_SYMBOLS = [
    # 半導體
    "2330", "2454", "2303", "2379", "3711", "3037", "3008",
    # 電子製造
    "2317", "2382", "2308", "2357", "2395",
    # 金融
    "2881", "2882", "2884", "2886", "2891",
    # 航運
    "2603", "2609", "2615",
    # 電信
    "2412", "3045",
    # 石化
    "1301", "1303",
    # 中小型/景氣循環（面板、記憶體、晶圓代工、被動元件等高波動族群，
    # 避免訓練樣本只有大型穩定股，導致異常偵測等模型對這類股票失去鑑別度）
    "3481", "2409", "6182", "2344", "6770", "6138", "5328",
    "4722", "2313", "6116", "2363", "2481", "2498",
    # 妖股基因股（2023-2026 妖股飆股型態回測中策略累計報酬最高的股票，
    # 讓模型多看「真的會飆」的樣本，對齊妖股短線基準）
    "3673", "3653", "1560", "6187", "6223", "8054", "3665", "2543",
    # 全產業別多樣化補充（依 stock_info.sector 實際分類，每個產業取
    # 1-2 檔流動性最佳的代表股，避免訓練樣本集中在少數族群、讓模型對
    # 未涵蓋產業的股票缺乏鑑別度）
    "1101", "1102", "1216", "1314", "1316", "1319", "1326", "1402",
    "1409", "1504", "1536", "1605", "1609", "1711", "1717", "1785",
    "1795", "1802", "1809", "1815", "1905", "1909", "2002", "2027",
    "2103", "2105", "2258", "2312", "2324", "2337", "2367", "2371",
    "2374", "2408", "2468", "2485", "2515", "2610", "2618", "2731",
    "2745", "2887", "2903", "2915", "3231", "3293", "3324",
    "3450", "3546", "4142", "4171", "4743", "4979", "5864", "5871",
    "6016", "6148", "6443", "6505", "6578", "6616", "6629", "6757",
    "6763", "6869", "6870", "6873", "6919", "6925", "6936",
    "6952", "7566", "7723", "7780", "7781", "8033", "8044", "8096",
    "8112", "8390", "8422", "8464", "8476", "8926", "8932", "8933",
    "8938", "9904", "9910", "9934",
]
MIN_MONSTER_AVG_VOLUME_LOTS = 1000
MIN_MONSTER_TURNOVER_MILLION = 30
# 模型紙上交易沒有實際委託張數，固定以一張普通股做可比較的淨損益模擬。
# 使用未折扣的台股手續費與一般賣出證交稅；實際券商折扣/當沖稅率仍以券商帳務為準。
PAPER_TRADE_SHARES = 1000
PAPER_BUY_COMMISSION_RATE = 0.001425
PAPER_SELL_COMMISSION_RATE = 0.001425
PAPER_SELL_TAX_RATE = 0.003
PAPER_BASE_SLIPPAGE_RATE = 0.001
PAPER_MAX_VOLUME_PARTICIPATION = 0.05
PAPER_LIMIT_MOVE_THRESHOLD = 0.095
PAPER_INITIAL_CAPITAL = 2_500_000
PAPER_MAX_OPEN_POSITIONS = 20
PAPER_DEFAULT_MAX_HOLD_SESSIONS = 20
MONSTER_WATCH_SYMBOLS = {
    symbol.strip()
    for symbol in os.environ.get("MONSTER_WATCH_SYMBOLS", "").split(",")
    if symbol.strip()
}
# 證交所公告的停止交易生效日。完整終止上市清單會同步進資料庫；這份最小
# 種子讓新資料庫或官方端暫時離線時，仍可先擋住目前已確認會被 FinMind
# 錯配成其他股票行情的代號。日期以前的真實歷史仍保留。
KNOWN_INACTIVE_PERIODS = {
    "1589": {"from": "2026-05-27", "status": "suspended", "name": "永冠-KY"},
    "1701": {"from": "2024-09-02", "status": "delisted", "name": "中化"},
    "1704": {"from": "2019-01-30", "status": "delisted", "name": "榮化"},
    "2311": {"from": "2018-04-30", "status": "delisted", "name": "日月光"},
    "2325": {"from": "2018-04-30", "status": "delisted", "name": "矽品"},
    "2358": {"from": "2024-11-19", "status": "delisted", "name": "廷鑫"},
    "2443": {"from": "2024-11-19", "status": "delisted", "name": "昶虹"},
    "2448": {"from": "2021-01-06", "status": "delisted", "name": "晶電"},
    "2456": {"from": "2022-01-05", "status": "delisted", "name": "奇力新"},
    "2499": {"from": "2020-11-10", "status": "delisted", "name": "東貝"},
    "2809": {"from": "2025-10-01", "status": "delisted", "name": "京城銀"},
    "2823": {"from": "2021-12-30", "status": "delisted", "name": "中壽"},
    "2888": {"from": "2025-07-24", "status": "delisted", "name": "新光金"},
    "3454": {"from": "2026-03-27", "status": "delisted", "name": "晶睿"},
    "4725": {"from": "2021-01-18", "status": "delisted", "name": "信昌化"},
    "5264": {"from": "2021-01-15", "status": "delisted", "name": "鎧勝-KY"},
    "5305": {"from": "2020-11-30", "status": "delisted", "name": "敦南"},
    "6288": {"from": "2025-08-15", "status": "delisted", "name": "聯嘉光電"},
    "6806": {"from": "2026-06-23", "status": "delisted", "name": "森崴能源"},
}
RETIRED_SYMBOLS = frozenset(
    symbol for symbol, item in KNOWN_INACTIVE_PERIODS.items()
    if item.get("status") == "delisted"
)
EXCLUDED_CANDIDATE_SYMBOLS = set(RETIRED_SYMBOLS)
RESERVED_PRODUCTION_STRATEGIES = frozenset({"test", "__test__"})
MODEL_MIN_PRICE_ROWS = 120
MODEL_RECENT_WINDOW = 120
MODEL_MIN_CHIP_COVERAGE = 0.50
MODEL_MIN_FINANCE_COVERAGE = 0.25
FINMIND_HOURLY_LIMIT = 6000
FINMIND_RESERVED_CALLS = 1000
FINMIND_SAFE_HOURLY_LIMIT = FINMIND_HOURLY_LIMIT - FINMIND_RESERVED_CALLS
# 核心資料集(價格/籌碼/融資券/月營收/PER/財報三表)已經佔了 8 個，當沖/借券、
# 分點籌碼(taiwan_stock_trading_daily_report_secid_agg)兩個新的進階籌碼資料集，
# include_extended=True 全開時最多用到 12 個，這裡要設到 >= 12 才不會卡住、
# 擴充資料集形同沒接上。
FINMIND_MAX_DATASETS_PER_SYMBOL = 12
MONSTER_MAX_UPDATE_SYMBOLS = 300
FINMIND_OPTIONAL_SHORT_TERM_DATASETS = {
    "TaiwanStockInstitutionalInvestorsBuySell",
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockMarginPurchaseShortSale",
    "TaiwanStockMonthRevenue",
    "TaiwanStockPER",
    "TaiwanStockFinancialStatements",
    "TaiwanStockBalanceSheet",
    "TaiwanStockCashFlowsStatement",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockKBar",
    # 這個不是真的 FinMind dataset 名稱(secid_agg 是獨立 API 路徑，不是
    # /data?dataset=... 的標準介面)，只是借用同一個白名單機制做額度記錄/
    # 開關控制，實際抓取邏輯在 fetch_finmind_secid_agg/fetch_secid_agg_optional。
    "TaiwanStockTradingDailyReportSecIdAgg",
}
DATA_SOURCE_PRIORITY = [
    "SinoPac Shioaji: intraday holdings price, positions, portfolio alerts",
    "TWSE / TPEx official: latest daily OHLCV, institutional flows, margin, PER/PBR, monthly revenue",
    "FinMind: paid licensed data source; allowed for formal model scores",
    "Yahoo Finance: non-official fallback only; excluded from formal model scores",
]
REQUIRED_MODEL_PACKAGES = {
    "numpy": "2.4.4",
    "scipy": "1.17.1",
    "scikit-learn": "1.8.0",
    "xgboost": "3.3.0",
    "lightgbm": "4.6.0",
    "shioaji": "1.3.3",
}
MODEL_PACKAGE_IMPORTS = {
    "numpy": "numpy",
    "scipy": "scipy",
    "scikit-learn": "sklearn",
    "xgboost": "xgboost",
    "lightgbm": "lightgbm",
    "shioaji": "shioaji",
}
OFFICIAL_SOURCE_KEYWORDS = (
    "twse",
    "tpex",
    "mops",
    "taifex",
    "finmind",
    "shioaji",
    "sinopac",
    "永豐",
)
DB_WRITE_LOCK = threading.RLock()
DB_WAL_READY = False

# reserve_finmind_call 是「讀檔→加一→寫檔」三步，每日更新執行緒、妖股掃描
# 執行緒、HTTP handler 會同時呼叫；沒有鎖的話兩條執行緒讀到同一個計數各自
# +1 寫回，實際打了兩次 API 計數卻只多 1，額度保護會低估用量而被打穿。
FINMIND_USAGE_LOCK = threading.Lock()

# predict_symbol 結果快取的存活秒數。預測建立在「日K資料」上(盤中報價完全
# 不會進 predict_symbol)，日K只在每日更新/官方快照同步/補抓時才變動，且那些
# 寫入路徑都會主動失效快取；TTL 只是第三道保險，順便涵蓋 buy_signal_threshold
# 這種會隨結算紀錄緩慢變動的間接輸入。
PREDICT_RESULT_CACHE_TTL_SECONDS = 120

# 妖股雷達仍以「10 日內 +10%」追蹤爆發型候選；這組常數不再作為獨立 AI
# 的訓練標籤。AI 使用下方 SHORT_PROFIT_* 多週期扣成本淨報酬目標。
MONSTER_TARGET_RETURN = 0.10
MONSTER_TARGET_HORIZON_DAYS = 10
SHORT_PROFIT_TARGET_TYPE = "short-profit-net-v1"
SHORT_PROFIT_HORIZONS = (3, 5, 10)
SHORT_PROFIT_HORIZON_WEIGHTS = {3: 0.20, 5: 0.35, 10: 0.45}
SHORT_PROFIT_MAX_HORIZON_DAYS = max(SHORT_PROFIT_HORIZONS)
SHORT_PROFIT_DOWNSIDE_PENALTY = 0.20
SHORT_PROFIT_POLICY = {
    "targetType": SHORT_PROFIT_TARGET_TYPE,
    "horizons": list(SHORT_PROFIT_HORIZONS),
    "horizonWeights": SHORT_PROFIT_HORIZON_WEIGHTS,
    "entry": "next-day-open",
    "costs": {
        "entrySlippage": 0.001,
        "buyCommission": 0.001425,
        "sellCommission": 0.001425,
        "sellTax": 0.003,
    },
    "downsidePenalty": SHORT_PROFIT_DOWNSIDE_PENALTY,
    "positiveRule": "weighted 3/5/10-day net return remains positive after downside penalty",
}
SHORT_PROFIT_POLICY_HASH = hashlib.sha256(
    json.dumps(SHORT_PROFIT_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()[:16]
RADAR_STOP_LOSS_RETURN = -0.07
RADAR_MIN_FORMAL_SCORE = 60.0
# 盤中實際成交價不可偏離日線觸發價太遠；2% 也與既有 V 轉分支
# current <= buy_trigger * 1.02 的上限一致，避免突破分支反而能無限追價。
RADAR_MAX_ENTRY_DRIFT_PCT = 2.0
# 用未折扣手續費、證交稅與盤中估計滑價後，+10% / -7% 基本合約的
# 淨風報約 1.21。保留 1.15 下限，讓一般窄價差通過、接近價差上限的
# 交易因實際成本侵蝕而降級成觀察。
RADAR_MIN_NET_REWARD_RISK_RATIO = 1.15
RADAR_REGIME_LABELS = {
    "strong_breadth": "強勢擴散",
    "theme_rotation": "題材輪動",
    "risk_off": "弱勢避險",
}
RADAR_THRESHOLD_CANDIDATES = (60.0, 65.0, 70.0, 75.0, 80.0, 85.0)
DEFAULT_RADAR_REGIME_THRESHOLDS = {
    key: RADAR_MIN_FORMAL_SCORE for key in RADAR_REGIME_LABELS
}
RADAR_RULE_WEIGHT_KEYS = ("volume", "month_high", "market_strength", "surge", "counter")
RADAR_LIVE_WEIGHT_MIN_SETTLED = 50
DEFAULT_RADAR_RULE_WEIGHTS = {
    "volume": 44.0,
    "month_high": 34.0,
    "market_strength": 16.0,
    "surge": 0.0,
    "counter": 6.0,
}
DEFAULT_RADAR_ENTRY_GUARDRAILS = {
    "blockSurgeSetup": False,
    "maxRet5": None,
}
LEGACY_INTRADAY_CONFIRMED_ENTRY_MODES = frozenset({"intraday_execution_analysis"})
WRITE_SQL_PREFIXES = (
    "begin immediate",
    "insert",
    "update",
    "delete",
    "replace",
    "create",
    "alter",
    "drop",
    "pragma journal_mode",
    "pragma wal_checkpoint",
    "vacuum",
)

RECURRING_INVESTMENT_EVIDENCE_KEYS = {
    "investmentplan",
    "investment_plan",
    "recurringinvestment",
    "recurring_investment",
    "stocksavings",
    "stock_savings",
    "ordersource",
    "order_source",
    "sourcetype",
    "source_type",
    "customfield",
    "custom_field",
}
RECURRING_INVESTMENT_MARKERS = {
    "stock_savings",
    "stocksaving",
    "stocksavings",
    "recurring_investment",
    "recurringinvestment",
    "定期定額",
    "存股",
}


def classify_explicit_buy_strategy_horizon(payload):
    """Classify only explicit broker/source evidence; never infer from the symbol."""
    matches = []

    def normalize_marker(value):
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    def walk(value, depth=0):
        if depth > 5:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key or "").strip().lower()
                if normalized_key in RECURRING_INVESTMENT_EVIDENCE_KEYS:
                    marker = normalize_marker(child)
                    if child is True or marker in RECURRING_INVESTMENT_MARKERS:
                        matches.append({"key": str(key), "value": child})
                if isinstance(child, (dict, list, tuple)):
                    walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)

    walk(payload if isinstance(payload, dict) else {})
    if matches:
        return {
            "strategyHorizon": "long_trend",
            "strategyHorizonSource": "explicit_recurring_investment_evidence",
            "evidence": matches[:10],
        }
    return {
        "strategyHorizon": "unknown",
        "strategyHorizonSource": "no_explicit_strategy_evidence",
        "evidence": [],
    }


def is_intraday_confirmed_entry_mode(value):
    mode = str(value or "")
    return (
        mode.startswith("intraday_confirmed")
        or mode in LEGACY_INTRADAY_CONFIRMED_ENTRY_MODES
    )


def _normalize_radar_entry_guardrail_rules(raw):
    if not isinstance(raw, dict):
        return None
    block_surge = raw.get("blockSurgeSetup", False)
    if not isinstance(block_surge, bool):
        return None
    max_ret5 = raw.get("maxRet5")
    if max_ret5 is not None:
        if isinstance(max_ret5, bool):
            return None
        try:
            max_ret5 = float(max_ret5)
        except (TypeError, ValueError):
            return None
        if not 5.0 <= max_ret5 <= 50.0:
            return None
    return {
        "blockSurgeSetup": block_surge,
        "maxRet5": max_ret5,
    }


def _normalize_radar_live_weight_validation(raw):
    raw = raw if isinstance(raw, dict) else {}
    try:
        settled = max(0, int(raw.get("settled") or 0))
    except (TypeError, ValueError):
        settled = 0
    try:
        avg_net_return = float(raw.get("avgNetReturn"))
    except (TypeError, ValueError):
        avg_net_return = None
    try:
        profit_factor = float(raw.get("profitFactor"))
    except (TypeError, ValueError):
        profit_factor = None
    entry_mode = str(raw.get("entryMode") or "intraday_confirmed")
    sample_gate_passed = bool(
        entry_mode == "intraday_confirmed"
        and settled >= RADAR_LIVE_WEIGHT_MIN_SETTLED
    )
    performance_gate_passed = bool(
        avg_net_return is not None and avg_net_return > 0
        and profit_factor is not None and profit_factor > 1
    )
    approved = bool(
        raw.get("approved") is True
        and sample_gate_passed
        and performance_gate_passed
    )
    return {
        **raw,
        "entryMode": entry_mode,
        "settled": settled,
        "minimumSettled": RADAR_LIVE_WEIGHT_MIN_SETTLED,
        "avgNetReturn": avg_net_return,
        "profitFactor": profit_factor,
        "sampleGatePassed": sample_gate_passed,
        "performanceGatePassed": performance_gate_passed,
        "approved": approved,
        "frozen": not approved,
    }


def load_radar_rule_config(path=RADAR_RULE_CONFIG_PATH):
    """Load only an explicitly approved, rule-only walk-forward calibration."""
    fallback = {
        "approved": False,
        "source": "built_in_default",
        "effectiveWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
        "liveValidation": _normalize_radar_live_weight_validation({}),
        "regimeThresholdCalibration": {
            "approved": False,
            "baseThreshold": RADAR_MIN_FORMAL_SCORE,
            "effectiveThresholds": dict(DEFAULT_RADAR_REGIME_THRESHOLDS),
            "regimes": {},
        },
        "entryGuardrailCalibration": {
            "approved": False,
            "policy": "fixed_rule_only_no_chase",
            "recommendedKey": "baseline",
            "recommendedLabel": "基準（不限制）",
            "recommendedRules": dict(DEFAULT_RADAR_ENTRY_GUARDRAILS),
            "effectiveRules": dict(DEFAULT_RADAR_ENTRY_GUARDRAILS),
        },
    }
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback
    def validate(raw):
        try:
            values = {key: float(raw[key]) for key in RADAR_RULE_WEIGHT_KEYS}
        except (KeyError, TypeError, ValueError):
            return None
        if any(value < 0 for value in values.values()):
            return None
        if not math.isclose(sum(values.values()), 100.0, rel_tol=0, abs_tol=0.05):
            return None
        return values

    recommended = validate(payload.get("recommendedWeights") or {})
    effective = validate(payload.get("effectiveWeights") or {})
    live_validation = _normalize_radar_live_weight_validation(
        payload.get("liveValidation")
    )
    raw_regime_calibration = payload.get("regimeThresholdCalibration") or {}
    raw_regimes = raw_regime_calibration.get("regimes") or {}
    effective_thresholds = dict(DEFAULT_RADAR_REGIME_THRESHOLDS)
    normalized_regimes = {}
    for regime_key in RADAR_REGIME_LABELS:
        raw_regime = raw_regimes.get(regime_key) or {}
        approved = bool(raw_regime.get("approved"))
        try:
            recommended_threshold = float(
                raw_regime.get("recommendedThreshold", RADAR_MIN_FORMAL_SCORE)
            )
        except (TypeError, ValueError):
            recommended_threshold = RADAR_MIN_FORMAL_SCORE
        recommended_threshold = max(
            RADAR_MIN_FORMAL_SCORE, min(90.0, recommended_threshold)
        )
        effective_threshold = recommended_threshold if approved else RADAR_MIN_FORMAL_SCORE
        effective_thresholds[regime_key] = effective_threshold
        normalized_regimes[regime_key] = {
            **raw_regime,
            "label": RADAR_REGIME_LABELS[regime_key],
            "approved": approved,
            "recommendedThreshold": recommended_threshold,
            "effectiveThreshold": effective_threshold,
        }
    regime_calibration = {
        **raw_regime_calibration,
        "approved": any(item["approved"] for item in normalized_regimes.values()),
        "baseThreshold": RADAR_MIN_FORMAL_SCORE,
        "effectiveThresholds": effective_thresholds,
        "regimes": normalized_regimes,
    }
    raw_entry_calibration = payload.get("entryGuardrailCalibration") or {}
    normalized_entry_rules = _normalize_radar_entry_guardrail_rules(
        raw_entry_calibration.get("recommendedRules")
    )
    entry_guardrail_approved = bool(
        raw_entry_calibration.get("approved") is True
        and normalized_entry_rules is not None
        and normalized_entry_rules != DEFAULT_RADAR_ENTRY_GUARDRAILS
    )
    recommended_entry_rules = (
        normalized_entry_rules
        if normalized_entry_rules is not None
        else dict(DEFAULT_RADAR_ENTRY_GUARDRAILS)
    )
    entry_guardrail_calibration = {
        **raw_entry_calibration,
        "approved": entry_guardrail_approved,
        "policy": "fixed_rule_only_no_chase",
        "recommendedRules": recommended_entry_rules,
        "effectiveRules": (
            dict(recommended_entry_rules)
            if entry_guardrail_approved
            else dict(DEFAULT_RADAR_ENTRY_GUARDRAILS)
        ),
    }
    if not payload.get("approved"):
        if payload.get("method") != "rule_only_walk_forward" or recommended is None:
            return fallback
        return {
            **payload,
            "approved": False,
            "source": "walk_forward_observation",
            "recommendedWeights": recommended,
            "effectiveWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
            "liveValidation": live_validation,
            "regimeThresholdCalibration": regime_calibration,
            "entryGuardrailCalibration": entry_guardrail_calibration,
        }
    if effective is None:
        return fallback
    if live_validation.get("approved") is not True:
        return {
            **payload,
            "approved": False,
            "configuredApproved": True,
            "source": "live_sample_freeze",
            "recommendedWeights": recommended or effective,
            "effectiveWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
            "liveValidation": live_validation,
            "regimeThresholdCalibration": regime_calibration,
            "entryGuardrailCalibration": entry_guardrail_calibration,
        }
    return {
        **payload,
        "source": "walk_forward_approved",
        "effectiveWeights": effective,
        "liveValidation": live_validation,
        "regimeThresholdCalibration": regime_calibration,
        "entryGuardrailCalibration": entry_guardrail_calibration,
    }


RADAR_RULE_CONFIG = load_radar_rule_config()


def radar_entry_guardrail_decision(surge_setup, ret5, config=None):
    """Evaluate the approved rule-only entry guardrail using point-in-time data."""
    calibration = (config or RADAR_RULE_CONFIG).get("entryGuardrailCalibration") or {}
    rules = _normalize_radar_entry_guardrail_rules(calibration.get("effectiveRules"))
    if calibration.get("approved") is not True or rules is None:
        rules = dict(DEFAULT_RADAR_ENTRY_GUARDRAILS)
    reasons = []
    if rules["blockSurgeSetup"] and bool(surge_setup):
        reasons.append("完整追價型態未通過不追價進場防線")
    if rules["maxRet5"] is not None:
        value = None
        if not isinstance(ret5, bool):
            try:
                value = float(ret5)
            except (TypeError, ValueError):
                value = None
        if value is None:
            reasons.append("5日漲幅資料缺失，無法驗證不追價上限")
        elif value >= rules["maxRet5"]:
            reasons.append(
                f"5日漲幅 {value:.1f}% 已達不追價上限 {rules['maxRet5']:.1f}%"
            )
    return {
        "approved": calibration.get("approved") is True,
        "vetoed": bool(reasons),
        "reasons": reasons,
        "rules": rules,
        "recommendedKey": str(calibration.get("recommendedKey") or "baseline"),
        "recommendedLabel": str(calibration.get("recommendedLabel") or "基準（不限制）"),
    }


def radar_regime_threshold(regime_key, config=None):
    calibration = (config or RADAR_RULE_CONFIG).get("regimeThresholdCalibration") or {}
    thresholds = calibration.get("effectiveThresholds") or {}
    try:
        value = float(thresholds.get(regime_key, RADAR_MIN_FORMAL_SCORE))
    except (TypeError, ValueError):
        value = RADAR_MIN_FORMAL_SCORE
    return max(RADAR_MIN_FORMAL_SCORE, min(90.0, value))


def compute_sector_theme_snapshot(
    candidates, stock_info=None, history=None, min_sector_count=3, top_n=6,
):
    """Compute point-in-time sector breadth, excess returns, and theme persistence."""
    candidates = list(candidates or [])
    stock_info = stock_info or {}
    history = list(history or [])
    if not candidates:
        return {
            "sectors": {}, "hotSectors": [], "overallRet5": 0.0,
            "overallRet20": 0.0, "totalTurnoverMillion": 0.0,
            "candidateCount": 0, "breadthSectorCount": 0,
            "hotSectorCount": 0, "themeHeatMax": 0.0,
        }

    by_sector = {}
    for item in candidates:
        symbol = str(item.get("symbol") or "")
        sector = str(
            item.get("sector")
            or (stock_info.get(symbol) or {}).get("sector")
            or "台股"
        )
        by_sector.setdefault(sector, []).append(item)

    def number(item, *keys):
        for key in keys:
            try:
                value = item.get(key)
                if value is not None and math.isfinite(float(value)):
                    return float(value)
            except (AttributeError, TypeError, ValueError):
                continue
        return 0.0

    overall_ret5 = sum(number(item, "ret5", "change5") for item in candidates) / len(candidates)
    overall_ret20 = sum(number(item, "ret20", "change20") for item in candidates) / len(candidates)
    total_turnover = sum(max(0.0, number(item, "turnoverMillion", "turnover_million")) for item in candidates)

    def previous_hot_streak(sector):
        streak = 0
        for snapshot in reversed(history):
            stat = ((snapshot or {}).get("sectors") or {}).get(sector)
            if not stat or not bool(stat.get("hot")):
                break
            streak += 1
        return streak

    sectors = {}
    for sector, items in by_sector.items():
        count = len(items)
        if count < int(min_sector_count):
            continue
        avg_ret5 = sum(number(item, "ret5", "change5") for item in items) / count
        avg_ret20 = sum(number(item, "ret20", "change20") for item in items) / count
        avg_volume_ratio = sum(number(item, "volumeRatio", "volume_ratio") for item in items) / count
        turnover = sum(max(0.0, number(item, "turnoverMillion", "turnover_million")) for item in items)
        turnover_share = turnover / total_turnover if total_turnover > 0 else 0.0
        excess_ret5 = avg_ret5 - overall_ret5
        excess_ret20 = avg_ret20 - overall_ret20
        turnover_ok = turnover_share >= 0.03 if total_turnover > 0 else True
        hot = bool(turnover_ok and (excess_ret5 > 0 or excess_ret20 > 0))
        streak_days = (1 + previous_hot_streak(sector)) if hot else 0
        count_score = min(count / 5.0, 1.0) * 20.0
        turnover_score = min(turnover_share / 0.15, 1.0) * 20.0
        ret5_score = min(max(excess_ret5, 0.0) / 8.0, 1.0) * 20.0
        ret20_score = min(max(excess_ret20, 0.0) / 15.0, 1.0) * 20.0
        if streak_days >= 10:
            persistence_score = 20.0
        elif streak_days >= 5:
            persistence_score = 14.0
        elif streak_days >= 3:
            persistence_score = 8.0
        else:
            persistence_score = min(streak_days / 3.0, 1.0) * 8.0
        heat = round(count_score + turnover_score + ret5_score + ret20_score + persistence_score, 1)
        sectors[sector] = {
            "count": count,
            "avgRet5": round(avg_ret5, 2),
            "avgRet20": round(avg_ret20, 2),
            "avgVolumeRatio": round(avg_volume_ratio, 2),
            "turnoverMillion": round(turnover, 2),
            "turnoverShare": round(turnover_share, 4),
            "excessRet5": round(excess_ret5, 2),
            "excessRet20": round(excess_ret20, 2),
            "hot": hot,
            "persistentHot": streak_days >= 2,
            "streakDays": streak_days,
            "fermentation3": streak_days >= 3,
            "fermentation5": streak_days >= 5,
            "fermentation10": streak_days >= 10,
            "themeHeat": heat,
            "heatComponents": {
                "candidateCount": round(count_score, 1),
                "turnoverShare": round(turnover_score, 1),
                "excessRet5": round(ret5_score, 1),
                "excessRet20": round(ret20_score, 1),
                "persistence": round(persistence_score, 1),
            },
        }
    hot_sectors = [
        sector for sector, stat in sorted(
            sectors.items(),
            key=lambda pair: (pair[1]["themeHeat"], pair[1]["turnoverShare"]),
            reverse=True,
        )
        if stat["hot"]
    ][:max(1, int(top_n))]
    heats = [float(stat.get("themeHeat") or 0) for stat in sectors.values()]
    return {
        "sectors": sectors,
        "hotSectors": hot_sectors,
        "overallRet5": round(overall_ret5, 2),
        "overallRet20": round(overall_ret20, 2),
        "totalTurnoverMillion": round(total_turnover, 2),
        "candidateCount": len(candidates),
        "breadthSectorCount": sum(1 for stat in sectors.values() if stat["count"] >= int(min_sector_count)),
        "hotSectorCount": len(hot_sectors),
        "themeHeatMax": round(max(heats), 1) if heats else 0.0,
    }


def classify_radar_market_regime(market, sector_snapshot):
    """Classify a point-in-time radar market into one of three exhaustive states."""
    market = market or {}
    sector_snapshot = sector_snapshot or {}
    taiex_ret20 = float(market.get("taiex_ret_20") or 0.0)
    taiex_gap = float(market.get("taiex_ma_gap") or 0.0)
    otc_ret20 = float(market.get("otc_ret_20") or 0.0)
    otc_gap = float(market.get("otc_ma_gap") or 0.0)
    breadth_sectors = int(sector_snapshot.get("breadthSectorCount") or 0)
    hot_sectors = int(sector_snapshot.get("hotSectorCount") or 0)
    risk_off = bool(
        (taiex_gap < 0 and otc_gap < 0 and (taiex_ret20 < 0 or otc_ret20 < 0))
        or (taiex_ret20 <= -0.05 and otc_ret20 <= 0)
        or (otc_ret20 <= -0.05 and taiex_ret20 <= 0)
    )
    strong_breadth = bool(
        not risk_off
        and taiex_gap >= 0 and otc_gap >= 0
        and taiex_ret20 > 0 and otc_ret20 > 0
        and breadth_sectors >= 3 and hot_sectors >= 2
    )
    key = "risk_off" if risk_off else "strong_breadth" if strong_breadth else "theme_rotation"
    reason = (
        "加權與櫃買同步站上月線，且至少兩個產業呈現擴散"
        if key == "strong_breadth" else
        "大盤未全面擴散，資金集中於少數輪動題材"
        if key == "theme_rotation" else
        "加權與櫃買趨勢轉弱，正式訊號需提高防守"
    )
    return {
        "key": key,
        "label": RADAR_REGIME_LABELS[key],
        "reason": reason,
        "taiexRet20": round(taiex_ret20, 4),
        "taiexMaGap": round(taiex_gap, 4),
        "otcRet20": round(otc_ret20, 4),
        "otcMaGap": round(otc_gap, 4),
        "breadthSectorCount": breadth_sectors,
        "hotSectorCount": hot_sectors,
        "themeHeatMax": float(sector_snapshot.get("themeHeatMax") or 0.0),
    }


def radar_rule_score_components(
    volume_ratio, month_high_strength, stock_stronger, surge_setup, counter_strength, weights=None,
):
    weights = weights or RADAR_RULE_CONFIG["effectiveWeights"]
    volume_component = min(max(float(volume_ratio or 0), 0.0) / 4.0, 1.0)
    components = {
        "volume": volume_component * float(weights["volume"]),
        "monthHigh": float(weights["month_high"]) if month_high_strength else 0.0,
        "marketStrength": float(weights["market_strength"]) if stock_stronger else 0.0,
        "surge": float(weights["surge"]) if surge_setup else 0.0,
        "counter": float(weights["counter"]) if counter_strength else 0.0,
    }
    raw_score = sum(components.values())
    return {
        "score": round(max(0.0, min(100.0, raw_score)), 2),
        "rawScore": round(max(0.0, raw_score), 4),
        "components": {key: round(value, 4) for key, value in components.items()},
        "weights": {key: float(weights[key]) for key in RADAR_RULE_WEIGHT_KEYS},
    }


def radar_execution_analysis(entry_fill_price, planned_stop_price=None, estimated_exit_slippage_pct=0.1):
    """Return executable, fee-aware entry/exit economics for one radar trade.

    The entry is already an executable fill (ask quote, or current quote plus
    estimated entry slippage). The target and the -7% maximum stop distance are
    therefore anchored to that real fill, matching ``simulate_radar_trade_path``.
    """
    try:
        entry_fill = float(entry_fill_price or 0)
    except (TypeError, ValueError):
        entry_fill = 0.0
    if entry_fill <= 0:
        return {
            "ok": False,
            "entryFillPrice": None,
            "entryCostPrice": None,
            "stopPrice": None,
            "targetPrice": None,
            "estimatedExitSlippagePct": None,
            "targetNetReturnPct": None,
            "stopNetReturnPct": None,
            "netRewardRiskRatio": None,
            "minimumNetRewardRiskRatio": RADAR_MIN_NET_REWARD_RISK_RATIO,
            "rewardRiskPassed": False,
        }
    try:
        planned_stop = float(planned_stop_price or 0)
    except (TypeError, ValueError):
        planned_stop = 0.0
    if not 0 < planned_stop < entry_fill:
        planned_stop = 0.0
    try:
        estimated_slippage_pct = max(0.0, float(estimated_exit_slippage_pct or 0))
    except (TypeError, ValueError):
        estimated_slippage_pct = PAPER_BASE_SLIPPAGE_RATE * 100

    contract_stop = entry_fill * (1 + RADAR_STOP_LOSS_RETURN)
    stop_price = max(planned_stop, contract_stop)
    target_price = entry_fill * (1 + MONSTER_TARGET_RETURN)
    exit_slippage_rate = max(PAPER_BASE_SLIPPAGE_RATE, estimated_slippage_pct / 100)
    entry_cost = entry_fill * (1 + PAPER_BUY_COMMISSION_RATE)
    exit_cost_factor = (1 - exit_slippage_rate) * (
        1 - PAPER_SELL_COMMISSION_RATE - PAPER_SELL_TAX_RATE
    )
    target_proceeds = target_price * exit_cost_factor
    stop_proceeds = stop_price * exit_cost_factor
    net_reward = max(target_proceeds - entry_cost, 0.0)
    net_risk = max(entry_cost - stop_proceeds, 0.0)
    reward_risk = net_reward / net_risk if net_risk > 0 else None
    return {
        "ok": True,
        "entryFillPrice": entry_fill,
        "entryCostPrice": entry_cost,
        "stopPrice": stop_price,
        "targetPrice": target_price,
        "estimatedExitSlippagePct": exit_slippage_rate * 100,
        "targetNetReturnPct": (target_proceeds / entry_cost - 1) * 100,
        "stopNetReturnPct": (stop_proceeds / entry_cost - 1) * 100,
        "netRewardRiskRatio": reward_risk,
        "minimumNetRewardRiskRatio": RADAR_MIN_NET_REWARD_RISK_RATIO,
        "rewardRiskPassed": bool(
            reward_risk is not None
            and reward_risk >= RADAR_MIN_NET_REWARD_RISK_RATIO
        ),
    }


def radar_trade_policy_payload():
    return {
        "entry": "first confirmed ask quote; historical fallback is next-session open",
        "entrySlippageRate": PAPER_BASE_SLIPPAGE_RATE,
        "exitSlippageRate": PAPER_BASE_SLIPPAGE_RATE,
        "buyCommissionRate": PAPER_BUY_COMMISSION_RATE,
        "sellCommissionRate": PAPER_SELL_COMMISSION_RATE,
        "sellTaxRate": PAPER_SELL_TAX_RATE,
        "targetReturn": MONSTER_TARGET_RETURN,
        "stopLossReturn": RADAR_STOP_LOSS_RETURN,
        "horizonDays": MONSTER_TARGET_HORIZON_DAYS,
        "sameDayConflict": "stop_first_conservative",
        "minimumFormalScore": RADAR_MIN_FORMAL_SCORE,
        "maximumEntryDriftPct": RADAR_MAX_ENTRY_DRIFT_PCT,
        "minimumNetRewardRiskRatio": RADAR_MIN_NET_REWARD_RISK_RATIO,
        "rewardRiskBasis": "net_after_fees_tax_and_estimated_slippage",
        "regimeThresholds": {
            key: radar_regime_threshold(key) for key in RADAR_REGIME_LABELS
        },
    }


def radar_daily_watch_allowed(
    score, setup_ok, danger_risk, radar_override, surge_setup,
    counter_override, month_high_strength, minimum_score=RADAR_MIN_FORMAL_SCORE,
):
    """Single source of truth for the daily formal-watch score floor."""
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        numeric_score = 0.0
    try:
        numeric_floor = max(RADAR_MIN_FORMAL_SCORE, float(minimum_score))
    except (TypeError, ValueError):
        numeric_floor = RADAR_MIN_FORMAL_SCORE
    structural_trigger = bool(
        radar_override or surge_setup or counter_override or month_high_strength
    )
    return bool(
        setup_ok
        and not danger_risk
        and numeric_score >= numeric_floor
        and structural_trigger
    )


def radar_exit_levels(buy_trigger, close, atr):
    """Build display/alert levels from the same +10%/-7% radar contract."""
    trigger = max(0.0, float(buy_trigger or 0))
    reference_close = max(0.0, float(close or 0))
    atr_value = max(0.0, float(atr or 0))
    if trigger <= 0:
        return {
            "stopPrice": 0.0,
            "takeProfit": 0.0,
            "trailingStop": 0.0,
            "riskDistance": 0.0,
            "rewardRiskRatio": None,
        }
    technical_stop = max(reference_close - atr_value * 1.35, 0.0)
    contract_stop = trigger * (1 + RADAR_STOP_LOSS_RETURN)
    stop_price = max(technical_stop, contract_stop, 0.0)
    risk_distance = max(trigger - stop_price, atr_value * 1.15, trigger * 0.025)
    take_profit = trigger * (1 + MONSTER_TARGET_RETURN)
    actual_risk = max(trigger - stop_price, 0.0)
    reward_risk = (
        (take_profit - trigger) / actual_risk
        if actual_risk > 0 else None
    )
    return {
        "stopPrice": stop_price,
        "takeProfit": take_profit,
        "trailingStop": max(trigger - risk_distance * 0.75, 0.0),
        "riskDistance": risk_distance,
        "rewardRiskRatio": reward_risk,
    }


def simulate_radar_trade_path(entry_fill_price, future_rows):
    """Settle one radar signal with one executable, cost-aware OHLC policy.

    `entry_fill_price` is already the executable fill: a recorded ask quote for live
    confirmations, or next-session open plus entry slippage for historical fallback.
    Daily bars cannot reveal whether target or stop happened first inside one session,
    so a same-day conflict is conservatively settled as stop-first.
    """
    try:
        entry_fill = float(entry_fill_price)
    except (TypeError, ValueError):
        return None
    if entry_fill <= 0:
        return None
    rows = list(future_rows or [])[:MONSTER_TARGET_HORIZON_DAYS]
    target_price = entry_fill * (1 + MONSTER_TARGET_RETURN)
    stop_price = entry_fill * (1 + RADAR_STOP_LOSS_RETURN)
    entry_cost = entry_fill * (1 + PAPER_BUY_COMMISSION_RATE)
    exit_price = None
    exit_date = None
    exit_reason = None
    hold_days = None
    max_favorable = None
    max_adverse = None

    for day_number, row in enumerate(rows, 1):
        try:
            open_price = float(row.get("open") or row.get("close") or 0)
            high_price = float(row.get("high") or open_price or 0)
            low_price = float(row.get("low") or open_price or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if open_price <= 0 or high_price <= 0 or low_price <= 0:
            continue
        favorable = high_price / entry_fill - 1
        adverse = low_price / entry_fill - 1
        max_favorable = favorable if max_favorable is None else max(max_favorable, favorable)
        max_adverse = adverse if max_adverse is None else min(max_adverse, adverse)
        gap_stop = open_price <= stop_price
        gap_target = open_price >= target_price
        touched_stop = low_price <= stop_price
        touched_target = high_price >= target_price
        if gap_stop:
            exit_price, exit_reason = open_price, "stop_loss_gap"
        elif gap_target:
            exit_price, exit_reason = open_price, "take_profit_gap"
        elif touched_stop and touched_target:
            exit_price, exit_reason = stop_price, "stop_loss_same_day_conflict"
        elif touched_stop:
            exit_price, exit_reason = stop_price, "stop_loss"
        elif touched_target:
            exit_price, exit_reason = target_price, "take_profit_10pct"
        if exit_reason:
            exit_date = row.get("date")
            hold_days = day_number
            break

    matured = len(rows) >= MONSTER_TARGET_HORIZON_DAYS
    if exit_reason is None and matured:
        final_row = rows[MONSTER_TARGET_HORIZON_DAYS - 1]
        try:
            exit_price = float(final_row.get("close") or 0)
        except (TypeError, ValueError, AttributeError):
            exit_price = 0
        if exit_price > 0:
            exit_reason = "time_exit_10d"
            exit_date = final_row.get("date")
            hold_days = MONSTER_TARGET_HORIZON_DAYS

    settled = exit_reason is not None
    net_return = None
    if settled and exit_price and exit_price > 0:
        exit_fill = exit_price * (1 - PAPER_BASE_SLIPPAGE_RATE)
        proceeds = exit_fill * (1 - PAPER_SELL_COMMISSION_RATE - PAPER_SELL_TAX_RATE)
        net_return = proceeds / entry_cost - 1
    target_hit = bool(exit_reason and exit_reason.startswith("take_profit")) if settled else None
    stop_hit = bool(exit_reason and exit_reason.startswith("stop_loss")) if settled else None
    return {
        "settled": settled,
        "matured": matured,
        "observedDays": len(rows),
        "targetHit": target_hit,
        "stopHit": stop_hit,
        "entryFillPrice": entry_fill,
        "entryCostPrice": entry_cost,
        "targetPrice": target_price,
        "stopPrice": stop_price,
        "exitPrice": exit_price,
        "exitDate": exit_date,
        "exitReason": exit_reason,
        "holdDays": hold_days,
        "netReturn": net_return,
        "maxFavorable": max_favorable,
        "maxAdverse": max_adverse,
    }


def precision_recall_thresholds(
    records, thresholds, score_key="score", label_key="targetHit",
    net_return_key="netReturn",
):
    """Compare fixed score floors without changing any production score.

    This follows the same binary precision/recall definitions as
    sklearn.metrics.precision_recall_curve.  Thresholds are supplied by the
    caller and are evaluated only on already-settled, point-in-time records.
    """
    settled = []
    for raw in records or []:
        row = dict(raw or {})
        score = row.get(score_key)
        label = row.get(label_key)
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if label is None:
            continue
        row["_pr_score"] = score
        row["_pr_label"] = bool(label)
        settled.append(row)
    positives = sum(1 for row in settled if row["_pr_label"])
    points = []
    for raw_threshold in sorted({float(value) for value in thresholds or []}):
        predicted = [
            row for row in settled if row["_pr_score"] >= raw_threshold
        ]
        true_positives = sum(1 for row in predicted if row["_pr_label"])
        false_positives = len(predicted) - true_positives
        false_negatives = positives - true_positives
        precision = (
            true_positives / len(predicted) if predicted else None
        )
        recall = true_positives / positives if positives else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None
            and precision + recall > 0 else None
        )
        net_returns = []
        for row in predicted:
            value = row.get(net_return_key)
            try:
                net_returns.append(float(value))
            except (TypeError, ValueError):
                continue
        wins = [value for value in net_returns if value > 0]
        losses = [-value for value in net_returns if value <= 0]
        points.append({
            "threshold": raw_threshold,
            "predictedPositive": len(predicted),
            "truePositive": true_positives,
            "falsePositive": false_positives,
            "falseNegative": false_negatives,
            "precision": round(precision, 6) if precision is not None else None,
            "recall": round(recall, 6) if recall is not None else None,
            "f1": round(f1, 6) if f1 is not None else None,
            "averageNetReturn": (
                round(sum(net_returns) / len(net_returns), 6)
                if net_returns else None
            ),
            "profitFactor": (
                round(sum(wins) / sum(losses), 6)
                if losses and sum(losses) > 0 else None
            ),
        })
    return {
        "method": "fixed_threshold_precision_recall",
        "samples": len(settled),
        "actualPositive": positives,
        "points": points,
        "scoreChanged": False,
    }


class LockedConnection(sqlite3.Connection):
    """把 DB_WRITE_LOCK 的持有範圍跟 SQLite 交易對齊的連線。

    舊版以「單一語句」為單位取放 RLock，但一個寫入交易（例如批次 upsert）
    橫跨多個語句：語句之間 RLock 被讓出去，另一條執行緒的連線插進來寫入
    時會撞到本連線尚未 commit 的 SQLite 交易——變成「A 持 RLock 等 SQLite
    鎖、B 持 SQLite 交易等 RLock」的死鎖，搭配 busy_timeout 60 秒 × 24 次
    重試，每輪僵局會凍結 20 分鐘以上（2026-07-02 上午的掃描全卡死於此）。

    修正：第一個寫入語句取得 RLock 後一路持有到 commit/rollback/close 才
    釋放，確保「持有未完成寫入交易」和「持有 RLock」永遠是同一條執行緒，
    程序內不可能再出現互等。跨程序（tick collector 等）仍靠 busy_timeout
    與重試處理。RLock 可重入，同一執行緒巢狀 execute 不受影響。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._holds_write_lock = False

    def _acquire_write_lock(self):
        if not self._holds_write_lock:
            DB_WRITE_LOCK.acquire()
            self._holds_write_lock = True

    def _release_write_lock(self):
        if self._holds_write_lock:
            self._holds_write_lock = False
            DB_WRITE_LOCK.release()

    def _run_with_retry(self, func, *args):
        last_error = None
        for attempt in range(24):
            try:
                return func(*args)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(min(0.25 * (attempt + 1), 2.0))
        raise last_error

    def execute(self, sql, parameters=(), /):
        sql_text = str(sql or "").lstrip().lower()
        if sql_text.startswith(WRITE_SQL_PREFIXES):
            self._acquire_write_lock()
            return self._run_with_retry(super().execute, sql, parameters)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        sql_text = str(sql or "").lstrip().lower()
        if sql_text.startswith(WRITE_SQL_PREFIXES):
            self._acquire_write_lock()
            return self._run_with_retry(super().executemany, sql, parameters)
        return super().executemany(sql, parameters)

    def executescript(self, sql, /):
        self._acquire_write_lock()
        return self._run_with_retry(super().executescript, sql)

    def commit(self):
        try:
            return self._run_with_retry(super().commit)
        finally:
            self._release_write_lock()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._release_write_lock()

    def close(self):
        try:
            return super().close()
        finally:
            self._release_write_lock()

    def __exit__(self, exc_type, exc_val, exc_tb):
        # sqlite3.Connection.__exit__ 內部會 commit（正常）或 rollback（例外），
        # 兩者都會釋放寫入鎖；close() 再兜底一次。
        result = super().__exit__(exc_type, exc_val, exc_tb)
        self.close()
        return result

NON_OFFICIAL_SOURCE_KEYWORDS = (
    "yahoo",
    "simulate",
    "simulation",
    "proxy",
    "fallback",
    "estimate",
    "derived",
    "推估",
)
FEATURE_NAMES = [
    "ma_trend",
    "rsi",
    "macd_pct",
    "atr_pct",
    "ret_5",
    "ret_20",
    "ret_60",
    "volume_ratio",
    "boll_position",
    "chip_score",
    "finance_score",
    "taiex_ret_20",
    "taiex_ma_gap",
    "otc_ret_20",
    "nasdaq_ret_1",
    "sp500_ret_1",
    "usdtwd_ret_20",
    "market_regime",
    "stock_vs_taiex_20",
    "sector_rel_strength",
    "valuation_score",
    "daytrade_risk",
    "lending_risk",
    "short_pressure",
    "revenue_momentum",
    "fundamental_score",
    "chip_data_coverage",
    "finance_data_coverage",
    "advanced_flow_score",
    "realtime_money_flow_score",
    "advanced_flow_coverage",
    "foreign_consec_buy",
    "trust_consec_buy",
    "foreign_weekly_chg",
    "weekly_rsi",
    "monthly_ma_trend",
    # 2026-07-03 #164特徵工程新增(加在尾端,不動既有索引——quick_monster_filter
    # 直接用values[4]/[5]/[7]/[18]定位)。改FEATURE_NAMES長度會讓舊model.pkl
    # 的feature_names比對失效,加完必須立刻重訓。
    "margin_delta_flow",
    "daytrade_imbalance",
]


def short_profit_policy_spec(symbols, threshold):
    """完整策略身分：目標、特徵、成本、門檻、母體與排序契約缺一不可。"""
    return {
        "target": SHORT_PROFIT_POLICY,
        "targetPolicyHash": SHORT_PROFIT_POLICY_HASH,
        "featureNames": list(FEATURE_NAMES),
        "threshold": round(float(threshold), 8),
        "symbols": sorted({str(symbol) for symbol in (symbols or []) if str(symbol)}),
        "modelType": "ensemble-logistic-xgboost-lightgbm-isolation-rank",
        "selectionGate": {
            "expectedNetReturn": ">0",
            "riskAdjustedExpectedReturn": ">0",
            "allHorizonModelsRequired": True,
            "monsterSetupRequired": False,
        },
        "scoreWeights": {
            "winModels": 0.44,
            "learningToRank": 0.34,
            "isolationForest": 0.14,
            "setupAdjustment": 0.08,
        },
    }


def short_profit_policy_hash(symbols, threshold):
    spec = short_profit_policy_spec(symbols, threshold)
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]

MARKET_SOURCES = [
    ("TAIEX", "^TWII", "加權指數 TAIEX"),
    # OTC 櫃買指數：Yahoo ^TWOII 已失效(.TW/.TWO 皆 HTTP 404)，改由 TPEx 官方
    # OpenAPI tpex_index 抓取(update_market_data 內對 market_key=="OTC" 特判)。
    ("OTC", "TPEX_INDEX", "櫃買指數 OTC"),
    ("NASDAQ", "^IXIC", "NASDAQ"),
    ("SP500", "^GSPC", "S&P 500"),
    ("USDTWD", "TWD=X", "USD/TWD"),
]

# ===== 重訓品質閘門 =====
# train_model 原本無條件 os.replace 覆蓋 model.pkl：FinMind 額度用盡/價格
# 斷更時樣本可能靜默縮到幾百筆、AUC 崩到 0.5，壞模型照樣上線，隔天所有
# 雷達訊號都是退化品質且使用者不可見。覆蓋前先跟舊模型比對，太差就沿用舊版。
MODEL_GATE_MIN_AUC = 0.55
MODEL_GATE_MAX_AUC_DROP = 0.10
# 2026-07-04 稽核發現：只用絕對差值判斷AUC跌幅，對高基準模型寬鬆、對本來就
# 貼近及格邊緣的模型仍是同一把尺——舊AUC 0.65→新0.56(絕對差0.09通過)相對
# 降幅卻達13.8%，新模型已經很弱(只比隨機猜測0.5高一點)卻被判定合格。加一條
# 相對降幅檢查，跟絕對值用OR方式擋，兩者任一觸發就拒絕。
MODEL_GATE_MAX_AUC_RELATIVE_DROP = 0.15
MODEL_GATE_MAX_SAMPLE_SHRINK = 0.30
LEGACY_HORIZON_REVERT_META_KEY = "legacy_manual_horizon_revert_v1"


def evaluate_model_gate(old_model, new_metrics, new_samples, new_feature_names, consecutive_rejects=0):
    """純函式：回傳 (accept, reason, forced)。

    consecutive_rejects 僅保留呼叫相容性與稽核資訊，不得作為放行理由。品質
    閘門若因 AUC 或樣本覆蓋失敗，無論連續拒絕幾次都必須保留舊模型；否則資料
    源持續縮水時，系統反而會在第 N 次自動覆蓋成已知較差的模型。

    特徵 schema 變更時必須直接接受——舊 model.pkl 在新程式碼下 load_model
    會拒載，擋下新模型會讓整個系統無模型可用(掃描/預測全滅)，比上一個
    品質稍差的模型嚴重得多。"""
    if not old_model:
        return True, "無舊模型可比較，直接上線", False
    old_features = list(old_model.get("feature_names") or [])
    if old_features != list(new_feature_names):
        return True, "特徵schema已變更，舊模型無法在新程式碼下服役，直接上線", False
    new_auc = float((new_metrics or {}).get("auc") or 0)
    old_auc = float((old_model.get("metrics") or {}).get("auc") or 0)
    old_samples = int(old_model.get("samples") or 0)
    if new_auc < MODEL_GATE_MIN_AUC:
        return False, f"新模型AUC {new_auc:.3f} 低於下限 {MODEL_GATE_MIN_AUC:.2f}", False
    if old_auc and old_auc - new_auc > MODEL_GATE_MAX_AUC_DROP:
        return False, f"AUC 從 {old_auc:.3f} 掉到 {new_auc:.3f}，超過允許跌幅 {MODEL_GATE_MAX_AUC_DROP:.2f}", False
    if old_auc and new_auc < old_auc * (1 - MODEL_GATE_MAX_AUC_RELATIVE_DROP):
        relative_drop = 1 - new_auc / old_auc
        return False, (
            f"AUC 從 {old_auc:.3f} 掉到 {new_auc:.3f}，相對降幅 {relative_drop:.1%} "
            f"超過允許值 {MODEL_GATE_MAX_AUC_RELATIVE_DROP:.0%}"
        ), False
    if old_samples and new_samples < old_samples * (1 - MODEL_GATE_MAX_SAMPLE_SHRINK):
        return False, f"訓練樣本從 {old_samples} 縮水到 {new_samples}，超過允許縮幅 {MODEL_GATE_MAX_SAMPLE_SHRINK:.0%}", False
    return True, "通過品質閘門", False


def read_meta_int(meta, key, fallback=0):
    """從 meta dict 讀整數，讀不到才用 fallback。fallback 可以是 callable——
    昂貴的計算(例如 liquid_monster_universe() 要 ~2.6 秒的全市場流動性掃描)
    必須包成 lambda 傳進來惰性求值。2026-07-03 事故：list_monster_scores 把
    len(self.liquid_monster_universe()) 當一般參數傳，Python eager 評估讓每個
    /api/monster-scores 請求都白跑一次(端點 3 秒)，整個電腦版介面跟著卡。"""
    raw = meta.get(key)
    if raw is not None and str(raw).strip() != "":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            pass
    value = fallback() if callable(fallback) else fallback
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def today_key():
    # 明確用台北時區：掃描日期(scan_date)、策略訊號日期等業務日期不能跟著
    # 伺服器系統時區跑，跟 server.py 的 taipei_localtime 同一套原則。
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    except Exception:
        return dt.date.today().isoformat()


def now_text():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_finmind_token():
    import os

    token = os.environ.get("FINMIND_API_TOKEN") or os.environ.get("FINMIND_TOKEN")
    if token:
        return token.strip()
    token_file = ROOT / "finmind_token.txt"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def sigmoid(value):
    return 1 / (1 + math.exp(-max(-35, min(35, value))))


def is_official_source(source):
    text = str(source or "").strip().lower()
    if not text:
        return False
    if any(keyword in text for keyword in NON_OFFICIAL_SOURCE_KEYWORDS):
        return False
    return any(keyword in text for keyword in OFFICIAL_SOURCE_KEYWORDS)


def clamp(value, low, high):
    return max(low, min(high, value))


# market_data_quality 判斷大盤資料(TAIEX/OTC/USDTWD等)夠不夠新時，允許的最大
# 「有資料的最近一天」跟「查詢日期」之間的日曆天數差距。這裡刻意設得比一般
# 週末(3天)寬鬆不少，用來容忍長假(連續假期、美股與台股假日不對齊造成的
# 報價時間差)，但足以抓出「某個來源連續好幾天抓不到資料」這種真正的異常。
MARKET_DATA_MAX_STALE_DAYS = 6


def calendar_days_between(date_a, date_b):
    try:
        a = dt.date.fromisoformat(str(date_a)[:10])
        b = dt.date.fromisoformat(str(date_b)[:10])
    except (TypeError, ValueError):
        return 0
    return abs((b - a).days)


def smooth_clamp_ratio(ratio, low=0.01, high=0.99, tail=0.2):
    """跟 clamp(ratio, low, high) 在 [low, high] 範圍內行為完全一致(恆等映射，
    不引入任何偏移)；超出範圍時改用指數衰減平滑趨近邊界，而不是像 clamp()
    那樣直接截斷成同一個值 —— 讓「稍微異常」跟「極度異常」的樣本還能分出
    差異，不會全部卡在同一個地板/天花板值上，喪失鑑別度。"""
    if ratio < low:
        return low * math.exp((ratio - low) / tail)
    if ratio > high:
        return high + (1 - high) * (1 - math.exp(-(ratio - high) / tail))
    return ratio


def clamp_open_to_bar(open_v, low_v, high_v):
    """開盤價依定義必落在當日 [low, high] 區間內(low 是含開盤在內的當日最低、
    high 是最高)。FinMind TaiwanStockPrice 偶爾回傳 open 越界的髒列(實測 2026
    年 4-7 月有 2061 筆 open>high 或 open<low，且 high/low/close 都對得上 Yahoo、
    唯獨 open 壞)，會污染開盤跳空/當日振幅類特徵。當 low/high 本身合理(>0 且
    low<=high)時，把越界的 open 夾回 [low, high];否則(壞 bar)原樣返回不干預，
    交給既有的 min(o,h,l,c)<=0 Yahoo 補值護欄處理。in-range 的 open 不受影響。"""
    if open_v is None or low_v is None or high_v is None:
        return open_v
    if 0 < low_v <= high_v:
        return min(max(open_v, low_v), high_v)
    return open_v


def price_scale_is_plausible(candidate_close, reference_close, low_ratio=0.5, high_ratio=2.0):
    """比較替代收盤價跟最近一筆已知正常收盤價的比例，篩掉尺度不一致的
    Yahoo Finance 補值。Yahoo 的歷史股價會因為除權息/分割回溯調整，跟
    FinMind 的原始(未還原)股價不是同一個尺度基準；只補「FinMind 當天壞掉
    的單一天」卻混進尺度不一致的 Yahoo 值，會在原始價格序列裡憑空造成一次
    跟前後完全不連續的假跳空。沒有參考值可比對時視為可接受。"""
    if not reference_close or reference_close <= 0:
        return True
    if not candidate_close or candidate_close <= 0:
        return False
    ratio = candidate_close / reference_close
    return low_ratio <= ratio <= high_ratio


# 套件版本在一個 server run 內不會變(要換版必重啟)，但 importlib.metadata.version
# 每次都會去 email-parse 套件 metadata(~2ms/套件)。原本每檔 brain 判斷都經 predict_symbol
# → load_model_with_error → compare_model_environment → current_model_environment 重查一輪
# (6 個套件),盤中 100+ 檔候選就是上百次無謂的 metadata 解析(profile 實測佔每檔 ~0.04s)。
# memoize 掉:同一 run 內每個套件只查一次。換套件版本會重啟 server,快取自然重建。
_PACKAGE_VERSION_CACHE = {}


def package_version(distribution):
    if importlib_metadata is None:
        return ""
    if distribution in _PACKAGE_VERSION_CACHE:
        return _PACKAGE_VERSION_CACHE[distribution]
    try:
        value = importlib_metadata.version(distribution)
    except Exception:
        value = ""
    _PACKAGE_VERSION_CACHE[distribution] = value
    return value


def current_model_environment():
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_full": sys.version.split()[0],
        "python_executable": sys.executable,
        "packages": {
            name: package_version(name)
            for name in REQUIRED_MODEL_PACKAGES
        },
        "required_packages": dict(REQUIRED_MODEL_PACKAGES),
    }


def compare_model_environment(model_env):
    current = current_model_environment()
    if not model_env:
        return {"ok": True, "issues": [], "current": current}
    if model_env.get("error"):
        # model_env.json 存在但讀取/解析失敗時，read_model_env() 回傳的是
        # {"error": ...} 佔位物件，不是真正的環境紀錄——沒有 "python"/
        # "packages" 欄位可以比對，下面的迴圈天生不會產生任何 issues，會被
        # 誤判成「環境吻合」。但實際上我們對這個模型是在哪個環境訓練的
        # 一無所知，這正是版本比對閘門本來要擋的情境，比對不出來就該當作
        # 可能不吻合，比默默放行安全（呼叫端 load_model() 會因此拒絕載入
        # 模型，逼人去修 model_env.json 而不是帶著版本風險繼續跑）。
        return {"ok": False, "issues": [f"model_env.json 無法讀取：{model_env['error']}"], "current": current}
    issues = []
    expected_python = str(model_env.get("python") or "").strip()
    if expected_python and expected_python != current["python"]:
        issues.append(f"python expected {expected_python}, current {current['python']}")
    expected_packages = model_env.get("packages") or {}
    for name, expected_version in expected_packages.items():
        expected = str(expected_version or "").strip()
        current_version = str(current["packages"].get(name) or "").strip()
        if expected and current_version != expected:
            issues.append(f"{name} expected {expected}, current {current_version or 'missing'}")
    return {"ok": not issues, "issues": issues, "current": current}


def is_etf_like_stock(symbol="", name="", sector="", market_type=""):
    symbol = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    text = " ".join(str(value or "") for value in (name, sector, market_type)).lower()
    return symbol.startswith("00") or "etf" in text


def start_date(days):
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def add_column_if_missing(conn, table, existing_columns, name, definition):
    # init_db() 的「PRAGMA table_info 檢查欄位在不在→ALTER TABLE」是兩個
    # 分開的步驟，中間沒有互斥保護。不同 OS 行程(server.py 主行程、
    # realtime_tick_collector.py 子行程、或手動執行的 daily_update.py/
    # health_check.py/data_integrity_check.py)在同一次新增欄位的部署後
    # 幾乎同時啟動時，兩邊都會各自讀到「欄位不存在」再各自執行 ALTER
    # TABLE，後執行的那個會拋出 sqlite3.OperationalError: duplicate column
    # name——這個錯誤訊息不含 "locked"，會被 LockedConnection._run_with_retry
    # 的重試機制直接放行拋出，沒有任何外層防護，會讓 backend = StockMLBackend()
    # 這個模組層級初始化整個失敗，該行程完全無法啟動。這裡把「欄位已存在」
    # 這個結果本身視為成功(語意上就是幂等操作：不管是我們自己加的還是
    # 別的行程搶先加的，欄位存在了就達成目的)，只有其他原因的
    # OperationalError 才真的往外拋。
    if name in existing_columns:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


class _PriceRowsMemoScope:
    """build_brain_decision 期間的「請求內」prices 快取範圍。

    一次 brain 判斷會讀同一檔 prices 兩次:build_brain_decision 開頭讀一次算
    K線/量能,predict_symbol(結果快取 miss 時)的 ensure_model_ready_rows 又讀
    一次。predict 快取 TTL 只有 120 秒,使用者開畫面時幾乎必然 miss,所以第二
    次 DB 讀幾乎每次都發生。這個範圍開一個 thread-local dict 讓同一次判斷內的
    load_price_rows 命中 memo,消掉第二次 DB 讀。memo 生命週期只在一次判斷內
    (__exit__ 就清空)、不跨判斷、不跨執行緒(thread-local),所以沒有任何「讀到
    過時價格」的風險。巢狀時只有最外層負責清,避免內層提前把外層 memo 清掉。"""
    __slots__ = ("_backend", "_owns")

    def __init__(self, backend):
        self._backend = backend
        self._owns = False

    def __enter__(self):
        if getattr(self._backend._price_rows_memo, "cache", None) is None:
            self._backend._price_rows_memo.cache = {}
            self._owns = True
        return self

    def __exit__(self, *exc):
        if self._owns:
            self._backend._price_rows_memo.cache = None
        return False


class StockMLBackend:
    def __init__(self):
        self.db_path = DB_PATH
        self.model_path = MODEL_PATH
        self.model_env_path = MODEL_ENV_PATH
        self._official_latest_cache = None
        self._official_latest_cache_date = ""
        self._model_load_error = ""
        self._model_load_lock = threading.Lock()
        self._model_cache = None
        self._last_price_fetch_errors = {}
        # predict_symbol 結果快取：同一檔股票在短時間內被三個入口(個股查詢、
        # Brain Engine、妖股掃描/紙上快照)重複呼叫時，不用重跑完整特徵計算
        # +5模型推論。正確性靠四道保險：TTL、模型 trained_at 比對、
        # upsert_price_rows/update_market_data 寫入新資料時主動失效、以及
        # 2026-07-04 稽核修復新增的 generation 版本戳(_predict_cache_gen)——
        # 修掉 read-compute-then-write 的 lost-invalidation 競態：執行緒A算到一半
        # 時執行緒B寫入新日K並失效該股(此時A還沒寫快取、pop 無效)，A原本會用
        # 失效前的舊資料覆寫快取，讓過時預測殘留最長 TTL 秒。改成A寫回前比對
        # gen 是否在計算期間被動過，動過就放棄寫回。_predict_cache_lock 只保護
        # dict 讀寫與 gen，絕不跨 DB I/O(_save_prediction_row 走 DB_WRITE_LOCK，
        # 為避免 lock-ordering 死鎖，一律在鎖外呼叫)。
        self._predict_cache = {}
        self._predict_cache_lock = threading.Lock()
        self._predict_cache_gen = 0
        self._latest_complete_price_date_cache = {
            "at": 0.0,
            "minimumSymbols": 0,
            "value": "",
        }
        # 請求內 prices 快取(見 _PriceRowsMemoScope):只在單次 build_brain_decision
        # 內活著,消掉同一檔在一次判斷裡被讀兩次的 DB overhead。
        self._price_rows_memo = threading.local()
        self.init_db()

    def read_model_env(self):
        if not self.model_env_path.exists():
            return None
        try:
            return json.loads(self.model_env_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"error": str(exc)}

    def write_model_env(self, model):
        env = current_model_environment()
        env.update({
            "trained_at": model.get("trained_at") or now_text(),
            "model_version": model.get("version") or "",
            "data_policy": model.get("data_policy") or DATA_POLICY_VERSION,
            "training_data_max_date": model.get("training_data_max_date"),
            "training_sample_max_date": model.get("training_sample_max_date"),
        })
        temp_path = self.model_env_path.with_name(f"{self.model_env_path.name}.tmp")
        temp_path.write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.model_env_path)
        return env

    def connect(self):
        global DB_WAL_READY
        conn = sqlite3.connect(self.db_path, timeout=60, factory=LockedConnection)
        conn.execute("PRAGMA busy_timeout = 60000")
        if not DB_WAL_READY:
            with DB_WRITE_LOCK:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                DB_WAL_READY = True
        return conn

    def init_db(self):
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prices (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    foreign_buy_sell REAL,
                    trust_buy_sell REAL,
                    margin_balance REAL,
                    short_balance REAL,
                    monthly_revenue REAL,
                    revenue_growth REAL,
                    per REAL,
                    pbr REAL,
                    dividend_yield REAL,
                    day_trade_ratio REAL,
                    day_trade_buy_sell_imbalance REAL,
                    securities_lending_volume REAL,
                    securities_lending_fee_rate REAL,
                    large_investor_buy_sell REAL,
                    retail_investor_buy_sell REAL,
                    broker_branch_net_buy REAL,
                    main_force_buy_sell REAL,
                    realtime_money_flow REAL,
                    realtime_large_order_flow REAL,
                    gross_margin REAL,
                    operating_margin REAL,
                    roe REAL,
                    debt_ratio REAL,
                    operating_cashflow_ratio REAL,
                    financial_statement_source TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, date)
                )
            """)
            price_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(prices)").fetchall()
            }
            for name, definition in {
                "per": "REAL",
                "pbr": "REAL",
                "dividend_yield": "REAL",
                "day_trade_ratio": "REAL",
                "day_trade_buy_sell_imbalance": "REAL",
                "securities_lending_volume": "REAL",
                "securities_lending_fee_rate": "REAL",
                "large_investor_buy_sell": "REAL",
                "retail_investor_buy_sell": "REAL",
                "broker_branch_net_buy": "REAL",
                "main_force_buy_sell": "REAL",
                "realtime_money_flow": "REAL",
                "realtime_large_order_flow": "REAL",
                "gross_margin": "REAL",
                "operating_margin": "REAL",
                "roe": "REAL",
                "debt_ratio": "REAL",
                "operating_cashflow_ratio": "REAL",
                "price_source": "TEXT",
                "chip_source": "TEXT",
                "margin_source": "TEXT",
                "ownership_flow_source": "TEXT",
                "branch_flow_source": "TEXT",
                "realtime_flow_source": "TEXT",
                "retail_flow_source": "TEXT",
                "revenue_source": "TEXT",
                "valuation_source": "TEXT",
                "financial_statement_source": "TEXT",
                "finance_source": "TEXT",
            }.items():
                add_column_if_missing(conn, "prices", price_columns, name, definition)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_symbol_inactive_periods (
                    symbol TEXT NOT NULL,
                    inactive_from TEXT NOT NULL,
                    inactive_to TEXT,
                    status TEXT NOT NULL,
                    name TEXT,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, inactive_from)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_symbol_inactive_lookup
                ON market_symbol_inactive_periods(symbol, inactive_from, inactive_to)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS official_active_symbols (
                    symbol TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    name TEXT,
                    listing_date TEXT,
                    evidence_date TEXT,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                )
            """)
            for symbol, item in KNOWN_INACTIVE_PERIODS.items():
                conn.execute("""
                    INSERT INTO market_symbol_inactive_periods (
                        symbol, inactive_from, inactive_to, status, name,
                        source, source_url, observed_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, inactive_from) DO UPDATE SET
                        status = excluded.status,
                        name = COALESCE(excluded.name, market_symbol_inactive_periods.name),
                        source = excluded.source,
                        source_url = excluded.source_url
                """, (
                    symbol,
                    item["from"],
                    item["status"],
                    item.get("name"),
                    "TWSE official announcement seed",
                    TWSE_DELISTED_URL,
                    now_text(),
                ))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS data_cleanup_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cleanup_key TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    row_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT,
                    payload_json TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    UNIQUE(cleanup_key, table_name, row_key)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_data_cleanup_audit_key
                ON data_cleanup_audit(cleanup_key, table_name, archived_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS data_cleanup_restore_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_audit_id INTEGER NOT NULL UNIQUE,
                    cleanup_key TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    restored_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    active_evidence_date TEXT,
                    active_evidence_source TEXT,
                    FOREIGN KEY (original_audit_id) REFERENCES data_cleanup_audit(id)
                )
            """)
            try:
                conn.execute("""
                    UPDATE prices
                    SET financial_statement_source = 'FinMind financial statements'
                    WHERE gross_margin IS NOT NULL
                      AND (financial_statement_source IS NULL OR financial_statement_source = '')
                """)
                conn.execute("""
                    UPDATE prices
                    SET finance_source = TRIM(
                        COALESCE(finance_source, '') ||
                        CASE
                            WHEN finance_source IS NOT NULL AND finance_source != ''
                             AND financial_statement_source IS NOT NULL AND financial_statement_source != ''
                             AND finance_source NOT LIKE '%' || financial_statement_source || '%'
                            THEN ' | '
                            ELSE ''
                        END ||
                        CASE
                            WHEN financial_statement_source IS NOT NULL AND financial_statement_source != ''
                             AND (
                                finance_source IS NULL OR finance_source = ''
                                OR finance_source NOT LIKE '%' || financial_statement_source || '%'
                             )
                            THEN financial_statement_source
                            ELSE ''
                        END
                    )
                    WHERE financial_statement_source IS NOT NULL
                      AND financial_statement_source != ''
                      AND (
                        finance_source IS NULL OR finance_source = ''
                        OR finance_source NOT LIKE '%' || financial_statement_source || '%'
                      )
                """)
                conn.execute("""
                    UPDATE prices
                    SET finance_source = TRIM(
                        COALESCE(revenue_source, '') ||
                        CASE
                            WHEN revenue_source IS NOT NULL AND revenue_source != ''
                             AND valuation_source IS NOT NULL AND valuation_source != ''
                            THEN ' | '
                            ELSE ''
                        END ||
                        COALESCE(valuation_source, '') ||
                        CASE
                            WHEN (
                                (revenue_source IS NOT NULL AND revenue_source != '')
                                OR (valuation_source IS NOT NULL AND valuation_source != '')
                            )
                             AND financial_statement_source IS NOT NULL AND financial_statement_source != ''
                            THEN ' | '
                            ELSE ''
                        END ||
                        COALESCE(financial_statement_source, '')
                    )
                    WHERE (finance_source IS NULL OR finance_source = '')
                      AND (
                        (revenue_source IS NOT NULL AND revenue_source != '')
                        OR (valuation_source IS NOT NULL AND valuation_source != '')
                        OR (financial_statement_source IS NOT NULL AND financial_statement_source != '')
                      )
                """)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            conn.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price_date TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    probability REAL NOT NULL,
                    threshold REAL NOT NULL,
                    action TEXT NOT NULL,
                    target_horizon INTEGER NOT NULL,
                    target_return REAL NOT NULL,
                    target_type TEXT NOT NULL DEFAULT 'legacy-monster-window',
                    close REAL NOT NULL,
                    outcome_date TEXT,
                    outcome_close REAL,
                    outcome_return REAL,
                    hit INTEGER
                )
            """)
            prediction_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()
            }
            add_column_if_missing(
                conn, "predictions", prediction_columns, "target_type",
                "TEXT NOT NULL DEFAULT 'legacy-monster-window'",
            )
            # _save_prediction_row 原本只靠應用層「SELECT不存在才INSERT」防重複，
            # 這是多執行緒(ThreadingHTTPServer)下的TOCTOU競態——兩個執行緒都可能
            # 讀到「不存在」、都各自INSERT，讓predictions表出現同一(symbol,
            # price_date, model_version)的重複列，污染hit_rate等統計。改成資料庫
            # 層UNIQUE約束才是真正的保證。建立索引前先清掉既有重複列(只留id最小
            # 的那筆)，否則若正式資料庫已經有重複資料(這個系統運作這段期間確實
            # 已經出現過)，CREATE UNIQUE INDEX會直接失敗讓整個init_db()掛掉。
            prediction_indexes = {
                str(row[1]) for row in conn.execute("PRAGMA index_list(predictions)").fetchall()
            }
            if "idx_predictions_unique" not in prediction_indexes:
                conn.execute("""
                    DELETE FROM predictions
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM predictions
                        GROUP BY symbol, price_date, model_version
                    )
                """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_unique
                ON predictions(symbol, price_date, model_version)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_predictions_signal_sync
                ON predictions(target_type, action, price_date, symbol, created_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS brain_v2_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price_date TEXT NOT NULL,
                    context TEXT NOT NULL,
                    engine_version TEXT,
                    v2_score REAL,
                    entry_threshold REAL,
                    data_confidence_threshold REAL,
                    entry_allowed INTEGER,
                    formal_model_score REAL,
                    kline_score REAL,
                    volume_score REAL,
                    market_score REAL,
                    chip_money_score REAL,
                    fundamental_score REAL,
                    strategy_backtest_score REAL,
                    tradingview_score REAL,
                    risk_score REAL,
                    data_confidence_score REAL,
                    required_component_failures TEXT,
                    close REAL,
                    soft_gate_score REAL,
                    soft_gate_penalty REAL,
                    soft_gate_entry_allowed INTEGER,
                    UNIQUE(symbol, price_date, context)
                )
            """)
            snapshot_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(brain_v2_snapshots)").fetchall()
            }
            for name, definition in {
                "soft_gate_score": "REAL",
                "soft_gate_penalty": "REAL",
                "soft_gate_entry_allowed": "INTEGER",
                # 妖股短線規則引擎(monster_rule_engine)原本另外存一張
                # monster_rule_snapshots，跟這裡的 soft_gate 是同一件事(先收集
                # 資料、不影響既有判斷、等累積命中率再決定要不要拿來當降級煞車)，
                # 只是分散在兩張表各自維護。合併進同一張快照表，同一個
                # (symbol, price_date, context) 才能一次查到兩套實驗性訊號。
                "rule_action": "TEXT",
                "rule_vetoed": "INTEGER",
                "rule_veto_reason": "TEXT",
                "rule_overheated": "INTEGER",
                "rule_rules": "TEXT",
                "rule_bonus_tags": "TEXT",
            }.items():
                add_column_if_missing(conn, "brain_v2_snapshots", snapshot_columns, name, definition)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    buy_at TEXT,
                    parent_trade_id INTEGER,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    signal TEXT,
                    stop_price REAL,
                    target_price REAL,
                    status TEXT NOT NULL DEFAULT 'paper',
                    exit_price REAL,
                    exit_at TEXT,
                    pnl REAL,
                    note TEXT,
                    broker_order_id TEXT,
                    broker_seqno TEXT,
                    broker_ordno TEXT,
                    filled_shares INTEGER,
                    filled_at TEXT,
                    strategy_horizon TEXT NOT NULL DEFAULT 'unknown',
                    strategy_horizon_source TEXT,
                    strategy_horizon_locked_at TEXT,
                    entry_cost_includes_buy_fee INTEGER NOT NULL DEFAULT 0,
                    broker_cost_amount REAL,
                    source_lot_key TEXT,
                    execution_price REAL,
                    broker_dseq TEXT,
                    trade_condition TEXT,
                    execution_evidence_source TEXT,
                    pnl_pct REAL,
                    pnl_basis TEXT,
                    realized_pnl_key TEXT
                )
            """)
            trade_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
            }
            for name, definition in {
                "buy_at": "TEXT",
                "parent_trade_id": "INTEGER",
                "broker_order_id": "TEXT",
                "broker_seqno": "TEXT",
                "broker_ordno": "TEXT",
                "filled_shares": "INTEGER",
                "filled_at": "TEXT",
                "strategy_horizon": "TEXT NOT NULL DEFAULT 'unknown'",
                "strategy_horizon_source": "TEXT",
                "strategy_horizon_locked_at": "TEXT",
                "entry_cost_includes_buy_fee": "INTEGER NOT NULL DEFAULT 0",
                "broker_cost_amount": "REAL",
                "source_lot_key": "TEXT",
                "execution_price": "REAL",
                "broker_dseq": "TEXT",
                "trade_condition": "TEXT",
                "execution_evidence_source": "TEXT",
                "pnl_pct": "REAL",
                "pnl_basis": "TEXT",
                "realized_pnl_key": "TEXT",
            }.items():
                add_column_if_missing(conn, "trades", trade_columns, name, definition)
            conn.execute("""
                UPDATE trades
                SET strategy_horizon = 'unknown'
                WHERE strategy_horizon IS NULL OR TRIM(strategy_horizon) = ''
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_open_buy_fifo
                ON trades(symbol, side, status, exit_at, filled_at, buy_at, id)
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_source_lot_key
                ON trades(source_lot_key)
                WHERE source_lot_key IS NOT NULL AND source_lot_key != ''
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_broker_dseq
                ON trades(symbol, side, broker_dseq)
                WHERE broker_dseq IS NOT NULL AND broker_dseq != ''
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_execution_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_key TEXT NOT NULL UNIQUE,
                    trade_id INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    deal_at TEXT NOT NULL,
                    execution_price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    broker_dseq TEXT NOT NULL,
                    trade_condition TEXT,
                    evidence_scope TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    FOREIGN KEY (trade_id) REFERENCES trades(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_execution_evidence_trade
                ON trade_execution_evidence(trade_id, deal_at, id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_execution_evidence_lookup
                ON trade_execution_evidence(symbol, side, broker_dseq, deal_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS legacy_lot_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imported_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_shares INTEGER NOT NULL,
                    migratable_shares INTEGER NOT NULL,
                    broker_average_price REAL,
                    replaced_trade_ids_json TEXT NOT NULL DEFAULT '[]',
                    replaced_trades_json TEXT NOT NULL DEFAULT '[]',
                    imported_trade_ids_json TEXT NOT NULL DEFAULT '[]',
                    lots_json TEXT NOT NULL,
                    cost_variance REAL,
                    note TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_legacy_lot_imports_symbol
                ON legacy_lot_imports(symbol, imported_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_session_validations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_session_validations_date
                ON market_session_validations(session_date, stage, id DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_session_acceptance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT NOT NULL,
                    source_validation_id INTEGER NOT NULL,
                    finalized_at TEXT NOT NULL,
                    full_day_ready INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(session_date, source_validation_id),
                    FOREIGN KEY (source_validation_id) REFERENCES market_session_validations(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_session_acceptance_date
                ON market_session_acceptance_history(session_date DESC, id DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stability_observation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_key TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,
                    start_session_date TEXT NOT NULL,
                    target_consecutive_sessions INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    completed_at TEXT,
                    last_evaluated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stability_observation_days (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    session_date TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    blockers_json TEXT NOT NULL,
                    acceptance_json TEXT NOT NULL,
                    UNIQUE(run_id, session_date),
                    FOREIGN KEY (run_id) REFERENCES stability_observation_runs(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stability_observation_days_run
                ON stability_observation_days(run_id, session_date)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_horizon_evidence_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audited_at TEXT NOT NULL,
                    apply_mode TEXT NOT NULL,
                    scanned_lots INTEGER NOT NULL,
                    evidence_lots INTEGER NOT NULL,
                    classified_lots INTEGER NOT NULL,
                    updated_lots INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_horizon_lock_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    assignment_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radar_deployment_readiness (
                    readiness_date TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    eligible_settled INTEGER NOT NULL,
                    target_hit_rate REAL,
                    avg_net_return REAL,
                    profit_factor REAL,
                    live_pass INTEGER NOT NULL,
                    walk_forward_pass INTEGER NOT NULL,
                    consecutive_pass_days INTEGER NOT NULL,
                    enforced INTEGER NOT NULL,
                    formal_ready INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radar_strategy_experiment_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_date TEXT NOT NULL UNIQUE,
                    generated_at TEXT NOT NULL,
                    lookback_days INTEGER NOT NULL,
                    live_settled INTEGER NOT NULL DEFAULT 0,
                    proxy_settled INTEGER NOT NULL DEFAULT 0,
                    qualified_count INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_exit_snapshots (
                    symbol TEXT PRIMARY KEY,
                    decision_date TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    strategy_horizon TEXT,
                    decision_type TEXT,
                    decision_verified INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_exit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    decision_date TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    strategy_horizon TEXT,
                    decision_type TEXT,
                    decision_verified INTEGER NOT NULL DEFAULT 0,
                    trade_id INTEGER,
                    buy_date TEXT,
                    signal_price REAL,
                    shares INTEGER NOT NULL DEFAULT 0,
                    sell_shares INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            portfolio_exit_history_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(portfolio_exit_history)"
                ).fetchall()
            }
            for name, definition in {
                "invalid_for_trading": "INTEGER NOT NULL DEFAULT 0",
                "invalid_reason": "TEXT",
            }.items():
                add_column_if_missing(
                    conn,
                    "portfolio_exit_history",
                    portfolio_exit_history_columns,
                    name,
                    definition,
                )
            conn.execute("""
                UPDATE portfolio_exit_history
                SET invalid_for_trading = 1,
                    invalid_reason = 'broker_volume_lots_compared_with_daily_shares'
                WHERE decision_date = '2026-07-13'
                  AND decision_type = 'phase2'
                  AND decision_verified = 1
                  AND COALESCE(invalid_for_trading, 0) = 0
                  AND payload_json LIKE '%Shioaji%'
                  AND json_extract(payload_json, '$.volumeRatio') IS NOT NULL
                  AND CAST(json_extract(payload_json, '$.volumeRatio') AS REAL) < 0.01
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_portfolio_exit_history_date
                ON portfolio_exit_history(decision_date DESC, symbol, decision_verified)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_exit_outcomes (
                    history_id INTEGER NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    outcome_date TEXT NOT NULL,
                    future_close REAL NOT NULL,
                    price_source TEXT,
                    future_return_pct REAL,
                    decision_net_pnl REAL,
                    decision_net_pct REAL,
                    correct INTEGER NOT NULL DEFAULT 0,
                    premature_sell INTEGER NOT NULL DEFAULT 0,
                    settled_at TEXT NOT NULL,
                    PRIMARY KEY (history_id, horizon_days),
                    FOREIGN KEY (history_id) REFERENCES portfolio_exit_history(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_portfolio_exit_outcomes_horizon
                ON portfolio_exit_outcomes(horizon_days, correct)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sinopac_order_fills (
                    dedup_key TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    action TEXT,
                    price REAL,
                    shares INTEGER,
                    deal_at TEXT,
                    broker_order_id TEXT,
                    broker_seqno TEXT,
                    broker_ordno TEXT,
                    source TEXT,
                    raw_json TEXT,
                    imported_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_prices (
                    market_key TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL NOT NULL,
                    volume REAL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market_key, date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_info (
                    symbol TEXT PRIMARY KEY,
                    name TEXT,
                    sector TEXT,
                    market_type TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS monster_scores (
                    scan_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price_date TEXT NOT NULL,
                    score REAL NOT NULL,
                    -- 2026-07-08 掃描去模型:掃描改純型態量能、不跑 predict_symbol,
                    -- 沒有模型機率可存,故 probability/threshold 放寬成可空(僅供參考、
                    -- 不決定任何買賣)。舊 DB 由下方 migration 就地重建放寬。
                    probability REAL,
                    threshold REAL,
                    action TEXT NOT NULL,
                    buy_allowed INTEGER NOT NULL,
                    recorded_buy_allowed INTEGER,
                    invalid_for_trading INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    close REAL NOT NULL,
                    buy_trigger REAL,
                    pullback_price REAL,
                    stop_price REAL,
                    take_profit REAL,
                    trailing_stop REAL,
                    gap_limit REAL NOT NULL,
                    change1 REAL,
                    change5 REAL,
                    change20 REAL,
                    volume_ratio REAL,
                    latest_volume_lots REAL,
                    avg_volume20_lots REAL,
                    turnover_million REAL,
                    liquidity_ok INTEGER NOT NULL DEFAULT 1,
                    surge_setup INTEGER NOT NULL DEFAULT 0,
                    counter_trend_strength INTEGER NOT NULL DEFAULT 0,
                    market_regime TEXT,
                    regime_threshold REAL,
                    theme_heat REAL,
                    sector_theme_streak INTEGER NOT NULL DEFAULT 0,
                    theme_snapshot TEXT,
                    reasons TEXT NOT NULL,
                    model_version TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scan_date, symbol)
                )
            """)
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(monster_scores)").fetchall()
            }
            for name, definition in {
                "change1": "REAL",
                "change5": "REAL",
                "change20": "REAL",
                "volume_ratio": "REAL",
                "latest_volume_lots": "REAL",
                "avg_volume20_lots": "REAL",
                "turnover_million": "REAL",
                "liquidity_ok": "INTEGER NOT NULL DEFAULT 1",
                "surge_setup": "INTEGER NOT NULL DEFAULT 0",
                "counter_trend_strength": "INTEGER NOT NULL DEFAULT 0",
                "sector_excess_ret5": "REAL",
                "market_regime": "TEXT",
                "regime_threshold": "REAL",
                "theme_heat": "REAL",
                "sector_theme_streak": "INTEGER NOT NULL DEFAULT 0",
                "theme_snapshot": "TEXT",
                "overheated": "INTEGER NOT NULL DEFAULT 0",
                "risk_flags": "TEXT",
                "recorded_buy_allowed": "INTEGER",
                "invalid_for_trading": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                add_column_if_missing(conn, "monster_scores", existing_columns, name, definition)
            # 2026-07-08 掃描去模型:把舊 DB 的 monster_scores.probability/threshold 從
            # NOT NULL 放寬成可空(掃描不跑模型時無機率)。SQLite 不能 ALTER 掉 NOT NULL,
            # 只能重建;取 live schema 做字串替換再重建,避免手抄欄位漏掉先前 ALTER 加的
            # 欄位。monster_scores 是小表、每次掃描 DELETE+重寫,重建安全。只有偵測到
            # 仍為 NOT NULL 才做,idempotent。
            monster_info = {r[1]: r for r in conn.execute("PRAGMA table_info(monster_scores)").fetchall()}
            prob_col = monster_info.get("probability")
            if prob_col is not None and prob_col[3] == 1:  # notnull 旗標==1
                create_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='monster_scores'"
                ).fetchone()[0]
                rebuild_sql = (
                    create_sql
                    .replace("probability REAL NOT NULL", "probability REAL")
                    .replace("threshold REAL NOT NULL", "threshold REAL")
                    .replace("CREATE TABLE IF NOT EXISTS monster_scores", "CREATE TABLE monster_scores_rebuild")
                    .replace("CREATE TABLE monster_scores", "CREATE TABLE monster_scores_rebuild")
                )
                # fail-safe:字串替換若沒真的把兩個 NOT NULL 拿掉、或沒改到表名(未來有人動了
                # CREATE TABLE 的空白/欄位順序就會發生),絕不重建——否則會重建出一模一樣的
                # NOT NULL 表,每次啟動重跑、且之後存 None 仍會炸。寧可跳過(下次真的對齊再處理)。
                stripped = ("probability REAL NOT NULL" not in rebuild_sql
                            and "threshold REAL NOT NULL" not in rebuild_sql)
                renamed = "monster_scores_rebuild" in rebuild_sql
                if stripped and renamed:
                    conn.execute("DROP TABLE IF EXISTS monster_scores_rebuild")  # 清掉前次半途殘留
                    conn.execute(rebuild_sql)
                    conn.execute("INSERT INTO monster_scores_rebuild SELECT * FROM monster_scores")
                    conn.execute("DROP TABLE monster_scores")
                    conn.execute("ALTER TABLE monster_scores_rebuild RENAME TO monster_scores")
                else:
                    print("monster_scores migration 跳過:CREATE TABLE 格式與預期不符,字串替換無效,"
                          "不重建以免產生一模一樣的 NOT NULL 表")
            # 休市掃描保留全部分數/原因與原始買進判斷供稽核，但正式
            # buy_allowed 必須在資料庫層就歸零，不能只靠 API 臨時遮罩。
            closed_scan_dates = {
                str(row[0]) for row in conn.execute("""
                    SELECT DISTINCT scan_date
                    FROM monster_scores
                    WHERE strftime('%w', scan_date) IN ('0', '6')
                """).fetchall()
            }
            try:
                overrides = load_market_session_overrides(MARKET_SESSION_OVERRIDES_PATH)
                closed_scan_dates.update(
                    str(date_text) for date_text, status in overrides.items()
                    if isinstance(status, dict)
                    and status.get("known") is True
                    and status.get("isTradingDay") is False
                )
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            for date_text in sorted(closed_scan_dates):
                conn.execute("""
                    UPDATE monster_scores
                    SET recorded_buy_allowed = COALESCE(recorded_buy_allowed, buy_allowed),
                        buy_allowed = 0,
                        invalid_for_trading = 1
                    WHERE scan_date = ?
                """, (date_text,))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radar_sector_history (
                    scan_date TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    turnover_share REAL NOT NULL,
                    avg_ret5 REAL,
                    avg_ret20 REAL,
                    excess_ret5 REAL,
                    excess_ret20 REAL,
                    avg_volume_ratio REAL,
                    hot INTEGER NOT NULL DEFAULT 0,
                    streak_days INTEGER NOT NULL DEFAULT 0,
                    theme_heat REAL NOT NULL DEFAULT 0,
                    snapshot TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scan_date, sector)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS radar_entry_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    scan_date TEXT NOT NULL,
                    price_date TEXT,
                    score REAL,
                    setup_type TEXT,
                    entry_at TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_mode TEXT NOT NULL,
                    quote_source TEXT,
                    quote_age_seconds REAL,
                    bid_price REAL,
                    ask_price REAL,
                    spread_pct REAL,
                    estimated_slippage_pct REAL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scan_date, symbol)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_radar_entry_snapshots_signal_date
                ON radar_entry_snapshots(signal_date, symbol)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_signals (
                    signal_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_session TEXT,
                    signal_session_label TEXT,
                    signal_time TEXT,
                    name TEXT,
                    decision TEXT,
                    score REAL,
                    model_version TEXT,
                    price REAL,
                    buy_point REAL,
                    stop_price REAL,
                    target_price REAL,
                    trade_horizon TEXT,
                    trade_horizon_label TEXT,
                    trade_horizon_days TEXT,
                    trade_horizon_score REAL,
                    data_date TEXT,
                    data_source TEXT,
                    decision_source TEXT,
                    evidence_json TEXT,
                    return_1d REAL,
                    return_3d REAL,
                    return_5d REAL,
                    return_10d REAL,
                    return_20d REAL,
                    return_60d REAL,
                    hit_1d INTEGER,
                    hit_3d INTEGER,
                    hit_5d INTEGER,
                    hit_10d INTEGER,
                    hit_20d INTEGER,
                    hit_60d INTEGER,
                    max_drawdown_10d REAL,
                    stopped_first INTEGER,
                    outcome_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (signal_date, strategy, side, symbol)
                )
            """)
            strategy_signal_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(strategy_signals)").fetchall()
            }
            for name, definition in {
                "signal_session": "TEXT",
                "signal_session_label": "TEXT",
                "signal_time": "TEXT",
                "name": "TEXT",
                "decision": "TEXT",
                "score": "REAL",
                "model_version": "TEXT",
                "price": "REAL",
                "buy_point": "REAL",
                "stop_price": "REAL",
                "target_price": "REAL",
                "trade_horizon": "TEXT",
                "trade_horizon_label": "TEXT",
                "trade_horizon_days": "TEXT",
                "trade_horizon_score": "REAL",
                "data_date": "TEXT",
                "data_source": "TEXT",
                "decision_source": "TEXT",
                "evidence_json": "TEXT",
                "return_1d": "REAL",
                "return_3d": "REAL",
                "return_5d": "REAL",
                "return_10d": "REAL",
                "return_20d": "REAL",
                "return_60d": "REAL",
                "hit_1d": "INTEGER",
                "hit_3d": "INTEGER",
                "hit_5d": "INTEGER",
                "hit_10d": "INTEGER",
                "hit_20d": "INTEGER",
                "hit_60d": "INTEGER",
                "max_drawdown_10d": "REAL",
                "stopped_first": "INTEGER",
                "outcome_updated_at": "TEXT",
                "updated_at": "TEXT",
            }.items():
                add_column_if_missing(conn, "strategy_signals", strategy_signal_columns, name, definition)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_calibration (
                    calibration_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'observation',
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    precision_5d REAL,
                    average_return_5d REAL,
                    profit_factor_5d REAL,
                    pending_5d INTEGER NOT NULL DEFAULT 0,
                    suggested_action TEXT NOT NULL,
                    weight_multiplier REAL,
                    threshold_delta REAL,
                    reason TEXT NOT NULL,
                    observation_days INTEGER NOT NULL DEFAULT 1,
                    apply_ready INTEGER NOT NULL DEFAULT 0,
                    applied INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (calibration_date, strategy)
                )
            """)
            strategy_calibration_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(strategy_calibration)").fetchall()
            }
            for name, definition in {
                "mode": "TEXT NOT NULL DEFAULT 'observation'",
                "sample_count": "INTEGER NOT NULL DEFAULT 0",
                "precision_5d": "REAL",
                "average_return_5d": "REAL",
                "profit_factor_5d": "REAL",
                "pending_5d": "INTEGER NOT NULL DEFAULT 0",
                "suggested_action": "TEXT NOT NULL DEFAULT 'observe'",
                "weight_multiplier": "REAL",
                "threshold_delta": "REAL",
                "reason": "TEXT NOT NULL DEFAULT ''",
                "observation_days": "INTEGER NOT NULL DEFAULT 1",
                "apply_ready": "INTEGER NOT NULL DEFAULT 0",
                "applied": "INTEGER NOT NULL DEFAULT 0",
                "metrics_json": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            }.items():
                add_column_if_missing(conn, "strategy_calibration", strategy_calibration_columns, name, definition)
            # realtime_flow_staging：永豐 Shioaji 即時 tick 訂閱收集器
            # (realtime_tick_collector.py，獨立常駐子程序)寫入的盤中即時資金流/
            # 大單流向暫存表，跟 prices 主表分開，避免每日批次 update_prices 的
            # INSERT OR REPLACE 把收集器當天已經寫進去的真實資料覆蓋成 NULL。
            # merge_intraday_confirmation 會從這裡讀出來 merge 回 prices。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS realtime_flow_staging (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    realtime_money_flow REAL,
                    realtime_large_order_flow REAL,
                    tick_count INTEGER NOT NULL DEFAULT 0,
                    raw_tick_count INTEGER NOT NULL DEFAULT 0,
                    unknown_tick_count INTEGER NOT NULL DEFAULT 0,
                    total_volume_lots REAL NOT NULL DEFAULT 0,
                    last_tick_at TEXT,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, date)
                )
            """)
            realtime_flow_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(realtime_flow_staging)").fetchall()
            }
            for name, definition in {
                "raw_tick_count": "INTEGER NOT NULL DEFAULT 0",
                "unknown_tick_count": "INTEGER NOT NULL DEFAULT 0",
                "total_volume_lots": "REAL NOT NULL DEFAULT 0",
                "last_tick_at": "TEXT",
            }.items():
                add_column_if_missing(conn, "realtime_flow_staging", realtime_flow_columns, name, definition)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_volume_profile (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    cumulative_volume_lots REAL NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, date, minute)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_volume_profile_symbol_date
                ON intraday_volume_profile(symbol, date)
            """)
            # Shioaji scanner rankings are refreshed once per minute by the
            # long-lived tick collector.  This table intentionally keeps only
            # the latest row per symbol/day; durable discoveries are appended
            # to intraday_discovery_events below.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_scanner_staging (
                    trading_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    scan_at TEXT NOT NULL,
                    name TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    change_price REAL,
                    total_volume_lots REAL,
                    total_amount REAL,
                    volume_ratio REAL,
                    rank_types_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    snapshot_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trading_date, symbol)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_scanner_staging_date_scan
                ON intraday_scanner_staging(trading_date, scan_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_scanner_cycles (
                    trading_date TEXT NOT NULL,
                    scan_at TEXT NOT NULL,
                    symbol_count INTEGER NOT NULL DEFAULT 0,
                    rank_counts_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (trading_date, scan_at)
                )
            """)
            scanner_cycle_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(intraday_scanner_cycles)"
                ).fetchall()
            }
            add_column_if_missing(
                conn, "intraday_scanner_cycles", scanner_cycle_columns,
                "rank_counts_json", "TEXT NOT NULL DEFAULT '{}'",
            )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_scanner_cycles_date_time
                ON intraday_scanner_cycles(trading_date, scan_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_rotation_staging (
                    trading_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    scan_at TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    batch_index INTEGER NOT NULL,
                    batch_count INTEGER NOT NULL,
                    current_price REAL,
                    reference_price REAL,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    bid_price REAL,
                    ask_price REAL,
                    total_volume_lots REAL,
                    is_suspended INTEGER NOT NULL DEFAULT 0,
                    simtrade INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    snapshot_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trading_date, symbol)
                )
            """)
            rotation_staging_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(intraday_rotation_staging)"
                ).fetchall()
            }
            for name, definition in {
                "bid_price": "REAL",
                "ask_price": "REAL",
            }.items():
                add_column_if_missing(
                    conn, "intraday_rotation_staging",
                    rotation_staging_columns, name, definition,
                )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_rotation_staging_date_scan
                ON intraday_rotation_staging(trading_date, scan_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_rotation_cycles (
                    trading_date TEXT NOT NULL,
                    scan_at TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    batch_index INTEGER NOT NULL,
                    batch_count INTEGER NOT NULL,
                    requested_count INTEGER NOT NULL DEFAULT 0,
                    received_count INTEGER NOT NULL DEFAULT 0,
                    universe_count INTEGER NOT NULL DEFAULT 0,
                    fallback_count INTEGER NOT NULL DEFAULT 0,
                    requested_symbols_json TEXT NOT NULL DEFAULT '[]',
                    rotation_symbols_json TEXT NOT NULL DEFAULT '[]',
                    missing_symbols_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (trading_date, scan_at, batch_index)
                )
            """)
            rotation_cycle_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(intraday_rotation_cycles)"
                ).fetchall()
            }
            for name, definition in {
                "universe_count": "INTEGER NOT NULL DEFAULT 0",
                "fallback_count": "INTEGER NOT NULL DEFAULT 0",
                "requested_symbols_json": "TEXT NOT NULL DEFAULT '[]'",
                "rotation_symbols_json": "TEXT NOT NULL DEFAULT '[]'",
                "missing_symbols_json": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                add_column_if_missing(
                    conn, "intraday_rotation_cycles",
                    rotation_cycle_columns, name, definition,
                )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_rotation_cycles_date_round
                ON intraday_rotation_cycles(trading_date, round_id, batch_index)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_discovery_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trading_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    sector TEXT,
                    event_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    state TEXT,
                    current_price REAL,
                    current_change_pct REAL,
                    high_change_pct REAL,
                    volume_progress_ratio REAL,
                    turnover_million REAL,
                    liquidity_tier TEXT,
                    quote_source TEXT,
                    discovery_type TEXT,
                    in_radar INTEGER NOT NULL DEFAULT 0,
                    observation_only INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (trading_date, symbol, event_key)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_discovery_events_date_time
                ON intraday_discovery_events(trading_date, observed_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_discovery_events_date_symbol
                ON intraday_discovery_events(trading_date, symbol)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_discovery_exclusion_audit (
                    trading_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    reason_label TEXT NOT NULL,
                    excluded INTEGER NOT NULL DEFAULT 1,
                    first_observed_at TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trading_date, symbol, reason_code)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_discovery_exclusion_date
                ON intraday_discovery_exclusion_audit(trading_date, excluded, reason_code)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_candidate_signals (
                    signal_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signaled_at TEXT NOT NULL,
                    price_date TEXT,
                    score REAL,
                    entry_price REAL NOT NULL,
                    entry_mode TEXT NOT NULL,
                    quote_source TEXT,
                    gate_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (signal_date, symbol)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_candidate_signals_date
                ON intraday_candidate_signals(signal_date, symbol)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_discovery_daily_stats (
                    trading_date TEXT PRIMARY KEY,
                    actual_movers INTEGER NOT NULL DEFAULT 0,
                    detected_movers INTEGER NOT NULL DEFAULT 0,
                    early_detected INTEGER NOT NULL DEFAULT 0,
                    missed_movers INTEGER NOT NULL DEFAULT 0,
                    discovered_symbols INTEGER NOT NULL DEFAULT 0,
                    recall REAL NOT NULL DEFAULT 0,
                    early_recall REAL NOT NULL DEFAULT 0,
                    precision REAL NOT NULL DEFAULT 0,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            daily_stats_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(intraday_discovery_daily_stats)"
                ).fetchall()
            }
            for name, definition in {
                "actionable_detected": "INTEGER NOT NULL DEFAULT 0",
                "late_detected": "INTEGER NOT NULL DEFAULT 0",
                "actionable_recall": "REAL NOT NULL DEFAULT 0",
                "late_rate": "REAL NOT NULL DEFAULT 0",
            }.items():
                add_column_if_missing(
                    conn, "intraday_discovery_daily_stats",
                    daily_stats_columns, name, definition,
                )
            # Five-minute bars are a durable research dataset. They are not read by
            # the production radar; pytorch_experiment.py may use them only after
            # the explicit data-volume and out-of-sample quality gates pass.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_minute_bars (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume_lots REAL NOT NULL DEFAULT 0,
                    cumulative_volume_lots REAL NOT NULL DEFAULT 0,
                    active_buy_volume_lots REAL NOT NULL DEFAULT 0,
                    active_sell_volume_lots REAL NOT NULL DEFAULT 0,
                    unknown_volume_lots REAL NOT NULL DEFAULT 0,
                    active_buy_amount REAL NOT NULL DEFAULT 0,
                    active_sell_amount REAL NOT NULL DEFAULT 0,
                    unknown_amount REAL NOT NULL DEFAULT 0,
                    large_buy_volume_lots REAL NOT NULL DEFAULT 0,
                    large_sell_volume_lots REAL NOT NULL DEFAULT 0,
                    raw_tick_count INTEGER NOT NULL DEFAULT 0,
                    directional_tick_count INTEGER NOT NULL DEFAULT 0,
                    unknown_tick_count INTEGER NOT NULL DEFAULT 0,
                    first_tick_at TEXT,
                    last_tick_at TEXT,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, date, minute)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_intraday_minute_bars_date_symbol
                ON intraday_minute_bars(date, symbol)
            """)
            # Real five-level bid/ask observations are aggregated into the same
            # five-minute cadence as intraday_minute_bars.  Keeping aggregates
            # instead of every quote event bounds storage while preserving the
            # depth, imbalance, spread, and microprice features required by an
            # isolated order-book experiment.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_book_5m_features (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    spread_observation_count INTEGER NOT NULL DEFAULT 0,
                    avg_bid_depth_lots REAL NOT NULL DEFAULT 0,
                    avg_ask_depth_lots REAL NOT NULL DEFAULT 0,
                    avg_imbalance REAL NOT NULL DEFAULT 0,
                    min_imbalance REAL NOT NULL DEFAULT 0,
                    max_imbalance REAL NOT NULL DEFAULT 0,
                    last_imbalance REAL NOT NULL DEFAULT 0,
                    avg_spread_pct REAL,
                    max_spread_pct REAL,
                    last_spread_pct REAL,
                    avg_microprice_gap_pct REAL,
                    last_microprice_gap_pct REAL,
                    last_best_bid REAL,
                    last_best_ask REAL,
                    net_bid_volume_change_lots REAL NOT NULL DEFAULT 0,
                    net_ask_volume_change_lots REAL NOT NULL DEFAULT 0,
                    first_snapshot_at TEXT,
                    last_snapshot_at TEXT,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, date, minute)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_order_book_5m_date_symbol
                ON order_book_5m_features(date, symbol)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_experiment_runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'observation',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    data_max_date TEXT,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    train_count INTEGER NOT NULL DEFAULT 0,
                    validation_count INTEGER NOT NULL DEFAULT 0,
                    test_count INTEGER NOT NULL DEFAULT 0,
                    config_json TEXT,
                    target_json TEXT,
                    split_json TEXT,
                    metrics_json TEXT,
                    comparison_json TEXT,
                    gate_json TEXT,
                    artifact_path TEXT,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_experiment_predictions (
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    actual_hit INTEGER NOT NULL,
                    actual_net_return REAL NOT NULL,
                    tcn_probability REAL,
                    xgboost_probability REAL,
                    lightgbm_probability REAL,
                    tcn_net_return REAL,
                    xgboost_net_return REAL,
                    lightgbm_net_return REAL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, symbol, signal_date),
                    FOREIGN KEY (run_id) REFERENCES model_experiment_runs(run_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_model_experiment_predictions_date
                ON model_experiment_predictions(run_id, signal_date)
            """)

    def finmind_hour_key(self):
        return dt.datetime.now().strftime("%Y-%m-%d %H:00")

    def read_finmind_usage(self):
        default = {
            "hour": self.finmind_hour_key(),
            "calls": 0,
            "safeLimit": FINMIND_SAFE_HOURLY_LIMIT,
            "hardLimit": FINMIND_HOURLY_LIMIT,
            "reserved": FINMIND_RESERVED_CALLS,
            "blocked": False,
            "lastError": "",
            "updatedAt": now_text(),
        }
        if not FINMIND_USAGE_PATH.exists():
            return default
        try:
            usage = json.loads(FINMIND_USAGE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return default
        if usage.get("hour") != default["hour"]:
            return default
        merged = {**default, **usage}
        merged["safeLimit"] = FINMIND_SAFE_HOURLY_LIMIT
        merged["hardLimit"] = FINMIND_HOURLY_LIMIT
        merged["reserved"] = FINMIND_RESERVED_CALLS
        return merged

    def write_finmind_usage(self, usage):
        usage["updatedAt"] = now_text()
        FINMIND_USAGE_PATH.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            with self.connect() as conn:
                self.set_meta(conn, "finmind_usage_hour", str(usage.get("hour") or ""))
                self.set_meta(conn, "finmind_usage_calls", str(usage.get("calls") or 0))
                self.set_meta(conn, "finmind_usage_safe_limit", str(usage.get("safeLimit") or FINMIND_SAFE_HOURLY_LIMIT))
                self.set_meta(conn, "finmind_usage_blocked", "1" if usage.get("blocked") else "0")
                self.set_meta(conn, "finmind_usage_last_error", str(usage.get("lastError") or ""))
        except Exception:
            pass

    def reserve_finmind_call(self, dataset, symbol):
        # 整段「讀計數→判斷→+1→寫回」必須是原子的，見 FINMIND_USAGE_LOCK 註解。
        with FINMIND_USAGE_LOCK:
            usage = self.read_finmind_usage()
            calls = int(usage.get("calls") or 0)
            last_error = str(usage.get("lastError") or "").lower()
            hard_block = usage.get("blocked") and any(code in last_error for code in ("402", "429", "quota", "limit"))
            if usage.get("blocked") and not hard_block and calls < FINMIND_SAFE_HOURLY_LIMIT:
                usage["blocked"] = False
                usage["lastError"] = ""
                self.write_finmind_usage(usage)
            if hard_block:
                raise RuntimeError(str(usage.get("lastError") or "FinMind quota blocked"))
            if calls >= FINMIND_SAFE_HOURLY_LIMIT:
                usage["blocked"] = True
                usage["lastError"] = (
                    f"FinMind quota guard: used {calls} calls in this hour, "
                    f"保守上限 {FINMIND_SAFE_HOURLY_LIMIT} 次，停止呼叫 {dataset} {symbol or ''}".strip()
                )
                self.write_finmind_usage(usage)
                raise RuntimeError(usage["lastError"])
            usage["calls"] = calls + 1
            usage["lastDataset"] = dataset
            usage["lastSymbol"] = symbol
            self.write_finmind_usage(usage)
            return usage

    def block_finmind_usage(self, message):
        with FINMIND_USAGE_LOCK:
            usage = self.read_finmind_usage()
            usage["blocked"] = True
            usage["lastError"] = message
            self.write_finmind_usage(usage)

    def fetch_finmind_dataset(self, dataset, symbol, start, end, token):
        self.reserve_finmind_call(dataset, symbol)
        params = {"dataset": dataset}
        if symbol:
            params["data_id"] = symbol
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        if token:
            params["token"] = token
        headers = {"User-Agent": "Mozilla/5.0"}
        request = Request(f"https://api.finmindtrade.com/api/v4/data?{urlencode(params)}", headers=headers)
        try:
            with urlopen(request, timeout=35) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in (402, 429):
                self.block_finmind_usage(f"FinMind 回應 {exc.code}，停止本小時後續 FinMind 呼叫")
            raise RuntimeError(f"FinMind {dataset} {symbol or ''} HTTP {exc.code}")
        data = raw.get("data") or []
        if not data:
            message = str(raw.get("msg") or f"{dataset} no rows")
            if any(code in message for code in ("402", "429", "使用量", "limit", "quota")):
                self.block_finmind_usage(f"FinMind 額度訊息：{message}")
            raise RuntimeError(message)
        return data

    def roc_date_to_iso(self, value):
        text = "".join(ch for ch in str(value or "") if ch.isdigit())
        if len(text) == 7:
            year = int(text[:3]) + 1911
            return f"{year:04d}-{int(text[3:5]):02d}-{int(text[5:7]):02d}"
        if len(text) == 8:
            return f"{int(text[:4]):04d}-{int(text[4:6]):02d}-{int(text[6:8]):02d}"
        return ""

    def parse_number(self, value):
        text = str(value or "").replace(",", "").replace("--", "").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None

    def fetch_openapi_json(self, url):
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        context = ssl._create_unverified_context()
        with urlopen(request, timeout=30, context=context) as response:
            return json.loads(response.read().decode("utf-8-sig"))

    def iso_to_request_date(self, value):
        text = "".join(ch for ch in str(value or "") if ch.isdigit())
        return text if len(text) == 8 else ""

    def fetch_twse_after_trading_snapshot_rows(self, target_date):
        """Fetch one exact TWSE session after the lagging latest-only OpenAPI."""
        request_date = self.iso_to_request_date(target_date)
        if not request_date:
            raise ValueError(f"TWSE 指定日期格式錯誤：{target_date}")
        payload = self.fetch_openapi_json(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            f"?date={request_date}&type=ALLBUT0999&response=json"
        )
        response_date = self.roc_date_to_iso(payload.get("date"))
        if str(payload.get("stat") or "").upper() != "OK" or response_date != str(target_date)[:10]:
            raise RuntimeError(
                f"TWSE MI_INDEX 尚未提供 {str(target_date)[:10]}，"
                f"stat={payload.get('stat')!s} date={response_date or '-'}"
            )

        rows = {}
        required_fields = {"證券代號", "成交股數", "開盤價", "最高價", "最低價", "收盤價"}
        for table in payload.get("tables") or []:
            fields = table.get("fields") or []
            if not required_fields.issubset(fields):
                continue
            indexes = {name: index for index, name in enumerate(fields)}
            for item in table.get("data") or []:
                symbol = str(item[indexes["證券代號"]] or "").strip()
                close = self.parse_number(item[indexes["收盤價"]])
                volume = self.parse_number(item[indexes["成交股數"]])
                if not re.fullmatch(r"\d{4}", symbol) or close is None or close <= 0 or volume is None or volume < 0:
                    continue
                row = {
                    "symbol": symbol,
                    "date": response_date,
                    "open": self.parse_number(item[indexes["開盤價"]]) or close,
                    "high": self.parse_number(item[indexes["最高價"]]) or close,
                    "low": self.parse_number(item[indexes["最低價"]]) or close,
                    "close": close,
                    "volume": volume,
                    "price_source": "TWSE official MI_INDEX afterTrading",
                }
                per_index = indexes.get("本益比")
                if per_index is not None:
                    per = self.parse_number(item[per_index])
                    row["per"] = per if per is not None and per > 0 else None
                    row["valuation_source"] = "TWSE official MI_INDEX afterTrading"
                    row["finance_source"] = self.finance_source_for_row(row)
                rows[symbol] = row
            break
        if len(rows) < TWSE_AFTER_TRADING_MIN_ROWS:
            raise RuntimeError(
                f"TWSE MI_INDEX {response_date or target_date} 僅 {len(rows)} 筆，"
                f"低於完整門檻 {TWSE_AFTER_TRADING_MIN_ROWS}"
            )
        return rows

    def fetch_tpex_after_trading_snapshot_rows(self, target_date):
        """Fetch one exact TPEx session and exclude warrants/ETNs from the snapshot."""
        request_date = self.iso_to_request_date(target_date)
        if not request_date:
            raise ValueError(f"TPEx 指定日期格式錯誤：{target_date}")
        roc_date = f"{int(request_date[:4]) - 1911:03d}/{request_date[4:6]}/{request_date[6:8]}"
        payload = self.fetch_openapi_json(
            "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
            f"stk_quote_result.php?l=zh-tw&o=json&d={roc_date}&s=0,asc,0"
        )
        response_date = self.roc_date_to_iso(payload.get("date"))
        if str(payload.get("stat") or "").lower() != "ok" or response_date != str(target_date)[:10]:
            raise RuntimeError(
                f"TPEx daily_close_quotes 尚未提供 {str(target_date)[:10]}，"
                f"stat={payload.get('stat')!s} date={response_date or '-'}"
            )

        rows = {}
        required_fields = {"代號", "收盤", "開盤", "最高", "最低", "成交股數"}
        for table in payload.get("tables") or []:
            fields = table.get("fields") or []
            if not required_fields.issubset(fields):
                continue
            indexes = {name: index for index, name in enumerate(fields)}
            for item in table.get("data") or []:
                symbol = str(item[indexes["代號"]] or "").strip()
                # 這個端點同表含一萬多檔權證、ETF、ETN；模型日K只收四碼普通股。
                if not re.fullmatch(r"\d{4}", symbol):
                    continue
                close = self.parse_number(item[indexes["收盤"]])
                volume = self.parse_number(item[indexes["成交股數"]])
                if close is None or close <= 0 or volume is None or volume < 0:
                    continue
                rows[symbol] = {
                    "symbol": symbol,
                    "date": response_date,
                    "open": self.parse_number(item[indexes["開盤"]]) or close,
                    "high": self.parse_number(item[indexes["最高"]]) or close,
                    "low": self.parse_number(item[indexes["最低"]]) or close,
                    "close": close,
                    "volume": volume,
                    "price_source": "TPEx official daily_close_quotes afterTrading",
                }
            break
        if len(rows) < TPEX_AFTER_TRADING_MIN_ROWS:
            raise RuntimeError(
                f"TPEx daily_close_quotes {response_date or target_date} 僅 {len(rows)} 檔普通股，"
                f"低於完整門檻 {TPEX_AFTER_TRADING_MIN_ROWS}"
            )
        return rows

    def fetch_twse_official_latest(self):
        rows = {}
        price_data = self.fetch_openapi_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
        for item in price_data:
            symbol = str(item.get("Code") or "").strip()
            date = self.roc_date_to_iso(item.get("Date"))
            close = self.parse_number(item.get("ClosingPrice"))
            if not symbol or not date or close is None:
                continue
            rows[symbol] = {
                "symbol": symbol,
                "date": date,
                "open": self.parse_number(item.get("OpeningPrice")),
                "high": self.parse_number(item.get("HighestPrice")),
                "low": self.parse_number(item.get("LowestPrice")),
                "close": close,
                "volume": self.parse_number(item.get("TradeVolume")),
                "price_source": "TWSE OpenAPI STOCK_DAY_ALL",
            }
        try:
            valuation_data = self.fetch_openapi_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
            for item in valuation_data:
                row = rows.get(str(item.get("Code") or "").strip())
                if not row:
                    continue
                row["per"] = self.parse_number(item.get("PEratio"))
                if row["per"] is not None and row["per"] <= 0:
                    row["per"] = None
                row["pbr"] = self.parse_number(item.get("PBratio"))
                row["dividend_yield"] = self.parse_number(item.get("DividendYield"))
                row["valuation_source"] = "TWSE OpenAPI BWIBBU_ALL"
                row["finance_source"] = self.finance_source_for_row(row)
        except Exception:
            pass
        request_date = ""
        if rows:
            request_date = self.iso_to_request_date(max(row["date"] for row in rows.values()))
        if request_date:
            try:
                inst_data = self.fetch_openapi_json(
                    f"https://www.twse.com.tw/rwd/zh/fund/T86?date={request_date}&selectType=ALLBUT0999&response=json"
                )
                fields = inst_data.get("fields") or []
                indexes = {name: index for index, name in enumerate(fields)}
                for item in inst_data.get("data") or []:
                    symbol = str(item[indexes.get("證券代號", 0)] or "").strip()
                    row = rows.get(symbol)
                    if not row:
                        continue
                    foreign_index = indexes.get("外陸資買賣超股數(不含外資自營商)")
                    trust_index = indexes.get("投信買賣超股數")
                    if foreign_index is not None:
                        foreign = self.parse_number(item[foreign_index])
                        if foreign is not None:
                            row["foreign_buy_sell"] = foreign / 1000
                    if trust_index is not None:
                        trust = self.parse_number(item[trust_index])
                        if trust is not None:
                            row["trust_buy_sell"] = trust / 1000
                    row["chip_source"] = "TWSE official T86"
            except Exception:
                pass
            try:
                margin_data = self.fetch_openapi_json(
                    f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={request_date}&selectType=ALL&response=json"
                )
                for table in margin_data.get("tables") or []:
                    fields = table.get("fields") or []
                    if fields[:2] != ["代號", "名稱"]:
                        continue
                    for item in table.get("data") or []:
                        if len(item) < 13:
                            continue
                        row = rows.get(str(item[0] or "").strip())
                        if not row:
                            continue
                        margin = self.parse_number(item[6])
                        short = self.parse_number(item[12])
                        if margin is not None:
                            row["margin_balance"] = margin
                        if short is not None:
                            row["short_balance"] = short
                        row["margin_source"] = "TWSE official MI_MARGN"
            except Exception:
                pass
        return rows

    def fetch_tpex_official_latest(self):
        rows = {}
        price_data = self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
        for item in price_data:
            symbol = str(item.get("SecuritiesCompanyCode") or "").strip()
            date = self.roc_date_to_iso(item.get("Date"))
            close = self.parse_number(item.get("Close"))
            if not symbol or not date or close is None:
                continue
            rows[symbol] = {
                "symbol": symbol,
                "date": date,
                "open": self.parse_number(item.get("Open")),
                "high": self.parse_number(item.get("High")),
                "low": self.parse_number(item.get("Low")),
                "close": close,
                "volume": self.parse_number(item.get("TradingShares")),
                "price_source": "TPEx OpenAPI tpex_mainboard_daily_close_quotes",
            }
        try:
            valuation_data = self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
            for item in valuation_data:
                row = rows.get(str(item.get("SecuritiesCompanyCode") or "").strip())
                if not row:
                    continue
                row["per"] = self.parse_number(item.get("PriceEarningRatio"))
                if row["per"] is not None and row["per"] <= 0:
                    row["per"] = None
                row["pbr"] = self.parse_number(item.get("PriceBookRatio"))
                row["dividend_yield"] = self.parse_number(item.get("YieldRatio"))
                row["valuation_source"] = "TPEx OpenAPI tpex_mainboard_peratio_analysis"
                row["finance_source"] = self.finance_source_for_row(row)
        except Exception:
            pass
        try:
            margin_data = self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance")
            for item in margin_data:
                row = rows.get(str(item.get("SecuritiesCompanyCode") or "").strip())
                if not row:
                    continue
                row["margin_balance"] = self.parse_number(item.get("MarginPurchaseBalance"))
                row["short_balance"] = self.parse_number(item.get("ShortSaleBalance"))
                row["margin_source"] = "TPEx OpenAPI tpex_mainboard_margin_balance"
        except Exception:
            pass
        try:
            inst_data = self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading")
            for item in inst_data:
                row = rows.get(str(item.get("SecuritiesCompanyCode") or "").strip())
                if not row:
                    continue
                foreign = self.parse_number(item.get("ForeignInvestorsIncludeMainlandAreaInvestors-Difference"))
                trust = self.parse_number(item.get("SecuritiesInvestmentTrustCompanies-Difference"))
                row["foreign_buy_sell"] = None if foreign is None else foreign / 1000
                row["trust_buy_sell"] = None if trust is None else trust / 1000
                row["chip_source"] = "TPEx OpenAPI tpex_3insti_daily_trading"
        except Exception:
            pass
        return rows

    def fetch_tpex_emerging_official_latest(self):
        """Fetch the official TPEx emerging-market daily reference prices.

        Emerging stocks are quote-driven negotiated securities. TPEx explicitly
        defines no official open or close; the daily weighted average is the
        published end-of-day reference price. Store that average as both open
        and close so the common OHLCV schema remains usable without pretending
        the latest trade is an exchange close. High, low and volume remain the
        exact official values. A no-trade session carries the official previous
        average with zero volume so it is not misreported as a broken pipeline.
        """
        rows = {}
        data = self.fetch_openapi_json(
            "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics"
        )
        for item in data:
            symbol = str(item.get("SecuritiesCompanyCode") or "").strip()
            date = self.roc_date_to_iso(item.get("Date"))
            if not re.fullmatch(r"\d{4}", symbol) or not date:
                continue

            volume = self.parse_number(item.get("TransactionVolume"))
            average = self.parse_number(item.get("Average"))
            no_trade = average is None or average <= 0
            if no_trade:
                average = self.parse_number(item.get("PreviousAveragePrice"))
                volume = 0.0
            if average is None or average <= 0 or volume is None or volume < 0:
                continue

            high = self.parse_number(item.get("Highest"))
            low = self.parse_number(item.get("Lowest"))
            if high is None or high <= 0:
                high = average
            if low is None or low <= 0:
                low = average
            rows[symbol] = {
                "symbol": symbol,
                "date": date,
                # 興櫃沒有正式開盤／收盤價，依櫃買中心制度使用日加權均價作
                # 統一日線參考；不可把最後一筆議價成交冒充正式收盤價。
                "open": average,
                "high": max(high, average),
                "low": min(low, average),
                "close": average,
                "volume": volume,
                "price_source": (
                    "TPEx official tpex_esb_latest_statistics no-trade previous average"
                    if no_trade else
                    "TPEx official tpex_esb_latest_statistics weighted average"
                ),
            }
        if len(rows) < TPEX_EMERGING_MIN_ROWS:
            raise RuntimeError(
                f"TPEx tpex_esb_latest_statistics 僅 {len(rows)} 檔，"
                f"低於完整門檻 {TPEX_EMERGING_MIN_ROWS}"
            )
        return rows

    def merge_mops_monthly_revenue_latest(self, rows):
        endpoints = [
            ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", "TWSE OpenAPI t187ap05_L"),
            ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O", "TPEx OpenAPI mopsfin_t187ap05_O"),
        ]
        for url, source in endpoints:
            try:
                data = self.fetch_openapi_json(url)
            except Exception:
                continue
            for item in data:
                symbol = str(item.get("公司代號") or "").strip()
                target = rows.get(symbol)
                if not target:
                    continue
                revenue = self.parse_number(item.get("營業收入-當月營收"))
                growth = self.parse_number(item.get("營業收入-去年同月增減(%)"))
                if revenue is not None:
                    target["monthly_revenue"] = revenue
                if growth is not None:
                    target["revenue_growth"] = growth
                if revenue is not None or growth is not None:
                    target["revenue_source"] = source
                    target["finance_source"] = self.finance_source_for_row(target)


    def fetch_official_latest_rows(self):
        cache_date = today_key()
        if self._official_latest_cache is not None and self._official_latest_cache_date == cache_date:
            return self._official_latest_cache
        rows = {}
        for fetcher in (self.fetch_twse_official_latest, self.fetch_tpex_official_latest):
            try:
                rows.update(fetcher())
            except Exception:
                continue
        try:
            # 剛從興櫃轉上市櫃時，兩個端點可能短暫同時出現同一代號；主板
            # OHLCV 定義較完整，故只補主板尚未提供的代號。
            for symbol, row in self.fetch_tpex_emerging_official_latest().items():
                rows.setdefault(symbol, row)
        except Exception:
            pass
        self.merge_mops_monthly_revenue_latest(rows)
        self._official_latest_cache = rows
        self._official_latest_cache_date = cache_date
        return rows

    def fetch_official_daily_snapshot_rows(self, target_date=None):
        """Fetch a fast all-market official daily snapshot for scanner preflight.

        This intentionally uses only lightweight TWSE/TPEx OpenAPI daily price
        and valuation endpoints. The slower chip/margin/monthly-revenue repair
        can still run per symbol later, but the scanner first needs verified
        latest price_source rows so Yahoo fallback rows do not hide valid stocks.
        """
        target_date = str(target_date or today_key())[:10]
        rows = {}
        errors = []
        twse_symbols = set()
        tpex_symbols = set()

        try:
            for item in self.fetch_openapi_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
                symbol = str(item.get("Code") or "").strip()
                date = self.roc_date_to_iso(item.get("Date"))
                close = self.parse_number(item.get("ClosingPrice"))
                volume = self.parse_number(item.get("TradeVolume"))
                if not re.fullmatch(r"\d{4}", symbol) or not date or close is None or volume is None:
                    continue
                rows[symbol] = {
                    "symbol": symbol,
                    "date": date,
                    "open": self.parse_number(item.get("OpeningPrice")) or close,
                    "high": self.parse_number(item.get("HighestPrice")) or close,
                    "low": self.parse_number(item.get("LowestPrice")) or close,
                    "close": close,
                    "volume": volume,
                    "price_source": "TWSE OpenAPI STOCK_DAY_ALL",
                }
                twse_symbols.add(symbol)
        except Exception as exc:
            errors.append(f"TWSE STOCK_DAY_ALL: {exc}")

        try:
            for item in self.fetch_openapi_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
                row = rows.get(str(item.get("Code") or "").strip())
                if not row:
                    continue
                per = self.parse_number(item.get("PEratio"))
                row["per"] = per if per is not None and per > 0 else None
                row["pbr"] = self.parse_number(item.get("PBratio"))
                row["dividend_yield"] = self.parse_number(item.get("DividendYield"))
                row["valuation_source"] = "TWSE OpenAPI BWIBBU_ALL"
                row["finance_source"] = self.finance_source_for_row(row)
        except Exception as exc:
            errors.append(f"TWSE BWIBBU_ALL: {exc}")

        try:
            for item in self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"):
                symbol = str(item.get("SecuritiesCompanyCode") or "").strip()
                date = self.roc_date_to_iso(item.get("Date"))
                close = self.parse_number(item.get("Close"))
                volume = self.parse_number(item.get("TradingShares"))
                if not re.fullmatch(r"\d{4}", symbol) or not date or close is None or volume is None:
                    continue
                rows[symbol] = {
                    "symbol": symbol,
                    "date": date,
                    "open": self.parse_number(item.get("Open")) or close,
                    "high": self.parse_number(item.get("High")) or close,
                    "low": self.parse_number(item.get("Low")) or close,
                    "close": close,
                    "volume": volume,
                    "price_source": "TPEx OpenAPI tpex_mainboard_daily_close_quotes",
                }
                tpex_symbols.add(symbol)
        except Exception as exc:
            errors.append(f"TPEx daily_close_quotes: {exc}")

        try:
            emerging_rows = self.fetch_tpex_emerging_official_latest()
            for symbol, row in emerging_rows.items():
                # 上市櫃轉板交界可能短暫重複，完整主板 OHLCV 優先。
                rows.setdefault(symbol, row)
        except Exception as exc:
            errors.append(f"TPEx emerging latest: {exc}")

        try:
            for item in self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"):
                row = rows.get(str(item.get("SecuritiesCompanyCode") or "").strip())
                if not row:
                    continue
                per = self.parse_number(item.get("PriceEarningRatio"))
                row["per"] = per if per is not None and per > 0 else None
                row["pbr"] = self.parse_number(item.get("PriceBookRatio"))
                row["dividend_yield"] = self.parse_number(item.get("YieldRatio"))
                row["valuation_source"] = "TPEx OpenAPI tpex_mainboard_peratio_analysis"
                row["finance_source"] = self.finance_source_for_row(row)
        except Exception as exc:
            errors.append(f"TPEx peratio_analysis: {exc}")

        # The two latest-only OpenAPI feeds can lag the exchange's dated close
        # tables for hours. Upgrade each market independently only after the
        # exact requested session passes its own completeness threshold.
        twse_latest = max((rows[symbol]["date"] for symbol in twse_symbols), default="")
        if twse_latest < target_date:
            try:
                fallback_rows = self.fetch_twse_after_trading_snapshot_rows(target_date)
                for symbol, fallback in fallback_rows.items():
                    rows[symbol] = {**rows.get(symbol, {}), **fallback}
            except Exception as exc:
                errors.append(f"TWSE afterTrading {target_date}: {exc}")

        tpex_latest = max((rows[symbol]["date"] for symbol in tpex_symbols), default="")
        if tpex_latest < target_date:
            try:
                fallback_rows = self.fetch_tpex_after_trading_snapshot_rows(target_date)
                for symbol, fallback in fallback_rows.items():
                    rows[symbol] = {**rows.get(symbol, {}), **fallback}
            except Exception as exc:
                errors.append(f"TPEx afterTrading {target_date}: {exc}")

        return rows, errors

    def load_cached_symbol_rows_if_fresh(self, symbol):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            latest = conn.execute("""
                SELECT MAX(updated_at) AS updated_at, COUNT(*) AS count
                FROM prices
                WHERE symbol = ?
            """, (symbol,)).fetchone()
            if not latest or not latest["count"]:
                return None
            # updated_at 以本地時間 now_text() 寫入;today_key() 是台北日。主機時區非台北(例如搬
            # NAS 跑 UTC)在跨日邊界兩者會分歧,把當天剛抓的資料誤判 not-fresh 而重抓(浪費額度)。
            # 額外接受「本地今日」(=同一支時鐘寫的)避免誤判;昨日快取仍不會被當成新鮮。
            _uat = str(latest["updated_at"] or "")
            _local_today = dt.datetime.now().strftime("%Y-%m-%d")
            if not (_uat.startswith(today_key()) or _uat.startswith(_local_today)):
                return None
            latest_source = conn.execute("""
                SELECT price_source
                FROM prices
                WHERE symbol = ?
                ORDER BY date DESC
                LIMIT 1
            """, (symbol,)).fetchone()
            if not latest_source or not is_official_source(latest_source["price_source"]):
                return None
            rows = conn.execute("SELECT * FROM prices WHERE symbol = ? ORDER BY date", (symbol,)).fetchall()
        return [dict(row) for row in rows]

    def fetch_symbol_rows(self, symbol, days=1120, use_cache=True, include_extended=False):
        if use_cache:
            cached = self.load_cached_symbol_rows_if_fresh(symbol)
            if cached:
                return cached
        token = read_finmind_token()
        start = start_date(days)
        end = today_key()
        finmind_dataset_count = 0
        rows = {}
        finmind_price_ok = False
        try:
            price_rows = self.fetch_finmind_dataset("TaiwanStockPrice", symbol, start, end, token)
            finmind_dataset_count += 1
            finmind_price_ok = True
        except Exception:
            cached_rows = self.load_price_rows(symbol)
            if cached_rows:
                rows = {row["date"]: dict(row) for row in cached_rows}
            else:
                yahoo_rows = self.fetch_yahoo_fallback_price_rows(symbol, days)
                for item in yahoo_rows:
                    rows[item["date"]] = {
                        "symbol": symbol,
                        "date": item["date"],
                        "open": item.get("open") or item["close"],
                        "high": item.get("high") or item["close"],
                        "low": item.get("low") or item["close"],
                        "close": item["close"],
                        "volume": item.get("volume"),
                        "price_source": item.get("price_source") or "Yahoo Finance chart API fallback",
                    }
            price_rows = []
        yahoo_backfill_rows = None
        last_good_close = None
        for item in price_rows:
            date = item.get("date")
            if not date:
                continue
            open_v = float(item.get("open") or 0)
            high_v = float(item.get("max") or 0)
            low_v = float(item.get("min") or 0)
            close_v = float(item.get("close") or 0)
            volume_v = float(item.get("Trading_Volume") or 0)
            source = "FinMind TaiwanStockPrice"
            if min(open_v, high_v, low_v, close_v) <= 0:
                # FinMind 對這天回傳壞資料（例如整天 OHLC 全 0），改用 Yahoo
                # Finance 補這一天，而不是把 0 直接寫進資料庫。同一輪只打一次
                # Yahoo API，之後查表重複使用。
                if yahoo_backfill_rows is None:
                    try:
                        yahoo_backfill_rows = {
                            row["date"]: row for row in self.fetch_yahoo_fallback_price_rows(symbol, days)
                        }
                    except Exception:
                        yahoo_backfill_rows = {}
                yahoo_row = yahoo_backfill_rows.get(date)
                if not yahoo_row:
                    continue
                yahoo_close = float(yahoo_row["close"])
                if not price_scale_is_plausible(yahoo_close, last_good_close):
                    continue
                close_v = yahoo_close
                open_v = float(yahoo_row.get("open") or close_v)
                high_v = float(yahoo_row.get("high") or close_v)
                low_v = float(yahoo_row.get("low") or close_v)
                volume_v = float(yahoo_row.get("volume") or 0)
                source = yahoo_row.get("price_source") or "Yahoo Finance chart API fallback"
            last_good_close = close_v
            # 護欄：把 FinMind 偶發的越界 open 夾回 [low, high](high/low/close 可信、
            # 只有 open 壞的系統性髒列)。只碰 open，不動 close 這個所有特徵/標籤的主欄。
            open_v = clamp_open_to_bar(open_v, low_v, high_v)
            rows[date] = {
                "symbol": symbol,
                "date": date,
                "open": open_v,
                "high": high_v,
                "low": low_v,
                "close": close_v,
                "volume": volume_v,
                "price_source": source,
            }

        chip_wide_rows = self.fetch_optional("TaiwanStockInstitutionalInvestorsBuySellWide", symbol, start, end, token) if finmind_price_ok else []
        if finmind_price_ok:
            finmind_dataset_count += 1
        self.merge_institutional_wide(rows, chip_wide_rows)

        chip_rows = []
        if finmind_price_ok and not any(row.get("chip_source") == "FinMind TaiwanStockInstitutionalInvestorsBuySellWide" for row in rows.values()):
            chip_rows = self.fetch_optional("TaiwanStockInstitutionalInvestorsBuySell", symbol, start, end, token)
            finmind_dataset_count += 1
        for item in chip_rows:
            row = rows.get(item.get("date"))
            if not row:
                continue
            buy = self.safe_float(item.get("buy"))
            sell = self.safe_float(item.get("sell"))
            if buy is None or sell is None:
                continue
            value = (buy - sell) / 1000
            name = item.get("name", "")
            if "Foreign" in name or "外資" in name:
                row["foreign_buy_sell"] = value
                row["chip_source"] = "FinMind TaiwanStockInstitutionalInvestorsBuySell"
            elif "Investment" in name or "投信" in name:
                row["trust_buy_sell"] = value
                row["chip_source"] = "FinMind TaiwanStockInstitutionalInvestorsBuySell"

        margin_rows = self.fetch_optional("TaiwanStockMarginPurchaseShortSale", symbol, start, end, token) if finmind_price_ok else []
        finmind_dataset_count += 1
        for item in margin_rows:
            row = rows.get(item.get("date"))
            if not row:
                continue
            margin = self.safe_float(item.get("MarginPurchaseTodayBalance"))
            short = self.safe_float(item.get("ShortSaleTodayBalance"))
            if margin is not None:
                row["margin_balance"] = margin
            if short is not None:
                row["short_balance"] = short
            if margin is not None or short is not None:
                row["margin_source"] = "FinMind TaiwanStockMarginPurchaseShortSale"

        revenue_rows = self.fetch_optional("TaiwanStockMonthRevenue", symbol, start_date(days + 430), end, token) if finmind_price_ok else []
        if finmind_price_ok:
            finmind_dataset_count += 1
        self.merge_revenue(rows, revenue_rows)
        per_rows = self.fetch_optional("TaiwanStockPER", symbol, start, end, token) if finmind_price_ok else []
        if finmind_price_ok:
            finmind_dataset_count += 1
        self.merge_per_pbr(rows, per_rows)
        # 財報(損益表/資產負債表/現金流量表)是季資料，往前多抓 430 天確保價格區間
        # 一開始也有財報快照可用（跟月營收 revenue_rows 用同樣的緩衝天數）。
        statement_start = start_date(days + 430)
        income_rows = self.fetch_optional("TaiwanStockFinancialStatements", symbol, statement_start, end, token) if finmind_price_ok else []
        if finmind_price_ok:
            finmind_dataset_count += 1
        balance_rows = self.fetch_optional("TaiwanStockBalanceSheet", symbol, statement_start, end, token) if finmind_price_ok else []
        if finmind_price_ok:
            finmind_dataset_count += 1
        cashflow_rows = self.fetch_optional("TaiwanStockCashFlowsStatement", symbol, statement_start, end, token) if finmind_price_ok else []
        if finmind_price_ok:
            finmind_dataset_count += 1
        self.merge_financial_statements(rows, income_rows, balance_rows, cashflow_rows)
        if include_extended and finmind_dataset_count < FINMIND_MAX_DATASETS_PER_SYMBOL:
            self.merge_day_trading(rows, self.fetch_optional("TaiwanStockDayTrading", symbol, start, end, token))
            finmind_dataset_count += 1
        if include_extended and finmind_dataset_count < FINMIND_MAX_DATASETS_PER_SYMBOL:
            self.merge_securities_lending(rows, self.fetch_optional("TaiwanStockSecuritiesLending", symbol, start, end, token))
            finmind_dataset_count += 1
        if include_extended and finmind_dataset_count < FINMIND_MAX_DATASETS_PER_SYMBOL:
            branch_rows = self.fetch_trading_daily_report_optional(symbol, rows.keys(), token, max_dates=1)
            if branch_rows:
                self.merge_broker_branch_flow(rows, branch_rows, source="FinMind TaiwanStockTradingDailyReport")
            else:
                self.merge_broker_branch_flow(rows, self.fetch_secid_agg_optional(symbol, start, end, token))
            finmind_dataset_count += 1
        self.merge_intraday_confirmation(rows, symbol, start, end, token)
        official = self.fetch_official_latest_rows().get(symbol)
        if official:
            target = rows.setdefault(official["date"], {"symbol": symbol, "date": official["date"]})
            for key, value in official.items():
                if key in {"symbol", "date"}:
                    continue
                if value is not None:
                    target[key] = value
            target["finance_source"] = self.finance_source_for_row(target)
        return [rows[key] for key in sorted(rows)]

    def fetch_optional(self, dataset, symbol, start, end, token):
        if dataset not in FINMIND_OPTIONAL_SHORT_TERM_DATASETS:
            return []
        try:
            return self.fetch_finmind_dataset(dataset, symbol, start, end, token)
        except Exception:
            return []

    def fetch_trading_daily_report_optional(self, symbol, dates, token, max_dates=1):
        if "TaiwanStockTradingDailyReport" not in FINMIND_OPTIONAL_SHORT_TERM_DATASETS:
            return []
        if not symbol or not token:
            return []
        normalized_dates = sorted({str(date)[:10] for date in dates or [] if str(date).strip()}, reverse=True)
        rows = []
        for date in normalized_dates[:max(1, int(max_dates or 1))]:
            try:
                self.reserve_finmind_call("TaiwanStockTradingDailyReport", symbol)
                params = {"data_id": symbol, "date": date, "token": token}
                request = Request(
                    f"https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report?{urlencode(params)}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urlopen(request, timeout=35) as response:
                    raw = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in (402, 429):
                    self.block_finmind_usage(f"FinMind TradingDailyReport HTTP {exc.code}")
                continue
            except Exception:
                continue
            data = raw.get("data") or []
            if not data:
                message = str(raw.get("msg") or "")
                if any(code in message for code in ("402", "429", "limit", "quota")):
                    self.block_finmind_usage(f"FinMind TradingDailyReport limit: {message}")
                continue
            rows.extend(data)
        return rows

    def fetch_yahoo_chart_rows(self, yahoo_symbol, days=1120):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(yahoo_symbol, safe='')}?range=5y&interval=1d&events=history"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=30) as response:
            raw = json.loads(response.read().decode("utf-8"))
        result = (raw.get("chart") or {}).get("result") or []
        if not result:
            raise RuntimeError(f"Yahoo chart no rows for {yahoo_symbol}")
        payload = result[0]
        timestamps = payload.get("timestamp") or []
        quote_rows = (payload.get("indicators") or {}).get("quote") or [{}]
        values = quote_rows[0]
        rows = []
        cutoff = dt.date.today() - dt.timedelta(days=days)
        for index, timestamp in enumerate(timestamps):
            close = self.safe_float((values.get("close") or [None])[index])
            if close is None or close <= 0:
                continue
            date = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date()
            if date < cutoff:
                continue
            open_v = self.safe_float((values.get("open") or [None])[index])
            high_v = self.safe_float((values.get("high") or [None])[index])
            low_v = self.safe_float((values.get("low") or [None])[index])
            volume_v = self.safe_float((values.get("volume") or [None])[index])
            # Yahoo 對冷門/低流動性台股的 chart API，偶爾會回傳「開高低收
            # 全部等於收盤價、成交量是 0」的假交易日(不是真的停牌，是 Yahoo
            # 資料本身在缺資料時的填補瑕疵)。這種列如果被當成真實交易日寫進
            # 資料庫，會在原始價格序列裡憑空造成一次跟前後完全不連續的跳空
            # (實測發現全庫有上千檔股票中鏢)，汙染 K 線型態/報酬率等所有
            # 依賴日線的計算，所以直接跳過，等下一個資料來源或下一輪重試。
            is_degenerate = (
                (volume_v or 0) <= 0
                and open_v is not None and high_v is not None and low_v is not None
                and open_v == high_v == low_v == close
            )
            if is_degenerate:
                continue
            rows.append({
                "date": date.isoformat(),
                "open": open_v,
                "high": high_v,
                "low": low_v,
                "close": close,
                "volume": volume_v,
            })
        if not rows:
            raise RuntimeError(f"Yahoo chart parsed no usable rows for {yahoo_symbol}")
        return rows

    def fetch_yahoo_fallback_price_rows(self, symbol, days=1120):
        errors = []
        for suffix in (".TW", ".TWO"):
            yahoo_symbol = f"{symbol}{suffix}"
            try:
                rows = self.fetch_yahoo_chart_rows(yahoo_symbol, days)
                for row in rows:
                    row["price_source"] = f"Yahoo Finance chart API fallback {yahoo_symbol}"
                return rows
            except Exception as exc:
                errors.append(f"{yahoo_symbol}: {exc}")
        raise RuntimeError(f"Yahoo fallback failed for {symbol}: {'; '.join(errors)}")

    def safe_float(self, value, fallback=None):
        if value is None:
            return fallback
        try:
            output = float(value)
        except (TypeError, ValueError):
            return fallback
        return output if math.isfinite(output) else fallback

    def fetch_taiex_live(self):
        """今日即時加權指數(盤中會跳動,收盤後為當日收盤)。用 Yahoo ^TWII chart 的
        meta.regularMarketPrice + chartPreviousClose 算今日漲跌%。快取 30 秒,避免每次
        開頁/多分頁重複打 Yahoo。失敗回 None(呼叫端 fail-soft,退回 EOD 大盤狀態)。"""
        cache = getattr(self, "_taiex_live_cache", None)
        if cache is not None and (dt.datetime.now() - cache[0]).total_seconds() < 30:
            return cache[1]
        result = None
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?range=1d&interval=5m"
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=8) as response:
                raw = json.loads(response.read().decode("utf-8"))
            meta = ((raw.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
            price = self.safe_float(meta.get("regularMarketPrice"))
            prev = self.safe_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
            ts = meta.get("regularMarketTime")
            if price and prev and price > 0 and prev > 0:
                quote_dt = dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=8))) if ts else None
                result = {
                    "taiexLivePrice": round(price, 2),
                    "taiexLiveChangePct": round((price / prev - 1) * 100, 2),
                    "taiexLiveTime": quote_dt.strftime("%H:%M") if quote_dt else None,
                    "taiexLiveDate": quote_dt.strftime("%Y-%m-%d") if quote_dt else None,
                }
        except Exception:
            result = None
        self._taiex_live_cache = (dt.datetime.now(), result)
        return result

    def fetch_tpex_index_rows(self):
        """櫃買指數(OTC)官方日線。

        Yahoo ^TWOII 已失效(.TW/.TWO 皆回 HTTP 404)，導致 OTC 大盤資料自
        2026-06 起斷更。改用 TPEx 官方 OpenAPI tpex_index 為主源，回傳格式與
        fetch_yahoo_chart_rows 完全一致(date/open/high/low/close)，讓
        store_market_rows 直接沿用、update_market_data 的寫入路徑不用改。

        注意：此端點僅提供最近數個交易日(實測 3 筆)，靠每日更新以
        INSERT OR REPLACE 累積歷史；歷史回補需另走 dated 端點(本次不做)。
        """
        data = self.fetch_openapi_json("https://www.tpex.org.tw/openapi/v1/tpex_index")
        rows = []
        for item in data:
            raw_date = str(item.get("Date") or "").strip()
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue  # tpex_index 的 Date 是西元 YYYYMMDD(非民國)
            date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            close = self.parse_number(item.get("Close"))
            if close is None or close <= 0:
                continue
            rows.append({
                "date": date,
                "open": self.parse_number(item.get("Open")),
                "high": self.parse_number(item.get("High")),
                "low": self.parse_number(item.get("Low")),
                "close": close,
                "volume": None,
            })
        if not rows:
            raise RuntimeError("TPEx tpex_index parsed no usable rows")
        rows.sort(key=lambda r: r["date"])
        return rows

    def update_market_data(self):
        # 網路抓取(每個來源最多 35 秒逾時)一定要在開啟寫入交易「之前」全部完成：
        # 舊版在同一條連線的迴圈裡邊抓邊寫，第一筆 INSERT 就取得全域寫入鎖並
        # 持有到底，後續每個 Yahoo 來源的網路等待期間，整個程序的其他寫入
        # (盤中報價、掃描、HTTP handler)全部被卡住。
        warnings = []
        counts = {}
        fetched = []
        for market_key, yahoo_symbol, label in MARKET_SOURCES:
            try:
                if market_key == "OTC":
                    # Yahoo ^TWOII 已 404 失效，OTC 櫃買指數改抓 TPEx 官方 OpenAPI。
                    rows = self.fetch_tpex_index_rows()
                    source = "TPEx OpenAPI tpex_index"
                else:
                    rows = self.fetch_yahoo_chart_rows(yahoo_symbol)
                    source = f"Yahoo {label} {yahoo_symbol}"
                counts[market_key] = len(rows)
                fetched.append((market_key, rows, source))
            except Exception as exc:
                warnings.append(f"{market_key}: {exc}")

        warnings.append("TXF: no verified TAIFEX daily history source configured; TXF feature disabled")

        with self.connect() as conn:
            for market_key, rows, source in fetched:
                self.store_market_rows(conn, market_key, rows, source)
            conn.execute("DELETE FROM market_prices WHERE market_key = 'TXF' AND source LIKE '%proxy%'")
            self.set_meta(conn, "last_market_update", now_text())
            self.set_meta(conn, "market_data_warnings", json.dumps(warnings, ensure_ascii=False))
        # 大盤資料影響「所有」股票的 market_gate/機率調整，整個預測快取作廢。
        # 同 _invalidate_predict_cache：clear 同時推進 gen，讓計算中的執行緒
        # 放棄用過時資料寫回。此處在 self.connect() 的 with 區塊外，不持
        # DB_WRITE_LOCK。
        with self._predict_cache_lock:
            self._predict_cache.clear()
            self._predict_cache_gen += 1
        return {"counts": counts, "warnings": warnings}

    def store_market_rows(self, conn, market_key, rows, source):
        for row in rows:
            conn.execute("""
                INSERT OR REPLACE INTO market_prices (
                    market_key, date, open, high, low, close, volume, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_key, row["date"], row.get("open"), row.get("high"), row.get("low"),
                row["close"], row.get("volume"), source, now_text()
            ))

    def update_stock_info(self, symbols):
        token = read_finmind_token()
        try:
            data = self.fetch_finmind_dataset("TaiwanStockInfo", "", "", "", token)
        except Exception:
            return {}
        wanted = set(symbols or [])
        found = {}
        with self.connect() as conn:
            for item in data:
                symbol = str(item.get("stock_id", "")).strip()
                if wanted and symbol not in wanted:
                    continue
                name = str(item.get("stock_name", "")).strip()
                sector = str(item.get("industry_category", "")).strip()
                market_type = str(item.get("type", "")).strip()
                if not symbol:
                    continue
                if is_etf_like_stock(symbol, name, sector, market_type):
                    continue
                current = found.get(symbol)
                if current and current.get("sector") not in {"電子工業", "其他", ""}:
                    continue
                found[symbol] = {"name": name, "sector": sector or "台股", "market_type": market_type}
            for symbol, info in found.items():
                conn.execute("""
                    INSERT OR REPLACE INTO stock_info (symbol, name, sector, market_type, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (symbol, info.get("name"), info.get("sector"), info.get("market_type"), now_text()))
        return found

    def merge_revenue(self, rows, revenue_rows):
        revenue_by_period = {}
        for item in revenue_rows:
            year = item.get("revenue_year")
            month = item.get("revenue_month")
            revenue = item.get("revenue")
            if year and month and revenue is not None:
                revenue_by_period[(int(year), int(month))] = float(revenue)

        monthly = []
        for item in revenue_rows:
            date = item.get("date")
            year = item.get("revenue_year")
            month = item.get("revenue_month")
            revenue = item.get("revenue")
            if not (date and year and month and revenue is not None):
                continue
            previous = revenue_by_period.get((int(year) - 1, int(month)))
            growth = ((float(revenue) - previous) / previous) * 100 if previous else None
            monthly.append((date, float(revenue), growth))
        monthly.sort(key=lambda row: row[0])

        active = None
        index = 0
        for date in sorted(rows):
            while index < len(monthly) and monthly[index][0] <= date:
                active = monthly[index]
                index += 1
            if active:
                rows[date]["monthly_revenue"] = active[1]
                rows[date]["revenue_growth"] = active[2]
                rows[date]["revenue_source"] = "FinMind TaiwanStockMonthRevenue"
                rows[date]["finance_source"] = self.finance_source_for_row(rows[date])

    def merge_per_pbr(self, rows, per_rows):
        # PER/PBR/殖利率跟月營收、財報一樣是低頻資料（不是每個交易日都有新
        # 快照），要往前結轉最近一次有效值，否則遇到 FinMind 當天沒回傳、或
        # 這天是官方快照補進來的價格列，就會整欄空白。三個欄位各自獨立結轉，
        # 避免其中一個因無效值被設成 None 就連帶蓋掉其他仍然有效的欄位。
        per_series, pbr_series, dividend_series = [], [], []
        for item in per_rows:
            date = item.get("date")
            if not date:
                continue
            per = self.safe_float(item.get("PER"))
            if per is not None and per <= 0:
                per = None
            if per is not None:
                per_series.append((date, per))
            pbr = self.safe_float(item.get("PBR"))
            if pbr is not None:
                pbr_series.append((date, pbr))
            dividend_yield = self.safe_float(item.get("dividend_yield"))
            if dividend_yield is not None:
                dividend_series.append((date, dividend_yield))
        per_series.sort(key=lambda row: row[0])
        pbr_series.sort(key=lambda row: row[0])
        dividend_series.sort(key=lambda row: row[0])

        active_per = active_pbr = active_dividend = None
        i = j = k = 0
        for date in sorted(rows):
            while i < len(per_series) and per_series[i][0] <= date:
                active_per = per_series[i][1]
                i += 1
            while j < len(pbr_series) and pbr_series[j][0] <= date:
                active_pbr = pbr_series[j][1]
                j += 1
            while k < len(dividend_series) and dividend_series[k][0] <= date:
                active_dividend = dividend_series[k][1]
                k += 1
            if active_per is None and active_pbr is None and active_dividend is None:
                continue
            rows[date]["per"] = active_per
            rows[date]["pbr"] = active_pbr
            rows[date]["dividend_yield"] = active_dividend
            rows[date]["valuation_source"] = "FinMind TaiwanStockPER"
            rows[date]["finance_source"] = self.finance_source_for_row(rows[date])

    def merge_institutional_wide(self, rows, chip_rows):
        merged = 0
        for item in chip_rows or []:
            row = rows.get(item.get("date"))
            if not row:
                continue
            foreign_buy = self.safe_float(item.get("Foreign_Investor_buy"))
            foreign_sell = self.safe_float(item.get("Foreign_Investor_sell"))
            trust_buy = self.safe_float(item.get("Investment_Trust_buy"))
            trust_sell = self.safe_float(item.get("Investment_Trust_sell"))
            dealer_buy = self.safe_float(item.get("Dealer_buy"))
            dealer_sell = self.safe_float(item.get("Dealer_sell"))
            dealer_self_buy = self.safe_float(item.get("Dealer_self_buy"))
            dealer_self_sell = self.safe_float(item.get("Dealer_self_sell"))
            dealer_hedging_buy = self.safe_float(item.get("Dealer_Hedging_buy"))
            dealer_hedging_sell = self.safe_float(item.get("Dealer_Hedging_sell"))
            if foreign_buy is not None and foreign_sell is not None:
                row["foreign_buy_sell"] = (foreign_buy - foreign_sell) / 1000
                merged += 1
            if trust_buy is not None and trust_sell is not None:
                row["trust_buy_sell"] = (trust_buy - trust_sell) / 1000
                merged += 1
            dealer_net_parts = []
            for buy, sell in (
                (dealer_buy, dealer_sell),
                (dealer_self_buy, dealer_self_sell),
                (dealer_hedging_buy, dealer_hedging_sell),
            ):
                if buy is not None and sell is not None:
                    dealer_net_parts.append(buy - sell)
            if dealer_net_parts:
                row["dealer_buy_sell"] = sum(dealer_net_parts) / 1000
            if (
                foreign_buy is not None or foreign_sell is not None or
                trust_buy is not None or trust_sell is not None or
                dealer_net_parts
            ):
                row["chip_source"] = "FinMind TaiwanStockInstitutionalInvestorsBuySellWide"
        return merged


    def merge_day_trading(self, rows, day_trading_rows):
        for item in day_trading_rows:
            row = rows.get(item.get("date"))
            if not row:
                continue
            volume = self.safe_float(item.get("Volume"))
            daily_volume = self.safe_float(row.get("volume"))
            buy_amount = self.safe_float(item.get("BuyAmount"))
            sell_amount = self.safe_float(item.get("SellAmount"))
            if volume is None or daily_volume is None or daily_volume <= 0:
                continue
            row["day_trade_ratio"] = volume / max(daily_volume, 1)
            if buy_amount is not None and sell_amount is not None:
                row["day_trade_buy_sell_imbalance"] = (buy_amount - sell_amount) / max(buy_amount + sell_amount, 1)

    def merge_securities_lending(self, rows, lending_rows):
        grouped = {}
        for item in lending_rows:
            date = item.get("date")
            if date not in rows:
                continue
            bucket = grouped.setdefault(date, {"volume": 0.0, "volume_seen": False, "fee_total": 0.0, "fee_count": 0})
            volume = self.safe_float(item.get("volume"))
            if volume is not None:
                bucket["volume"] += volume
                bucket["volume_seen"] = True
            fee_rate = self.safe_float(item.get("fee_rate"))
            if fee_rate is not None:
                bucket["fee_total"] += fee_rate
                bucket["fee_count"] += 1
        for date, bucket in grouped.items():
            rows[date]["securities_lending_volume"] = bucket["volume"] if bucket["volume_seen"] else None
            rows[date]["securities_lending_fee_rate"] = (
                bucket["fee_total"] / bucket["fee_count"] if bucket["fee_count"] else None
            )

    def statement_disclosure_date(self, period_end):
        # FinMind 回傳的財報 date 是「所屬期間」結束日（例如 Q1 是 3/31），
        # 不是「實際公開日」。台股法定最晚申報期限：Q1/Q3 季報是期末後 45
        # 天、Q2 半年報是期末後 60 天、年報(Q4) 是期末後 90 天。用最晚法定
        # 期限當作這筆數字「最早可能被市場知道」的日期，訓練特徵才不會
        # 提前用到當時還沒公開的財報數字（前視偏誤 / lookahead bias）。
        try:
            year, month, day = (int(part) for part in str(period_end).split("-"))
            base = dt.date(year, month, day)
        except (ValueError, AttributeError, TypeError):
            return period_end
        lag_days = {3: 45, 9: 45, 6: 60, 12: 90}.get(month, 60)
        return (base + dt.timedelta(days=lag_days)).isoformat()

    def merge_financial_statements(self, rows, income_rows, balance_rows, cashflow_rows):
        quarterly = {}

        def bucket(item):
            date = item.get("date")
            if not date:
                return None
            return quarterly.setdefault(date, {})

        for item in income_rows:
            target = bucket(item)
            if target is None:
                continue
            target[item.get("type")] = self.safe_float(item.get("value"))
        for item in balance_rows:
            target = bucket(item)
            if target is None:
                continue
            target[item.get("type")] = self.safe_float(item.get("value"))
        for item in cashflow_rows:
            target = bucket(item)
            if target is None:
                continue
            target[item.get("type")] = self.safe_float(item.get("value"))

        snapshots = []
        for date, values in sorted(quarterly.items()):
            # FinMind 的 type 欄位命名因產業/公司而異：
            #   一般製造業：Revenue, GrossProfit
            #   電信/服務業：OperatingRevenue（有時沒有 GrossProfit）
            #   金融業：NetRevenue / InterestIncome 等（無傳統 GrossProfit）
            # 盡可能用多個備援名稱，讓財報 coverage 不受命名差異影響。
            revenue = (
                values.get("Revenue") or
                values.get("OperatingRevenue") or
                values.get("NetRevenue") or
                values.get("TotalRevenue")
            )
            gross_profit = values.get("GrossProfit")
            operating_income = (
                values.get("OperatingIncome") or
                values.get("OperatingProfit")
            )
            net_income = (
                values.get("EquityAttributableToOwnersOfParent") or
                values.get("NetIncome") or
                values.get("ProfitAttributableToOwnersOfParent")
            )
            equity = (
                values.get("Equity") or
                values.get("EquityAttributableToOwnersOfParent") or
                values.get("TotalEquity")
            )
            liabilities = (
                values.get("Liabilities") or
                values.get("TotalLiabilities")
            )
            assets = (
                values.get("TotalAssets") or
                values.get("Assets")
            )
            operating_cashflow = (
                values.get("CashFlowsFromOperatingActivities") or
                values.get("NetCashInflowFromOperatingActivities") or
                values.get("CashReceivedThroughOperations") or
                values.get("NetCashProvidedByOperatingActivities")
            )
            snapshots.append((
                self.statement_disclosure_date(date),
                {
                    "gross_margin": (gross_profit / revenue) * 100 if revenue and gross_profit is not None else None,
                    "operating_margin": (operating_income / revenue) * 100 if revenue and operating_income is not None else None,
                    "roe": (net_income / equity) * 100 if equity and net_income is not None else None,
                    "debt_ratio": (liabilities / assets) * 100 if assets and liabilities is not None else None,
                    "operating_cashflow_ratio": (operating_cashflow / revenue) * 100 if revenue and operating_cashflow is not None else None,
                }
            ))
        snapshots.sort(key=lambda item: item[0])

        active = None
        index = 0
        for date in sorted(rows):
            while index < len(snapshots) and snapshots[index][0] <= date:
                active = snapshots[index][1]
                index += 1
            if active:
                usable = {key: value for key, value in active.items() if value is not None}
                if usable:
                    rows[date].update(usable)
                    rows[date]["financial_statement_source"] = "FinMind financial statements"
                    rows[date]["finance_source"] = self.finance_source_for_row(rows[date])

    BROKER_BRANCH_TOP_N = 5  # 主力定義：當日成交量(買+賣)最大的前5個分點

    def fetch_finmind_secid_agg(self, symbol, start, end, token):
        # secid_agg 是分點進出資料，走的是獨立 API 路徑(不是標準的
        # /api/v4/data?dataset=...)，所以不能直接用 fetch_finmind_dataset，
        # 但額度保留/封鎖判斷邏輯跟其他 FinMind 呼叫保持一致。
        dataset_key = "TaiwanStockTradingDailyReportSecIdAgg"
        self.reserve_finmind_call(dataset_key, symbol)
        params = {"data_id": symbol}
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        if token:
            params["token"] = token
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report_secid_agg?{urlencode(params)}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=35) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in (402, 429):
                self.block_finmind_usage(f"FinMind 回應 {exc.code}，停止本小時後續 FinMind 呼叫")
            raise RuntimeError(f"FinMind {dataset_key} {symbol or ''} HTTP {exc.code}")
        data = raw.get("data") or []
        if not data:
            message = str(raw.get("msg") or f"{dataset_key} no rows")
            if any(code in message for code in ("402", "429", "使用量", "limit", "quota")):
                self.block_finmind_usage(f"FinMind 額度訊息：{message}")
            raise RuntimeError(message)
        return data

    def fetch_secid_agg_optional(self, symbol, start, end, token):
        if "TaiwanStockTradingDailyReportSecIdAgg" not in FINMIND_OPTIONAL_SHORT_TERM_DATASETS:
            return []
        try:
            return self.fetch_finmind_secid_agg(symbol, start, end, token)
        except Exception:
            return []

    def merge_broker_branch_flow(self, rows, secid_rows, source="FinMind taiwan_stock_trading_daily_report_secid_agg"):
        by_date = {}
        for item in secid_rows:
            date = item.get("date")
            if date not in rows:
                continue
            buy = self.safe_float(item.get("buy_volume"))
            sell = self.safe_float(item.get("sell_volume"))
            if buy is None and sell is None:
                buy = self.safe_float(item.get("buy"))
                sell = self.safe_float(item.get("sell"))
            if buy is None and sell is None:
                continue
            net = (buy or 0) - (sell or 0)
            activity = (buy or 0) + (sell or 0)
            by_date.setdefault(date, []).append({"net": net, "activity": activity})
        for date, branches in by_date.items():
            if not branches:
                continue
            # broker_branch_net_buy：當日單一分點淨買賣力道最大(絕對值)的那一個
            # 分點，代表「最顯著的單一主力分點」當天的真實買賣超(張)。
            top_single = max(branches, key=lambda item: abs(item["net"]))
            rows[date]["broker_branch_net_buy"] = top_single["net"] / 1000
            # main_force_buy_sell：依當日成交量(買+賣)排序，取前5大分點，加總
            # 淨買賣超，代表「集合主力分點」當天的真實買賣超(張)。這個前5大
            # 分點定義是我方法論上的簡化選擇，不是 FinMind 官方分類，可再調整。
            top_branches = sorted(branches, key=lambda item: item["activity"], reverse=True)[: self.BROKER_BRANCH_TOP_N]
            rows[date]["main_force_buy_sell"] = sum(item["net"] for item in top_branches) / 1000
            rows[date]["branch_flow_source"] = source

    def merge_intraday_confirmation(self, rows, symbol, start, end, token):
        # 即時資金流/大單流向(realtime_money_flow/realtime_large_order_flow)
        # 只由永豐 Shioaji 即時 tick 訂閱收集器(realtime_tick_collector.py，
        # 獨立常駐子程序)寫入 realtime_flow_staging 這個獨立暫存表，這裡單純
        # 讀出來 merge 回 rows；跟 prices 主表分開儲存，是為了不讓每日批次
        # update_prices 的 INSERT OR REPLACE 把收集器當天已經寫進去的真實資料
        # 覆蓋成 NULL。
        try:
            with self.connect() as conn:
                conn.row_factory = sqlite3.Row
                staged = conn.execute(
                    "SELECT * FROM realtime_flow_staging WHERE symbol = ? AND date >= ? AND date <= ?",
                    (symbol, start or "0000-00-00", end or "9999-99-99"),
                ).fetchall()
        except sqlite3.OperationalError:
            staged = []
        for item in staged:
            date = item["date"]
            if date not in rows:
                continue
            # 收集器當天完全沒有收到任何 tick(tick_count<=0，例如沒開盤、訂閱
            # 失敗、子程序沒啟動)時，不能把 0 當成「真的沒有資金流」寫進模型
            # 特徵，要留 None，讓 model_data_quality 能正確算出 coverage 不足，
            # 而不是用「看起來像真實零訊號」的假 0 頂著。
            if not item["tick_count"]:
                continue
            rows[date]["realtime_money_flow"] = item["realtime_money_flow"]
            rows[date]["realtime_large_order_flow"] = item["realtime_large_order_flow"]
            rows[date]["realtime_flow_source"] = item["source"]

    def inactive_symbol_periods(self, conn=None):
        def load(active_conn):
            active_conn.row_factory = sqlite3.Row
            rows = active_conn.execute("""
                SELECT symbol, inactive_from, inactive_to, status, name, source, source_url
                FROM market_symbol_inactive_periods
                ORDER BY symbol, inactive_from
            """).fetchall()
            periods = {}
            for row in rows:
                periods.setdefault(str(row["symbol"]), []).append(dict(row))
            return periods

        if conn is not None:
            return load(conn)
        with self.connect() as own_conn:
            return load(own_conn)

    @staticmethod
    def symbol_inactive_on(symbol, date_value, periods):
        code = str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
        date_text = str(date_value or "")[:10]
        if not code or len(date_text) != 10:
            return False
        for period in (periods or {}).get(code, []):
            start = str(period.get("inactive_from") or "")[:10]
            end = str(period.get("inactive_to") or "")[:10]
            if start and date_text >= start and (not end or date_text <= end):
                return True
        return False

    def filter_active_symbols(self, symbols, on_date=None):
        date_text = str(on_date or today_key())[:10]
        periods = self.inactive_symbol_periods()
        return [
            str(symbol)
            for symbol in symbols or []
            if not self.symbol_inactive_on(symbol, date_text, periods)
        ]

    def current_official_active_symbols(self):
        records = []
        errors = []
        sources = (
            (
                TWSE_ACTIVE_COMPANIES_URL, "TWSE", "公司代號", "公司簡稱",
                "上市日期", "出表日期", "TWSE OpenAPI t187ap03_L",
            ),
            (
                TPEX_ACTIVE_COMPANIES_URL, "TPEx", "SecuritiesCompanyCode",
                "CompanyAbbreviation", "DateOfListing", "Date",
                "TPEx OpenAPI mopsfin_t187ap03_O",
            ),
        )
        observed_at = now_text()
        for url, market, symbol_key, name_key, listing_key, date_key, source in sources:
            try:
                data = self.fetch_openapi_json(url)
                for item in data:
                    symbol = str(item.get(symbol_key) or "").strip()
                    if not re.fullmatch(r"\d{4}", symbol):
                        continue
                    raw_evidence_date = str(item.get(date_key) or "").strip()
                    evidence_date = self.roc_date_to_iso(raw_evidence_date)
                    if not evidence_date and re.fullmatch(r"\d{8}", raw_evidence_date):
                        evidence_date = (
                            f"{int(raw_evidence_date[:3]) + 1911:04d}-"
                            f"{raw_evidence_date[3:5]}-{raw_evidence_date[5:7]}"
                        )
                    raw_listing_date = str(item.get(listing_key) or "").strip()
                    listing_date = ""
                    if re.fullmatch(r"\d{8}", raw_listing_date):
                        listing_year = int(raw_listing_date[:4])
                        if listing_year < 1911:
                            listing_year += 1911
                        listing_date = (
                            f"{listing_year:04d}-{raw_listing_date[4:6]}-"
                            f"{raw_listing_date[6:8]}"
                        )
                    records.append((
                        symbol, market, str(item.get(name_key) or "").strip(),
                        listing_date, evidence_date, source, observed_at,
                    ))
            except Exception as exc:
                errors.append(f"{market}: {exc}")
        unique_records = {row[0]: row for row in records}
        if len(unique_records) >= RADAR_COMPLETE_DAILY_MIN_ROWS:
            with self.connect() as conn:
                conn.execute("DELETE FROM official_active_symbols")
                conn.executemany("""
                    INSERT INTO official_active_symbols (
                        symbol, market, name, listing_date, evidence_date,
                        source, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, unique_records.values())
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            cached_rows = conn.execute(
                "SELECT * FROM official_active_symbols ORDER BY symbol"
            ).fetchall()
        if len(cached_rows) < RADAR_COMPLETE_DAILY_MIN_ROWS:
            raise RuntimeError(
                f"current official active company universe incomplete: {len(cached_rows)} symbols; "
                + " | ".join(errors[:3])
            )
        active = {str(row["symbol"]) for row in cached_rows}
        evidence_dates = [str(row["evidence_date"] or "") for row in cached_rows]
        return {
            "symbols": active,
            "count": len(active),
            "evidenceDate": max(evidence_dates, default=""),
            "errors": errors,
        }

    def refresh_twse_delisted_periods(self, force=False):
        today = today_key()
        if not force:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM model_meta WHERE key = 'last_twse_delisted_refresh_date'"
                ).fetchone()
                if row and str(row[0]) == today:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM market_symbol_inactive_periods WHERE status = 'delisted'"
                    ).fetchone()[0]
                    return {"ok": True, "cached": True, "date": today, "count": int(count or 0)}

        request = Request(
            TWSE_DELISTED_URL,
            headers={"User-Agent": "Mozilla/5.0 StockAI/9.9"},
        )
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
        if str(payload.get("status") or "").lower() != "ok":
            raise RuntimeError(str(payload.get("message") or "TWSE delisted list unavailable"))

        active_universe = self.current_official_active_symbols()
        active_symbols = set(active_universe["symbols"])
        observed_at = now_text()
        records = []
        source_total = 0
        active_excluded = []
        for item in payload.get("data") or []:
            if not isinstance(item, list) or len(item) < 3:
                continue
            roc_date, name, symbol = (str(item[0]).strip(), str(item[1]).strip(), str(item[2]).strip())
            match = re.fullmatch(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", roc_date)
            if not match or not symbol:
                continue
            source_total += 1
            if symbol in active_symbols:
                active_excluded.append(symbol)
                continue
            year, month, day = (int(value) for value in match.groups())
            inactive_from = f"{year + 1911:04d}-{month:02d}-{day:02d}"
            records.append((
                symbol, inactive_from, "delisted", name,
                "TWSE official terminated listing list", TWSE_DELISTED_URL, observed_at,
            ))
        if not records:
            raise RuntimeError("TWSE delisted list returned no usable records")

        with self.connect() as conn:
            for offset in range(0, len(active_symbols), 400):
                chunk = sorted(active_symbols)[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"""
                    DELETE FROM market_symbol_inactive_periods
                    WHERE status = 'delisted' AND symbol IN ({placeholders})
                """, chunk)
            conn.executemany("""
                INSERT INTO market_symbol_inactive_periods (
                    symbol, inactive_from, inactive_to, status, name,
                    source, source_url, observed_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, inactive_from) DO UPDATE SET
                    status = excluded.status,
                    name = excluded.name,
                    source = excluded.source,
                    source_url = excluded.source_url,
                    observed_at = excluded.observed_at
            """, records)
            self.set_meta(conn, "last_twse_delisted_refresh_date", today)
            self.set_meta(conn, "last_twse_delisted_refresh_at", observed_at)
            self.set_meta(conn, "last_twse_delisted_refresh_count", str(len(records)))
            self.set_meta(conn, "last_twse_delisted_source_total", str(source_total))
            self.set_meta(conn, "last_twse_delisted_active_excluded", str(len(active_excluded)))
            self.set_meta(conn, "last_official_active_universe_count", str(active_universe["count"]))
            self.set_meta(conn, "last_official_active_universe_date", active_universe["evidenceDate"])
        return {
            "ok": True,
            "cached": False,
            "date": today,
            "count": len(records),
            "sourceTotal": source_total,
            "activeExcluded": len(active_excluded),
            "activeUniverseCount": active_universe["count"],
            "activeEvidenceDate": active_universe["evidenceDate"],
        }

    def cleanup_invalid_production_data(self, cleanup_key="official-inactive-and-test-v1"):
        """Archive then remove rows that cannot represent a real market session."""
        archived_at = now_text()
        deleted = {}
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")

            def archive_and_delete(table, where_sql, reason, params=()):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    return 0
                rows = conn.execute(
                    f"SELECT rowid AS _audit_rowid, * FROM {table} WHERE {where_sql}",
                    params,
                ).fetchall()
                row_ids = []
                for row in rows:
                    payload = dict(row)
                    row_ids.append(int(payload.pop("_audit_rowid")))
                    payload_json = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
                    )
                    row_key = hashlib.sha256(
                        f"{table}|{payload_json}".encode("utf-8")
                    ).hexdigest()
                    conn.execute("""
                        INSERT OR IGNORE INTO data_cleanup_audit (
                            cleanup_key, table_name, row_key, reason, source,
                            payload_json, archived_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        cleanup_key, table, row_key, reason,
                        "TWSE official inactive periods / production data policy",
                        payload_json, archived_at,
                    ))
                if row_ids:
                    conn.executemany(
                        f"DELETE FROM {table} WHERE rowid = ?",
                        [(row_id,) for row_id in row_ids],
                    )
                return len(row_ids)

            inactive_match = """
                EXISTS (
                    SELECT 1 FROM market_symbol_inactive_periods inactive
                    WHERE inactive.symbol = {alias}.symbol
                      AND {date_expr} >= inactive.inactive_from
                      AND (
                            inactive.inactive_to IS NULL OR inactive.inactive_to = ''
                            OR {date_expr} <= inactive.inactive_to
                      )
                )
            """
            targets = (
                ("prices", "prices", "prices.date"),
                ("predictions", "predictions", "predictions.price_date"),
                ("strategy_signals", "strategy_signals", "strategy_signals.signal_date"),
                ("brain_v2_snapshots", "brain_v2_snapshots", "brain_v2_snapshots.price_date"),
                ("monster_scores", "monster_scores", "COALESCE(NULLIF(monster_scores.price_date, ''), monster_scores.scan_date)"),
                ("model_experiment_predictions", "model_experiment_predictions", "model_experiment_predictions.signal_date"),
            )
            for table, alias, date_expr in targets:
                where_sql = inactive_match.format(alias=alias, date_expr=date_expr)
                deleted[table] = archive_and_delete(
                    table,
                    where_sql,
                    "row date falls inside an official inactive trading period",
                )

            reserved_placeholders = ",".join("?" for _ in RESERVED_PRODUCTION_STRATEGIES)
            reserved_params = tuple(sorted(RESERVED_PRODUCTION_STRATEGIES))
            deleted["strategy_signals_reserved"] = archive_and_delete(
                "strategy_signals",
                f"LOWER(TRIM(strategy)) IN ({reserved_placeholders})",
                "reserved test strategy was written into production signals",
                reserved_params,
            )
            deleted["strategy_calibration_reserved"] = archive_and_delete(
                "strategy_calibration",
                f"LOWER(TRIM(strategy)) IN ({reserved_placeholders})",
                "reserved test strategy was written into production calibration",
                reserved_params,
            )
            total = sum(deleted.values())
            summary = {
                "ok": True,
                "cleanupKey": cleanup_key,
                "archivedAt": archived_at,
                "deleted": deleted,
                "totalDeleted": total,
            }
            self.set_meta(conn, "last_invalid_production_cleanup_at", archived_at)
            self.set_meta(
                conn,
                "last_invalid_production_cleanup_json",
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            )
            if deleted.get("prices"):
                self.set_meta(conn, "model_retrain_required_after_cleanup", "1")

        if deleted.get("prices"):
            with self._predict_cache_lock:
                self._predict_cache.clear()
                self._predict_cache_gen += 1
                self._latest_complete_price_date_cache["at"] = 0.0
        return summary

    def restore_quarantined_active_symbol_rows(
        self, active_symbols, cleanup_key="official-inactive-and-test-v1",
        evidence_date="", evidence_source="TWSE / TPEx current official universe",
    ):
        """Restore rows quarantined only because a historical code is active again."""
        active = {str(symbol) for symbol in active_symbols or []}
        allowed_tables = {
            "prices", "predictions", "strategy_signals", "brain_v2_snapshots",
            "monster_scores", "model_experiment_predictions",
        }
        restored_at = now_text()
        restored = {table: 0 for table in sorted(allowed_tables)}
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            audits = conn.execute("""
                SELECT id, table_name, payload_json
                FROM data_cleanup_audit
                WHERE cleanup_key = ?
                  AND reason = 'row date falls inside an official inactive trading period'
                ORDER BY id
            """, (cleanup_key,)).fetchall()
            table_columns = {}
            for audit in audits:
                table = str(audit["table_name"] or "")
                if table not in allowed_tables:
                    continue
                try:
                    payload = json.loads(audit["payload_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                symbol = str(payload.get("symbol") or "")
                if symbol not in active:
                    continue
                if table not in table_columns:
                    table_columns[table] = [
                        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    ]
                columns = [column for column in table_columns[table] if column in payload]
                if not columns:
                    continue
                placeholders = ",".join("?" for _ in columns)
                column_sql = ",".join(f'"{column}"' for column in columns)
                cursor = conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})",
                    [payload.get(column) for column in columns],
                )
                if int(cursor.rowcount or 0) <= 0:
                    continue
                restored[table] += 1
                conn.execute("""
                    INSERT OR IGNORE INTO data_cleanup_restore_audit (
                        original_audit_id, cleanup_key, table_name, symbol,
                        restored_at, reason, active_evidence_date, active_evidence_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    int(audit["id"]), cleanup_key, table, symbol, restored_at,
                    "historical terminated-list code is present in current official market universe",
                    str(evidence_date or "")[:10], evidence_source,
                ))
            total = sum(restored.values())
            summary = {
                "ok": True,
                "cleanupKey": cleanup_key,
                "restoredAt": restored_at,
                "restored": restored,
                "totalRestored": total,
                "activeUniverseCount": len(active),
                "evidenceDate": str(evidence_date or "")[:10],
            }
            self.set_meta(
                conn,
                "last_cleanup_active_symbol_restore_json",
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            )
        if restored.get("prices"):
            with self._predict_cache_lock:
                self._predict_cache.clear()
                self._predict_cache_gen += 1
                self._latest_complete_price_date_cache["at"] = 0.0
        return summary

    def upsert_price_rows(self, rows, conn=None):
        row_list = list(rows or [])
        if not row_list:
            return 0

        columns = [
            "symbol", "date", "open", "high", "low", "close", "volume",
            "foreign_buy_sell", "trust_buy_sell", "margin_balance", "short_balance",
            "monthly_revenue", "revenue_growth", "per", "pbr", "dividend_yield",
            "day_trade_ratio", "day_trade_buy_sell_imbalance",
            "securities_lending_volume", "securities_lending_fee_rate",
            "large_investor_buy_sell", "retail_investor_buy_sell",
            "broker_branch_net_buy", "main_force_buy_sell",
            "realtime_money_flow", "realtime_large_order_flow",
            "gross_margin", "operating_margin", "roe", "debt_ratio",
            "operating_cashflow_ratio",
            "price_source", "chip_source", "margin_source",
            "ownership_flow_source", "retail_flow_source", "branch_flow_source", "realtime_flow_source",
            "revenue_source", "valuation_source", "financial_statement_source", "finance_source",
            "updated_at",
        ]
        required = ("symbol", "date", "open", "high", "low", "close", "volume")
        price_fields = ("open", "high", "low", "close")

        def _is_positive_price(value):
            try:
                return float(value) > 0
            except (TypeError, ValueError):
                return False

        update_assignments = []
        for column in columns:
            if column in {"symbol", "date"}:
                continue
            if column == "updated_at":
                update_assignments.append("updated_at = excluded.updated_at")
            else:
                update_assignments.append(f"{column} = COALESCE(excluded.{column}, prices.{column})")
        sql = f"""
            INSERT INTO prices ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(symbol, date) DO UPDATE SET
                {", ".join(update_assignments)}
        """

        def values_for(row):
            finance_source = self.finance_source_for_row(row) or row.get("finance_source")
            enriched = dict(row)
            enriched["finance_source"] = finance_source
            enriched["updated_at"] = now_text()
            return tuple(enriched.get(column) for column in columns)

        inactive_periods = self.inactive_symbol_periods(conn)
        valid_rows = [
            row for row in row_list
            if all(row.get(column) is not None for column in required)
            and all(_is_positive_price(row.get(column)) for column in price_fields)
            and not self.symbol_inactive_on(row.get("symbol"), row.get("date"), inactive_periods)
        ]
        if not valid_rows:
            return 0

        if conn is not None:
            conn.executemany(sql, [values_for(row) for row in valid_rows])
            self._invalidate_predict_cache(valid_rows)
            return len(valid_rows)

        with self.connect() as own_conn:
            own_conn.executemany(sql, [values_for(row) for row in valid_rows])
        self._invalidate_predict_cache(valid_rows)
        return len(valid_rows)

    def _invalidate_predict_cache(self, rows):
        # 寫入了新的日K資料，這些股票快取中的預測結果就過時了。
        # 同時推進 generation：即使 pop 時目標股票的快取還沒被寫入(執行緒A
        # 正在計算、尚未寫回)，gen 前進也會讓A在寫回前發現「計算期間有失效
        # 發生」而放棄寫回，堵住 lost-invalidation 競態。這裡可能在
        # DB_WRITE_LOCK 內被呼叫(upsert_price_rows conn is not None 分支)，
        # 但只取 _predict_cache_lock、不再碰 DB，不會跟 predict_symbol 的
        # 「先放鎖再寫DB」形成環路。
        symbols = {row.get("symbol") for row in rows}
        with self._predict_cache_lock:
            for symbol in symbols:
                if symbol:
                    self._predict_cache.pop(symbol, None)
            self._predict_cache_gen += 1
            self._latest_complete_price_date_cache["at"] = 0.0

    def sync_official_daily_snapshot(self, symbols=None):
        rows, errors = self.fetch_official_daily_snapshot_rows()
        requested = {str(symbol).replace(".TWO", "").replace(".TW", "").strip() for symbol in symbols or [] if str(symbol).strip()}
        if requested:
            rows = {symbol: row for symbol, row in rows.items() if symbol in requested}
        latest_dates = {}
        for row in rows.values():
            latest_dates[row["date"]] = latest_dates.get(row["date"], 0) + 1
        market_type_updates = []
        for symbol, row in rows.items():
            source = str(row.get("price_source") or "").lower()
            if "tpex_esb" in source:
                market_type = "emerging"
            elif "tpex" in source:
                market_type = "tpex"
            elif "twse" in source:
                market_type = "twse"
            else:
                continue
            market_type_updates.append((market_type, now_text(), symbol, market_type))

        with self.connect() as conn:
            written = self.upsert_price_rows(rows.values(), conn)
            market_type_cursor = conn.executemany(
                """
                UPDATE stock_info
                SET market_type = ?, updated_at = ?
                WHERE symbol = ?
                  AND LOWER(COALESCE(market_type, '')) != ?
                """,
                market_type_updates,
            ) if market_type_updates else None
            market_types_updated = max(0, int(getattr(market_type_cursor, "rowcount", 0) or 0))
            self.set_meta(conn, "last_official_daily_snapshot_sync", now_text())
            self.set_meta(conn, "last_official_daily_snapshot_available", str(len(rows)))
            self.set_meta(conn, "last_official_daily_snapshot_written", str(written))
            self.set_meta(conn, "last_official_daily_snapshot_errors", " | ".join(errors[:5]))
        self._official_latest_cache = None
        self._official_latest_cache_date = ""
        return {
            "ok": not errors or bool(rows),
            "available": len(rows),
            "written": written,
            "marketTypesUpdated": market_types_updated,
            "latestDates": latest_dates,
            "errors": errors,
        }

    def update_prices(self, symbols=None, refresh_info=True, include_extended=False, force_refresh=False):
        symbols = symbols or DEFAULT_SYMBOLS
        counts = {}
        fetch_errors = {}
        if refresh_info:
            self.update_stock_info(symbols)
        for symbol in symbols:
            # force_refresh=True 會跳過 load_cached_symbol_rows_if_fresh 的「今天已更新過
            # 就直接回傳快取」短路判斷，強制重新打 FinMind，讓同一天內想要重訓也能拿到
            # 最新資料，而不用等到隔天快取自然失效。
            # 逐股隔離：單一股票抓取/寫入失敗(額度、網路、單檔壞資料)只記錄錯誤
            # 繼續下一檔，不能讓一檔壞掉拖垮整批更新(=整個每日更新)。
            try:
                rows = self.fetch_symbol_rows(symbol, include_extended=include_extended, use_cache=not force_refresh)
                if not rows:
                    counts[symbol] = 0
                    continue
                with self.connect() as conn:
                    counts[symbol] = self.upsert_price_rows(rows, conn)
            except Exception as exc:
                counts[symbol] = 0
                fetch_errors[symbol] = str(exc)
        # 不管成敗都先更新錯誤紀錄：若放在 raise 之後，全失敗時屬性/meta 會
        # 殘留「上一輪」的過期內容，事後查錯會被誤導。
        self._last_price_fetch_errors = fetch_errors
        # 全部失敗代表這輪更新完全沒有拿到任何新資料(例如額度整段封鎖、斷網)，
        # 這時要讓呼叫端知道整體失敗，而不是回傳一個全為 0 的 counts 假裝成功。
        # 用 set() 比對：symbols 若含重複代碼，len(symbols) 會大於去重後的
        # fetch_errors key 數，全失敗判斷會漏掉。
        if symbols and fetch_errors and len(fetch_errors) >= len(set(symbols)):
            first_error = next(iter(fetch_errors.values()))
            raise RuntimeError(f"全部 {len(set(symbols))} 檔更新失敗，第一個錯誤：{first_error}")
        with self.connect() as conn:
            self.set_meta(conn, "last_data_update", now_text())
            self.set_meta(conn, "last_price_fetch_errors", json.dumps(
                {key: value[:200] for key, value in list(fetch_errors.items())[:30]}, ensure_ascii=False
            ))
        return counts

    def finance_source_for_row(self, row):
        sources = []
        for key in ("revenue_source", "valuation_source", "financial_statement_source"):
            value = str(row.get(key) or "").strip()
            if value and value not in sources:
                sources.append(value)
        return " | ".join(sources) if sources else None

    def price_rows_memo(self):
        """回傳一個 context manager,包住的區塊內對同一檔的 load_price_rows 只讀一次
        DB(見 _PriceRowsMemoScope)。給 build_brain_decision 用。"""
        return _PriceRowsMemoScope(self)

    def load_price_rows(self, symbol):
        # 請求內 memo(僅在 price_rows_memo() 範圍內生效):同一檔在一次 brain 判斷裡
        # 會被讀兩次,memo 讓第二次直接回傳第一次的結果。範圍外 memo 為 None,行為
        # 與原本完全一致(每次都新讀 DB、回傳全新的 dict list)。
        memo = getattr(self._price_rows_memo, "cache", None)
        if memo is not None and symbol in memo:
            return memo[symbol]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM prices WHERE symbol = ? ORDER BY date", (symbol,)).fetchall()
        result = [dict(row) for row in rows]
        if memo is not None:
            memo[symbol] = result
        return result

    def rows_with_verified_sources(self, rows):
        verified = []
        for row in rows:
            price_source = str(row.get("price_source") or "").strip()
            if not is_official_source(price_source):
                continue
            verified.append(row)
        return verified

    def rule_analysis_data_quality(
        self, symbol, rows, min_price_rows=60, expected_latest_date=""
    ):
        """正式規則分析的資料門檻，不沿用模型訓練所需的財務覆蓋率。"""
        rows = rows or []
        latest = rows[-1] if rows else {}
        required_rows = max(20, int(min_price_rows or 60))
        recent = rows[-required_rows:] if rows else []
        complete_rows = [
            row for row in recent
            if all(row.get(key) is not None for key in ("open", "high", "low", "close", "volume"))
        ]
        checks = {
            "priceRowsEnough": len(rows) >= required_rows,
            # 新上市股票只有 60 根時，只應缺 priceRowsEnough；現有 60 根若
            # OHLCV 都完整，不該再額外誤報 recentPriceComplete 並反覆補抓。
            "recentPriceComplete": len(complete_rows) >= min(len(recent), required_rows),
            "latestPriceSourceOfficial": is_official_source(latest.get("price_source")),
        }
        expected_latest_date = str(expected_latest_date or "")[:10]
        latest_date = str(latest.get("date") or "")[:10]
        if expected_latest_date:
            checks["latestPriceFresh"] = bool(
                latest_date and latest_date >= expected_latest_date
            )
        missing = [key for key, ok in checks.items() if not ok]
        return {
            "symbol": symbol,
            "ok": not missing,
            "missing": missing,
            "rows": len(rows),
            "recentRows": len(recent),
            "latestDate": latest.get("date"),
            "expectedLatestDate": expected_latest_date or None,
            "priceSource": latest.get("price_source"),
        }

    def ensure_rule_analysis_rows(self, symbol, repair=True, min_price_rows=120):
        rows = self.rows_with_verified_sources(self.load_price_rows(symbol))
        try:
            expected_latest_date = self.latest_complete_price_date()
        except Exception:
            expected_latest_date = ""
        quality = self.rule_analysis_data_quality(
            symbol,
            rows,
            min_price_rows=min_price_rows,
            expected_latest_date=expected_latest_date,
        )
        # 上市未滿 required_rows 的股票無法靠重抓「創造」不存在的歷史資料；
        # 其餘缺口（沒有資料、OHLCV 不完整、來源不可信、落後全市場日期）才是
        # 可修復項目。force_refresh=True 很重要，否則今天曾抓過但仍過期的快取
        # 會直接短路，表面上執行補抓，實際上完全沒有碰資料源。
        repairable_missing = set(quality.get("missing") or []) - {"priceRowsEnough"}
        should_repair = not rows or bool(repairable_missing)
        if repair and not quality["ok"] and should_repair:
            try:
                self.update_prices(
                    [symbol],
                    refresh_info=False,
                    force_refresh=True,
                )
            except Exception as exc:
                print(f"rule analysis repair for {symbol} failed, use existing rows: {exc}")
                quality["repairAttempted"] = True
                quality["repairError"] = str(exc)
                return rows, quality
            rows = self.rows_with_verified_sources(self.load_price_rows(symbol))
            quality = self.rule_analysis_data_quality(
                symbol,
                rows,
                min_price_rows=min_price_rows,
                expected_latest_date=expected_latest_date,
            )
            quality["repairAttempted"] = True
        else:
            quality["repairAttempted"] = False
        return rows, quality

    def model_data_quality(self, symbol, rows):
        rows = rows or []
        latest = rows[-1] if rows else {}
        recent = rows[-MODEL_RECENT_WINDOW:] if rows else []
        price_complete = [
            row for row in recent
            if all(row.get(key) is not None for key in ("open", "high", "low", "close", "volume"))
        ]
        chip_fields = ("foreign_buy_sell", "trust_buy_sell", "margin_balance", "short_balance")
        finance_fields = ("monthly_revenue", "revenue_growth", "per", "pbr", "gross_margin")
        advanced_flow_source_pairs = (
            ("broker_branch_net_buy", "branch_flow_source"),
            ("main_force_buy_sell", "branch_flow_source"),
            ("realtime_money_flow", "realtime_flow_source"),
            ("realtime_large_order_flow", "realtime_flow_source"),
        )
        chip_values = sum(1 for row in recent for key in chip_fields if row.get(key) is not None)
        finance_values = sum(1 for row in recent for key in finance_fields if row.get(key) is not None)
        advanced_flow_values = sum(
            1
            for row in recent
            for key, source_key in advanced_flow_source_pairs
            if row.get(key) is not None and is_official_source(row.get(source_key))
        )
        chip_total = max(len(recent) * len(chip_fields), 1)
        finance_total = max(len(recent) * len(finance_fields), 1)
        advanced_flow_total = max(len(recent) * len(advanced_flow_source_pairs), 1)
        chip_coverage = chip_values / chip_total
        finance_coverage = finance_values / finance_total
        advanced_flow_coverage = advanced_flow_values / advanced_flow_total
        row_total = max(len(recent), 1)
        chip_source_coverage = sum(
            1 for row in recent
            if is_official_source(row.get("chip_source")) and (
                row.get("foreign_buy_sell") is not None or row.get("trust_buy_sell") is not None
            )
        ) / row_total
        margin_source_coverage = sum(
            1 for row in recent
            if is_official_source(row.get("margin_source")) and (
                row.get("margin_balance") is not None or row.get("short_balance") is not None
            )
        ) / row_total
        finance_source_coverage = sum(
            1 for row in recent
            if is_official_source(row.get("finance_source")) and any(row.get(key) is not None for key in finance_fields)
        ) / row_total
        advanced_flow_source_coverage = sum(
            1 for row in recent
            if any(
                row.get(key) is not None and is_official_source(row.get(source_key))
                for key, source_key in advanced_flow_source_pairs
            )
        ) / row_total
        checks = {
            "priceRowsEnough": len(rows) >= MODEL_MIN_PRICE_ROWS,
            "recentPriceComplete": len(price_complete) >= min(len(recent), MODEL_MIN_PRICE_ROWS),
            "latestPriceSourceOfficial": is_official_source(latest.get("price_source")),
            "chipCoverageOk": chip_coverage >= MODEL_MIN_CHIP_COVERAGE,
            "chipSourceCoverageOk": chip_source_coverage >= MODEL_MIN_CHIP_COVERAGE,
            "marginSourceCoverageOk": margin_source_coverage >= MODEL_MIN_CHIP_COVERAGE,
            "financeCoverageOk": finance_coverage >= MODEL_MIN_FINANCE_COVERAGE,
            "financeSourceCoverageOk": finance_source_coverage >= MODEL_MIN_FINANCE_COVERAGE,
        }
        missing = [key for key, ok in checks.items() if not ok]
        return {
            "symbol": symbol,
            "ok": not missing,
            "missing": missing,
            "rows": len(rows),
            "recentRows": len(recent),
            "chipCoverage": chip_coverage,
            "chipSourceCoverage": chip_source_coverage,
            "marginSourceCoverage": margin_source_coverage,
            "financeCoverage": finance_coverage,
            "financeSourceCoverage": finance_source_coverage,
            "advancedFlowCoverage": advanced_flow_coverage,
            "advancedFlowSourceCoverage": advanced_flow_source_coverage,
            "latestDate": latest.get("date"),
            "priceSource": latest.get("price_source"),
            "chipSource": latest.get("chip_source"),
            "marginSource": latest.get("margin_source"),
            "financeSource": latest.get("finance_source"),
            "branchFlowSource": latest.get("branch_flow_source"),
            "realtimeFlowSource": latest.get("realtime_flow_source"),
        }

    def ensure_model_ready_rows(self, symbol, repair=True):
        rows = self.rows_with_verified_sources(self.load_price_rows(symbol))
        quality = self.model_data_quality(symbol, rows)
        if repair and not quality["ok"]:
            # 補抓失敗(額度封鎖、網路斷線)不能往外拋——訓練/預測迴圈會對數百檔
            # 股票各呼叫一次這裡，任何一檔的網路失敗若直接炸出去，整個 20 分鐘
            # 的訓練就全部白跑。補不到就用手上既有的資料，讓下面的品質檢查
            # 決定這檔要不要跳過。
            try:
                self.update_prices([symbol], refresh_info=False)
            except Exception as exc:
                # 至少留下一行可觀測的紀錄，不然「資料不完整」的表象會蓋掉
                # 「其實是額度/網路補不到」的根因，除錯要多繞一圈。
                print(f"repair fetch for {symbol} failed, fall back to existing rows: {exc}")
                return rows, quality
            rows = self.rows_with_verified_sources(self.load_price_rows(symbol))
            quality = self.model_data_quality(symbol, rows)
        return rows, quality

    def market_data_quality(self, latest_date):
        market_rows = self.load_market_rows()
        context = MarketContext(market_rows)
        required = {
            "TAIEX": 20,
            "OTC": 20,
            "NASDAQ": 1,
            "SP500": 1,
            "USDTWD": 20,
        }
        missing = []
        for key, lookback in required.items():
            if context.close(key, latest_date) <= 0 or context.close(key, latest_date, lookback) <= 0:
                missing.append(key)
                continue
            # 只檢查收盤價>0 沒辦法抓到「這個來源已經好幾天沒更新，但舊資料
            # 剛好還是正數」的情況——bisect 只會往回找最近一筆<=latest_date
            # 的資料，資料再舊也一樣回傳。這裡額外比對「找到的那筆資料」跟
            # 查詢日期實際差了幾天，超過合理範圍就視為過期，不能直接當新鮮
            # 資料餵給 market_gate/adjust_probability_for_market。
            available_date = context.latest_available_date(key, latest_date)
            if available_date and calendar_days_between(available_date, latest_date) > MARKET_DATA_MAX_STALE_DAYS:
                missing.append(f"{key}(stale since {available_date})")
        return {
            "ok": not missing,
            "missing": missing,
            "latestDate": latest_date,
        }

    def latest_source_status(self, symbol):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT date, close, volume, price_source, chip_source, finance_source, updated_at
                FROM prices
                WHERE symbol = ?
                ORDER BY date DESC
                LIMIT 1
            """, (symbol,)).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM prices WHERE symbol = ?", (symbol,)).fetchone()[0]
            verified_count = conn.execute("""
                SELECT COUNT(*) FROM prices
                WHERE symbol = ?
                  AND price_source IS NOT NULL
                  AND price_source != ''
            """, (symbol,)).fetchone()[0]
        if not row:
            return {
                "symbol": symbol,
                "count": int(count or 0),
                "verifiedCount": int(verified_count or 0),
                "ok": False,
                "reason": "missing_daily_rows",
            }
        price_source = row["price_source"]
        ok = is_official_source(price_source)
        return {
            "symbol": symbol,
            "count": int(count or 0),
            "verifiedCount": int(verified_count or 0),
            "ok": ok,
            "reason": "" if ok else "missing_verified_price_source",
            "date": row["date"],
            "close": row["close"],
            "volume": row["volume"],
            "priceSource": price_source,
            "chipSource": row["chip_source"],
            "financeSource": row["finance_source"],
            "updatedAt": row["updated_at"],
        }

    def repair_before_scan(self, symbols=None, max_repair=300, progress_callback=None):
        if symbols:
            universe = [str(symbol).replace(".TWO", "").replace(".TW", "").strip() for symbol in symbols if str(symbol).strip()]
            universe = list(dict.fromkeys(symbol for symbol in universe if symbol.isdigit()))
        else:
            universe = self.listed_symbols()
        watch = [symbol for symbol in sorted(MONSTER_WATCH_SYMBOLS) if symbol not in universe]
        universe = list(dict.fromkeys([*watch, *universe]))
        max_repair = max(1, min(int(max_repair or 300), 1200))
        checked = 0
        needs_repair = []
        for symbol in universe:
            checked += 1
            status = self.latest_source_status(symbol)
            if not status["ok"] or symbol in MONSTER_WATCH_SYMBOLS:
                needs_repair.append({"symbol": symbol, "before": status})
            if len([item for item in needs_repair if item["symbol"] not in MONSTER_WATCH_SYMBOLS]) >= max_repair:
                break
        repaired = []
        failed = []
        total = len(needs_repair)
        for index, item in enumerate(needs_repair, start=1):
            symbol = item["symbol"]
            if progress_callback:
                progress_callback({
                    "phase": "資料補齊",
                    "total": total,
                    "processed": index - 1,
                    "saved": len(repaired),
                    "errors": len(failed),
                    "current": symbol,
                    "message": f"補齊正式資料來源 {index}/{total}",
                })
            try:
                counts = self.update_prices([symbol], refresh_info=False)
                after = self.latest_source_status(symbol)
                if after["ok"]:
                    repaired.append({
                        "symbol": symbol,
                        "rows": counts.get(symbol, 0),
                        "priceSource": after.get("priceSource"),
                        "date": after.get("date"),
                    })
                else:
                    failed.append({
                        "symbol": symbol,
                        "reason": after.get("reason") or "still_missing_verified_source",
                        "priceSource": after.get("priceSource"),
                    })
            except Exception as exc:
                failed.append({"symbol": symbol, "reason": str(exc)})
            if progress_callback:
                progress_callback({
                    "phase": "資料補齊",
                    "total": total,
                    "processed": index,
                    "saved": len(repaired),
                    "errors": len(failed),
                    "current": symbol,
                    "message": f"已補齊 {len(repaired)} 檔，失敗 {len(failed)} 檔",
                })
        return {
            "ok": True,
            "checked": checked,
            "requested": len(universe),
            "repairCandidates": total,
            "repaired": repaired,
            "failed": failed,
            "watchSymbols": sorted(MONSTER_WATCH_SYMBOLS),
        }

    def listed_symbols(self, limit=None):
        info = self.load_stock_info()
        if not info:
            info = self.update_stock_info([])
        inactive_periods = self.inactive_symbol_periods()
        today = today_key()
        output = []
        for symbol, item in info.items():
            market_type = str(item.get("market_type") or "").lower()
            if not symbol.isdigit() or len(symbol) != 4:
                continue
            if self.symbol_inactive_on(symbol, today, inactive_periods):
                continue
            if is_etf_like_stock(symbol, item.get("name"), item.get("sector"), market_type):
                continue
            if any(text in market_type for text in ("etf", "權證", "warrant", "受益", "index")):
                continue
            output.append(symbol)
        output.sort()
        return output[:limit] if limit else output

    def liquid_monster_universe(self, limit=None):
        source_ok = """
            (
                LOWER(COALESCE(latest.price_source, '')) LIKE '%twse%' OR
                LOWER(COALESCE(latest.price_source, '')) LIKE '%tpex%' OR
                LOWER(COALESCE(latest.price_source, '')) LIKE '%finmind%' OR
                LOWER(COALESCE(latest.price_source, '')) LIKE '%shioaji%' OR
                LOWER(COALESCE(latest.price_source, '')) LIKE '%sinopac%'
            )
            AND LOWER(COALESCE(latest.price_source, '')) NOT LIKE '%yahoo%'
            AND LOWER(COALESCE(latest.price_source, '')) NOT LIKE '%fallback%'
            AND LOWER(COALESCE(latest.price_source, '')) NOT LIKE '%simulate%'
            AND LOWER(COALESCE(latest.price_source, '')) NOT LIKE '%simulation%'
        """
        max_rows = int(limit or 0)
        sql_limit = "LIMIT ?" if max_rows > 0 else ""
        expected_latest_date = self.latest_complete_price_date()
        latest_date_filter = "AND latest.date = ?" if expected_latest_date else ""
        params = []
        if expected_latest_date:
            params.append(expected_latest_date)
        params.extend([
            MIN_MONSTER_AVG_VOLUME_LOTS,
            MIN_MONSTER_TURNOVER_MILLION,
        ])
        if max_rows > 0:
            params.append(max_rows)
        query = f"""
            WITH ranked AS (
                SELECT
                    p.symbol,
                    p.date,
                    p.close,
                    p.volume,
                    p.price_source,
                    ROW_NUMBER() OVER (PARTITION BY p.symbol ORDER BY p.date DESC) AS rn
                FROM prices p
                WHERE p.symbol GLOB '[0-9][0-9][0-9][0-9]'
            ),
            latest AS (
                SELECT * FROM ranked WHERE rn = 1
            ),
            recent AS (
                SELECT symbol, AVG(volume) / 1000.0 AS avg_volume20_lots
                FROM ranked
                WHERE rn <= 20 AND volume IS NOT NULL
                GROUP BY symbol
            )
            SELECT
                latest.symbol,
                recent.avg_volume20_lots,
                (latest.volume * latest.close) / 1000000.0 AS turnover_million
            FROM latest
            JOIN recent ON recent.symbol = latest.symbol
            LEFT JOIN stock_info si ON si.symbol = latest.symbol
            WHERE {source_ok}
              {latest_date_filter}
              AND latest.close > 0
              AND latest.volume > 0
              AND recent.avg_volume20_lots >= ?
              AND (latest.volume * latest.close) / 1000000.0 >= ?
              AND latest.symbol NOT LIKE '00%'
              AND LOWER(COALESCE(si.name, '')) NOT LIKE '%etf%'
              AND LOWER(COALESCE(si.sector, '')) NOT LIKE '%etf%'
              AND LOWER(COALESCE(si.market_type, '')) NOT LIKE '%etf%'
              AND LOWER(COALESCE(si.market_type, '')) NOT LIKE '%warrant%'
              AND COALESCE(si.name, '') NOT LIKE '%權證%'
              AND COALESCE(si.market_type, '') NOT LIKE '%權證%'
              AND COALESCE(si.market_type, '') NOT LIKE '%受益%'
            ORDER BY turnover_million DESC, recent.avg_volume20_lots DESC
            {sql_limit}
        """
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        inactive_periods = self.inactive_symbol_periods()
        active_date = expected_latest_date or today_key()
        symbols = [
            str(row[0]) for row in rows
            if str(row[0]) not in EXCLUDED_CANDIDATE_SYMBOLS
            and not self.symbol_inactive_on(str(row[0]), active_date, inactive_periods)
        ]
        for watch_symbol in sorted(MONSTER_WATCH_SYMBOLS):
            if watch_symbol not in symbols:
                symbols.append(watch_symbol)
        return symbols

    def intraday_discovery_baselines(self, symbols):
        """Return verified daily baselines for the live whole-market scan.

        One bulk query is used instead of loading a full feature window for
        every stock while the market is open.  Rows not aligned with the latest
        complete official close are excluded from discovery decisions.
        """
        clean_symbols = [
            str(symbol).replace(".TWO", "").replace(".TW", "").strip()
            for symbol in (symbols or [])
        ]
        clean_symbols = list(dict.fromkeys(
            symbol for symbol in clean_symbols
            if symbol.isdigit() and len(symbol) == 4
        ))
        if not clean_symbols:
            return {}
        expected_date = self.latest_complete_price_date()
        output = {}
        # Stay below SQLite builds that retain the traditional 999 variable cap.
        for offset in range(0, len(clean_symbols), 800):
            chunk = clean_symbols[offset:offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            with self.connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(f"""
                    WITH ranked AS (
                        SELECT
                            p.symbol, p.date, p.close, p.volume, p.price_source,
                            ROW_NUMBER() OVER (
                                PARTITION BY p.symbol ORDER BY p.date DESC
                            ) AS rn
                        FROM prices p
                        WHERE p.symbol IN ({placeholders})
                    ), stats AS (
                        SELECT
                            symbol,
                            MAX(CASE WHEN rn = 1 THEN date END) AS price_date,
                            MAX(CASE WHEN rn = 1 THEN close END) AS previous_close,
                            MAX(CASE WHEN rn = 1 THEN price_source END) AS price_source,
                            COUNT(CASE WHEN rn <= 20 THEN 1 END) AS history_days,
                            AVG(CASE WHEN rn <= 20 THEN volume END) / 1000.0
                                AS avg_volume20_lots,
                            AVG(CASE WHEN rn <= 20 THEN volume * close END) / 1000000.0
                                AS avg_turnover20_million
                        FROM ranked
                        WHERE rn <= 20
                        GROUP BY symbol
                    )
                    SELECT
                        stats.symbol, stats.price_date, stats.previous_close,
                        stats.price_source, stats.avg_volume20_lots,
                        stats.avg_turnover20_million, stats.history_days,
                        si.name, si.sector, si.market_type
                    FROM stats
                    LEFT JOIN stock_info si ON si.symbol = stats.symbol
                """, chunk).fetchall()
            for row in rows:
                item = dict(row)
                price_date = str(item.get("price_date") or "")[:10]
                previous_close = self.safe_float(item.get("previous_close"))
                avg_volume20_lots = self.safe_float(item.get("avg_volume20_lots"))
                avg_turnover20_million = self.safe_float(item.get("avg_turnover20_million"))
                if expected_date and price_date != expected_date:
                    continue
                if previous_close is None or previous_close <= 0:
                    continue
                if avg_volume20_lots is None or avg_volume20_lots <= 0:
                    continue
                output[str(item["symbol"])] = {
                    "symbol": str(item["symbol"]),
                    "name": str(item.get("name") or ""),
                    "sector": str(item.get("sector") or "上市櫃"),
                    "marketType": str(item.get("market_type") or ""),
                    "priceDate": price_date,
                    "previousClose": previous_close,
                    "avgVolume20Lots": avg_volume20_lots,
                    "avgTurnover20Million": max(0.0, avg_turnover20_million or 0.0),
                    "historyDays": int(item.get("history_days") or 0),
                    "baselineMode": "daily_history",
                    "priceSource": str(item.get("price_source") or ""),
                }
        return output

    def intraday_discovery_metadata(self, symbols):
        clean = list(dict.fromkeys(
            str(symbol).replace(".TWO", "").replace(".TW", "").strip()
            for symbol in (symbols or [])
            if str(symbol).replace(".TWO", "").replace(".TW", "").strip().isdigit()
        ))
        output = {}
        for offset in range(0, len(clean), 800):
            chunk = clean[offset:offset + 800]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            with self.connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(f"""
                    SELECT symbol, name, sector, market_type
                    FROM stock_info
                    WHERE symbol IN ({placeholders})
                """, chunk).fetchall()
            for db_row in rows:
                row = dict(db_row)
                output[str(row.get("symbol") or "")] = {
                    "name": str(row.get("name") or ""),
                    "sector": str(row.get("sector") or "上市櫃"),
                    "marketType": str(row.get("market_type") or ""),
                }
        return output

    def upsert_intraday_scanner_rows(self, rows, trading_date=None, scan_at=None):
        """Persist one merged Shioaji scanner cycle as latest-only staging rows."""
        scan_value = str(scan_at or now_text()).replace("T", " ")[:19]
        date_value = str(trading_date or scan_value[:10] or today_key())[:10]
        updated_at = now_text()
        saved = 0
        rank_counts = {}
        with self.connect() as conn:
            for raw in rows or []:
                row = dict(raw or {})
                symbol = str(row.get("symbol") or row.get("code") or "").strip()
                symbol = symbol.replace(".TWO", "").replace(".TW", "")
                if not (symbol.isdigit() and len(symbol) == 4):
                    continue
                rank_types = row.get("rankTypes") or row.get("rank_types") or []
                if isinstance(rank_types, str):
                    rank_types = [part.strip() for part in rank_types.split(",") if part.strip()]
                for rank_type in set(rank_types):
                    rank_counts[str(rank_type)] = rank_counts.get(str(rank_type), 0) + 1
                close = self.safe_float(row.get("close"))
                if close is None:
                    close = self.safe_float(row.get("current"))
                snapshot_at = str(
                    row.get("snapshotAt") or row.get("snapshot_at") or scan_value
                ).replace("T", " ")[:25]
                conn.execute("""
                    INSERT INTO intraday_scanner_staging (
                        trading_date, symbol, scan_at, name, open, high, low, close,
                        change_price, total_volume_lots, total_amount, volume_ratio,
                        rank_types_json, source, snapshot_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trading_date, symbol) DO UPDATE SET
                        scan_at = excluded.scan_at,
                        name = excluded.name,
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        change_price = excluded.change_price,
                        total_volume_lots = excluded.total_volume_lots,
                        total_amount = excluded.total_amount,
                        volume_ratio = excluded.volume_ratio,
                        rank_types_json = excluded.rank_types_json,
                        source = excluded.source,
                        snapshot_at = excluded.snapshot_at,
                        updated_at = excluded.updated_at
                """, (
                    date_value,
                    symbol,
                    scan_value,
                    str(row.get("name") or ""),
                    self.safe_float(row.get("open")),
                    self.safe_float(row.get("high")),
                    self.safe_float(row.get("low")),
                    close,
                    self.safe_float(row.get("changePrice") or row.get("change_price")),
                    self.safe_float(
                        row.get("totalVolumeLots")
                        or row.get("total_volume_lots")
                        or row.get("totalVolume")
                    ),
                    self.safe_float(row.get("totalAmount") or row.get("total_amount")),
                    self.safe_float(row.get("volumeRatio") or row.get("volume_ratio")),
                    json.dumps(sorted(set(rank_types)), ensure_ascii=False),
                    str(row.get("source") or "sinopac_shioaji_scanner"),
                    snapshot_at,
                    updated_at,
                ))
                saved += 1
            if saved:
                conn.execute("""
                    INSERT OR REPLACE INTO intraday_scanner_cycles (
                        trading_date, scan_at, symbol_count, rank_counts_json,
                        source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    date_value,
                    scan_value,
                    saved,
                    json.dumps(rank_counts, ensure_ascii=False, sort_keys=True),
                    "sinopac_shioaji_scanner",
                    updated_at,
                ))
                self.set_meta(conn, "last_intraday_scanner_at", scan_value)
                self.set_meta(conn, "last_intraday_scanner_date", date_value)
                self.set_meta(conn, "last_intraday_scanner_count", str(saved))
        return {"ok": True, "date": date_value, "scanAt": scan_value, "saved": saved}

    def upsert_intraday_rotation_quotes(
        self, quotes, trading_date=None, scan_at=None, round_id=None,
        batch_index=0, batch_count=1, requested_count=None,
        requested_symbols=None, rotation_symbols=None, universe_count=0,
        fallback_codes=None, missing_symbols=None, source=None,
    ):
        """Persist one rotating all-market snapshot batch from the live session."""
        scan_value = str(scan_at or now_text()).replace("T", " ")[:19]
        date_value = str(trading_date or scan_value[:10] or today_key())[:10]
        round_value = str(round_id or f"{date_value}-{scan_value[11:19]}")[:40]
        batch_index = max(0, int(batch_index or 0))
        batch_count = max(1, int(batch_count or 1))
        quote_map = quotes if isinstance(quotes, dict) else {}
        requested_symbols = list(dict.fromkeys(
            str(symbol).strip() for symbol in (requested_symbols or quote_map)
            if str(symbol).strip().isdigit() and len(str(symbol).strip()) == 4
        ))
        rotation_symbols = list(dict.fromkeys(
            str(symbol).strip() for symbol in (rotation_symbols or requested_symbols)
            if str(symbol).strip().isdigit() and len(str(symbol).strip()) == 4
        ))
        fallback_codes = sorted({str(symbol) for symbol in (fallback_codes or [])})
        missing_symbols = sorted({
            str(symbol) for symbol in (
                missing_symbols
                if missing_symbols is not None
                else [symbol for symbol in requested_symbols if symbol not in quote_map]
            )
        })
        requested = max(0, int(
            len(requested_symbols) if requested_count is None else requested_count
        ))
        updated_at = now_text()
        saved = 0
        with self.connect() as conn:
            for raw_symbol, raw_quote in quote_map.items():
                symbol = str(raw_symbol or "").replace(".TW", "").replace(".TWO", "").strip()
                quote = dict(raw_quote or {})
                if not (symbol.isdigit() and len(symbol) == 4):
                    continue
                snapshot_at = str(
                    quote.get("snapshotAt") or quote.get("receivedAt") or scan_value
                )[:40]
                conn.execute("""
                    INSERT INTO intraday_rotation_staging (
                        trading_date, symbol, scan_at, round_id, batch_index,
                        batch_count, current_price, reference_price, open_price,
                        high_price, low_price, bid_price, ask_price,
                        total_volume_lots, is_suspended,
                        simtrade, source, snapshot_at, first_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trading_date, symbol) DO UPDATE SET
                        scan_at = excluded.scan_at,
                        round_id = excluded.round_id,
                        batch_index = excluded.batch_index,
                        batch_count = excluded.batch_count,
                        current_price = excluded.current_price,
                        reference_price = excluded.reference_price,
                        open_price = excluded.open_price,
                        high_price = excluded.high_price,
                        low_price = excluded.low_price,
                        bid_price = excluded.bid_price,
                        ask_price = excluded.ask_price,
                        total_volume_lots = excluded.total_volume_lots,
                        is_suspended = excluded.is_suspended,
                        simtrade = excluded.simtrade,
                        source = excluded.source,
                        snapshot_at = excluded.snapshot_at,
                        updated_at = excluded.updated_at
                """, (
                    date_value, symbol, scan_value, round_value, batch_index,
                    batch_count,
                    self.safe_float(quote.get("currentPrice") or quote.get("close")),
                    self.safe_float(quote.get("referencePrice") or quote.get("reference")),
                    self.safe_float(quote.get("openPrice") or quote.get("open")),
                    self.safe_float(quote.get("highPrice") or quote.get("high")),
                    self.safe_float(quote.get("lowPrice") or quote.get("low")),
                    self.safe_float(quote.get("bidPrice") or quote.get("bid")),
                    self.safe_float(quote.get("askPrice") or quote.get("ask")),
                    self.safe_float(quote.get("totalVolume") or quote.get("volume")),
                    1 if quote.get("isSuspended") or quote.get("suspend") else 0,
                    1 if quote.get("simtrade") else 0,
                    str(quote.get("source") or "sinopac_shioaji_rotation"),
                    snapshot_at, scan_value, updated_at,
                ))
                saved += 1
            conn.execute("""
                INSERT OR REPLACE INTO intraday_rotation_cycles (
                    trading_date, scan_at, round_id, batch_index, batch_count,
                    requested_count, received_count, universe_count,
                    fallback_count, requested_symbols_json,
                    rotation_symbols_json, missing_symbols_json,
                    source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_value, scan_value, round_value, batch_index, batch_count,
                requested, saved, max(0, int(universe_count or 0)),
                len(fallback_codes),
                json.dumps(requested_symbols, ensure_ascii=False),
                json.dumps(rotation_symbols, ensure_ascii=False),
                json.dumps(missing_symbols, ensure_ascii=False),
                str(source or "sinopac_shioaji_rotation"), updated_at,
            ))
            self.set_meta(conn, "last_intraday_rotation_at", scan_value)
            self.set_meta(conn, "last_intraday_rotation_saved", str(saved))
        return {
            "ok": saved > 0,
            "date": date_value,
            "scanAt": scan_value,
            "roundId": round_value,
            "batchIndex": batch_index,
            "batchCount": batch_count,
            "requested": requested,
            "saved": saved,
            "fallbackCount": len(fallback_codes),
            "missingSymbols": missing_symbols,
        }

    def latest_intraday_rotation_payload(self, trading_date=None, max_age_seconds=180):
        date_value = str(trading_date or today_key())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM intraday_rotation_staging
                WHERE trading_date = ?
                ORDER BY symbol
            """, (date_value,)).fetchall()
            latest_cycle = conn.execute("""
                SELECT * FROM intraday_rotation_cycles
                WHERE trading_date = ?
                ORDER BY scan_at DESC, batch_index DESC
                LIMIT 1
            """, (date_value,)).fetchone()
        now = dt.datetime.now().astimezone()
        quotes = {}
        stale_count = 0
        latest_scan_at = ""
        for db_row in rows:
            row = dict(db_row)
            timestamp = str(row.get("snapshot_at") or row.get("scan_at") or "")
            try:
                parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                age_seconds = max(0.0, (now - parsed.astimezone()).total_seconds())
            except (TypeError, ValueError):
                stale_count += 1
                continue
            if max_age_seconds is not None and age_seconds > max(1, int(max_age_seconds)):
                stale_count += 1
                continue
            symbol = str(row.get("symbol") or "")
            latest_scan_at = max(latest_scan_at, str(row.get("scan_at") or ""))
            quotes[symbol] = {
                "currentPrice": self.safe_float(row.get("current_price")),
                "referencePrice": self.safe_float(row.get("reference_price")),
                "openPrice": self.safe_float(row.get("open_price")),
                "highPrice": self.safe_float(row.get("high_price")),
                "lowPrice": self.safe_float(row.get("low_price")),
                "bidPrice": self.safe_float(row.get("bid_price")),
                "askPrice": self.safe_float(row.get("ask_price")),
                "totalVolume": self.safe_float(row.get("total_volume_lots")),
                "totalVolumeUnit": "lots",
                "isSuspended": bool(row.get("is_suspended")),
                "simtrade": bool(row.get("simtrade")),
                "source": str(row.get("source") or "sinopac_shioaji_rotation"),
                "snapshotAt": timestamp,
                "scanAt": str(row.get("scan_at") or ""),
                "rotationRoundId": str(row.get("round_id") or ""),
                "rotationBatchIndex": int(row.get("batch_index") or 0),
                "quoteAgeSeconds": round(age_seconds, 1),
            }
        cycle = dict(latest_cycle) if latest_cycle else {}
        try:
            requested_symbols = json.loads(cycle.get("requested_symbols_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            requested_symbols = []
        try:
            rotation_symbols = json.loads(cycle.get("rotation_symbols_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            rotation_symbols = []
        try:
            missing_symbols = json.loads(cycle.get("missing_symbols_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            missing_symbols = []
        return {
            "ok": bool(quotes),
            "date": date_value,
            "scanAt": latest_scan_at or None,
            "count": len(quotes),
            "staleCount": stale_count,
            "quotes": quotes,
            "source": "sinopac_shioaji_rotation",
            "latestCycle": {
                "scanAt": cycle.get("scan_at"),
                "roundId": cycle.get("round_id"),
                "batchIndex": int(cycle.get("batch_index") or 0),
                "batchCount": int(cycle.get("batch_count") or 0),
                "universeCount": int(cycle.get("universe_count") or 0),
                "requested": int(cycle.get("requested_count") or 0),
                "received": int(cycle.get("received_count") or 0),
                "fallbackCount": int(cycle.get("fallback_count") or 0),
                "requestedSymbols": requested_symbols,
                "rotationSymbols": rotation_symbols,
                "missingSymbols": missing_symbols,
            },
        }

    def intraday_rotation_coverage(self, trading_date=None):
        date_value = str(trading_date or today_key())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            summary = conn.execute("""
                SELECT COUNT(*) AS cycles, MIN(scan_at) AS first_scan_at,
                       MAX(scan_at) AS last_scan_at,
                       SUM(requested_count) AS requested,
                       SUM(received_count) AS received
                FROM intraday_rotation_cycles
                WHERE trading_date = ?
            """, (date_value,)).fetchone()
            completed = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT round_id
                    FROM intraday_rotation_cycles
                    WHERE trading_date = ?
                    GROUP BY round_id
                    HAVING COUNT(DISTINCT batch_index) >= MAX(batch_count)
                )
            """, (date_value,)).fetchone()[0]
            covered = conn.execute("""
                SELECT COUNT(DISTINCT symbol)
                FROM intraday_rotation_staging
                WHERE trading_date = ?
            """, (date_value,)).fetchone()[0]
            cycle_rows = conn.execute("""
                SELECT rotation_symbols_json
                FROM intraday_rotation_cycles
                WHERE trading_date = ?
            """, (date_value,)).fetchall()
        attempted_symbols = set()
        for cycle_row in cycle_rows:
            try:
                attempted_symbols.update(json.loads(cycle_row[0] or "[]"))
            except (TypeError, json.JSONDecodeError):
                continue
        eligible = len(self.listed_symbols())
        first_at = str((summary and summary["first_scan_at"]) or "")
        last_at = str((summary and summary["last_scan_at"]) or "")

        def minute_of_day(value):
            try:
                parsed = dt.datetime.fromisoformat(value.replace("T", " "))
                return parsed.hour * 60 + parsed.minute
            except (TypeError, ValueError):
                return None

        first_minute = minute_of_day(first_at)
        last_minute = minute_of_day(last_at)
        span_minutes = (
            max(0, last_minute - first_minute)
            if first_minute is not None and last_minute is not None else 0
        )
        attempt_ratio = len(attempted_symbols) / eligible if eligible else 0.0
        coverage_ratio = covered / eligible if eligible else 0.0
        opening_covered = first_minute is not None and first_minute <= 9 * 60 + 30
        closing_covered = last_minute is not None and last_minute >= 13 * 60 + 15
        enough_cycles = int((summary and summary["cycles"]) or 0) >= 120
        enough_rounds = int(completed or 0) >= 1
        enough_span = span_minutes >= 210
        enough_symbols = coverage_ratio >= 0.95
        enough_attempted = attempt_ratio >= 0.98
        valid = all((
            opening_covered, closing_covered, enough_cycles, enough_rounds,
            enough_span, enough_attempted, enough_symbols,
        ))
        missing = []
        if not opening_covered:
            missing.append("缺少開盤前段全市場輪巡")
        if not closing_covered:
            missing.append("缺少尾盤全市場輪巡")
        if not enough_cycles:
            missing.append(
                f"輪巡批次不足({int((summary and summary['cycles']) or 0)}/120)"
            )
        if not enough_rounds:
            missing.append("尚未完成至少一輪全市場輪巡")
        if not enough_span:
            missing.append(f"輪巡跨度不足({span_minutes}/210分鐘)")
        if not enough_attempted:
            missing.append(f"輪巡要求覆蓋不足({len(attempted_symbols)}/{eligible})")
        if not enough_symbols:
            missing.append(f"有效報價覆蓋不足({covered}/{eligible})")
        return {
            "ok": True,
            "valid": valid,
            "date": date_value,
            "cycles": int((summary and summary["cycles"]) or 0),
            "completeRounds": int(completed or 0),
            "firstScanAt": first_at or None,
            "lastScanAt": last_at or None,
            "spanMinutes": span_minutes,
            "coveredSymbols": int(covered or 0),
            "attemptedSymbols": len(attempted_symbols),
            "eligibleSymbols": eligible,
            "attemptRatio": round(attempt_ratio, 6),
            "coverageRatio": round(coverage_ratio, 6),
            "requested": int((summary and summary["requested"]) or 0),
            "received": int((summary and summary["received"]) or 0),
            "missing": missing,
            "reason": "；".join(missing),
        }

    def latest_intraday_scanner_payload(self, trading_date=None, max_age_seconds=150):
        date_value = str(trading_date or today_key())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            latest = conn.execute("""
                SELECT MAX(scan_at) AS scan_at
                FROM intraday_scanner_staging
                WHERE trading_date = ?
            """, (date_value,)).fetchone()
            scan_at = str((latest and latest["scan_at"]) or "")
            rows = conn.execute("""
                SELECT * FROM intraday_scanner_staging
                WHERE trading_date = ? AND scan_at = ?
                ORDER BY symbol
            """, (date_value, scan_at)).fetchall() if scan_at else []
        age_seconds = None
        fresh = bool(rows)
        if scan_at:
            try:
                parsed = dt.datetime.fromisoformat(scan_at.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                age_seconds = max(0.0, (dt.datetime.now() - parsed).total_seconds())
                if max_age_seconds is not None:
                    fresh = fresh and age_seconds <= max(1, int(max_age_seconds))
            except (TypeError, ValueError):
                fresh = False
        output = []
        for db_row in rows:
            row = dict(db_row)
            try:
                rank_types = json.loads(row.get("rank_types_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                rank_types = []
            output.append({
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "open": self.safe_float(row.get("open")),
                "high": self.safe_float(row.get("high")),
                "low": self.safe_float(row.get("low")),
                "current": self.safe_float(row.get("close")),
                "close": self.safe_float(row.get("close")),
                "currentPrice": self.safe_float(row.get("close")),
                "openPrice": self.safe_float(row.get("open")),
                "highPrice": self.safe_float(row.get("high")),
                "lowPrice": self.safe_float(row.get("low")),
                "changePrice": self.safe_float(row.get("change_price")),
                "totalVolume": self.safe_float(row.get("total_volume_lots")),
                "totalVolumeLots": self.safe_float(row.get("total_volume_lots")),
                "totalVolumeUnit": "lots",
                "totalAmount": self.safe_float(row.get("total_amount")),
                "volumeRatio": self.safe_float(row.get("volume_ratio")),
                "rankTypes": rank_types,
                "source": str(row.get("source") or "sinopac_shioaji_scanner"),
                "snapshotAt": str(row.get("snapshot_at") or scan_at),
            })
        return {
            "ok": bool(output),
            "fresh": bool(fresh),
            "date": date_value,
            "scanAt": scan_at or None,
            "ageSeconds": age_seconds,
            "count": len(output),
            "rows": output,
        }

    def intraday_scanner_coverage(self, trading_date=None):
        date_value = str(trading_date or today_key())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT COUNT(*) AS cycles, MIN(scan_at) AS first_scan_at,
                       MAX(scan_at) AS last_scan_at, AVG(symbol_count) AS avg_symbols
                FROM intraday_scanner_cycles
                WHERE trading_date = ?
            """, (date_value,)).fetchone()
            rank_rows = conn.execute("""
                SELECT rank_counts_json
                FROM intraday_scanner_cycles
                WHERE trading_date = ?
            """, (date_value,)).fetchall()
        rank_counts = {
            "change_percent": 0,
            "day_range": 0,
            "volume": 0,
            "amount": 0,
            "tick_count": 0,
        }
        for rank_row in rank_rows:
            try:
                values = json.loads(rank_row["rank_counts_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                values = {}
            for key in rank_counts:
                rank_counts[key] = max(rank_counts[key], int(values.get(key) or 0))
        cycles = int((row and row["cycles"]) or 0)
        first_at = str((row and row["first_scan_at"]) or "")
        last_at = str((row and row["last_scan_at"]) or "")

        def minute_of_day(value):
            try:
                parsed = dt.datetime.fromisoformat(value.replace("T", " "))
                return parsed.hour * 60 + parsed.minute
            except (TypeError, ValueError):
                return None

        first_minute = minute_of_day(first_at)
        last_minute = minute_of_day(last_at)
        span_minutes = (
            max(0, last_minute - first_minute)
            if first_minute is not None and last_minute is not None
            else 0
        )
        opening_covered = first_minute is not None and first_minute <= 9 * 60 + 30
        closing_covered = last_minute is not None and last_minute >= 13 * 60 + 15
        enough_cycles = cycles >= 120
        enough_span = span_minutes >= 210
        required_rank_types = (
            "change_percent", "day_range", "volume", "amount",
        )
        four_rank_types = all(rank_counts[key] > 0 for key in required_rank_types)
        five_rank_types = all(value > 0 for value in rank_counts.values())
        valid = all((
            opening_covered, closing_covered, enough_cycles,
            enough_span, four_rank_types,
        ))
        missing = []
        if not opening_covered:
            missing.append("缺少開盤前段排行")
        if not closing_covered:
            missing.append("缺少尾盤排行")
        if not enough_cycles:
            missing.append(f"排行週期不足({cycles}/120)")
        if not enough_span:
            missing.append(f"觀測跨度不足({span_minutes}/210分鐘)")
        if not four_rank_types:
            missing.append("四類必要排行未完整取得")
        return {
            "ok": True,
            "valid": valid,
            "date": date_value,
            "cycles": cycles,
            "firstScanAt": first_at or None,
            "lastScanAt": last_at or None,
            "spanMinutes": span_minutes,
            "averageSymbols": round(float((row and row["avg_symbols"]) or 0), 1),
            "rankCounts": rank_counts,
            "requiredRankTypes": list(required_rank_types),
            "fourRankTypesComplete": four_rank_types,
            "fiveRankTypesComplete": five_rank_types,
            "missing": missing,
            "reason": "；".join(missing),
        }

    def latest_intraday_tick_quotes(self, symbols, trading_date=None, max_age_seconds=60):
        """Return latest subscribed Tick prices as a lightweight deep-scan overlay."""
        codes = list(dict.fromkeys(
            str(symbol).strip() for symbol in (symbols or [])
            if str(symbol).strip().isdigit() and len(str(symbol).strip()) == 4
        ))
        if not codes:
            return {}
        date_value = str(trading_date or today_key())[:10]
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                WITH scoped AS (
                    SELECT *
                    FROM intraday_minute_bars
                    WHERE date = ? AND symbol IN ({placeholders})
                ), windowed AS (
                    SELECT
                        symbol,
                        FIRST_VALUE(open) OVER (
                            PARTITION BY symbol ORDER BY minute ASC
                        ) AS day_open,
                        MAX(high) OVER (PARTITION BY symbol) AS day_high,
                        MIN(low) OVER (PARTITION BY symbol) AS day_low,
                        FIRST_VALUE(close) OVER (
                            PARTITION BY symbol ORDER BY minute DESC
                        ) AS latest_close,
                        SUM(volume_lots) OVER (PARTITION BY symbol) AS total_volume_lots,
                        MAX(last_tick_at) OVER (PARTITION BY symbol) AS last_tick_at,
                        MAX(updated_at) OVER (PARTITION BY symbol) AS updated_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol ORDER BY minute DESC
                        ) AS row_number
                    FROM scoped
                )
                SELECT * FROM windowed WHERE row_number = 1
            """, [date_value, *codes]).fetchall()
        now = dt.datetime.now()
        output = {}
        for db_row in rows:
            row = dict(db_row)
            timestamp = str(row.get("last_tick_at") or row.get("updated_at") or "")
            try:
                parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                age_seconds = max(0.0, (now - parsed).total_seconds())
            except (TypeError, ValueError):
                continue
            if max_age_seconds is not None and age_seconds > max(1, int(max_age_seconds)):
                continue
            symbol = str(row.get("symbol") or "")
            output[symbol] = {
                "currentPrice": self.safe_float(row.get("latest_close")),
                "openPrice": self.safe_float(row.get("day_open")),
                "highPrice": self.safe_float(row.get("day_high")),
                "lowPrice": self.safe_float(row.get("day_low")),
                "totalVolume": self.safe_float(row.get("total_volume_lots")),
                "totalVolumeUnit": "lots",
                "snapshotAt": timestamp,
                "quoteAgeSeconds": round(age_seconds, 1),
                "source": "sinopac_shioaji_realtime_tick",
            }
        return output

    def record_intraday_discovery_events(self, rows, trading_date=None, observed_at=None):
        """Append first-stage/state/peak events plus a five-minute heartbeat."""
        observed_value = str(observed_at or now_text()).replace("T", " ")[:19]
        date_value = str(trading_date or observed_value[:10] or today_key())[:10]
        try:
            moment = dt.datetime.fromisoformat(observed_value)
        except ValueError:
            moment = dt.datetime.now()
            observed_value = moment.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat = moment.replace(minute=(moment.minute // 5) * 5, second=0, microsecond=0)
        heartbeat_key = f"seen:{heartbeat:%H:%M}"
        inserted = 0
        attempted = 0
        created_at = now_text()
        with self.connect() as conn:
            for raw in rows or []:
                row = dict(raw or {})
                symbol = str(row.get("symbol") or "").strip()
                if not (symbol.isdigit() and len(symbol) == 4):
                    continue
                stage = str(row.get("stage") or "strong").strip().lower()
                state = str(row.get("state") or "active").strip().lower()
                high_change = self.safe_float(row.get("highChangePct"))
                current_change = self.safe_float(row.get("currentChangePct"))
                peak_value = max(
                    value for value in (high_change, current_change, 0.0)
                    if value is not None
                )
                peak_bucket = math.floor(max(0.0, peak_value) * 2.0) / 2.0
                event_specs = [
                    (f"stage:{stage}", "stage"),
                    (f"state:{state}", "state"),
                    (f"peak:{peak_bucket:.1f}", "peak"),
                    (heartbeat_key, "heartbeat"),
                ]
                confirmation_count = max(0, int(row.get("confirmationCount") or 0))
                if confirmation_count:
                    event_specs.append((
                        f"confirmation:{confirmation_count}",
                        "confirmation",
                    ))
                payload = json.dumps(row, ensure_ascii=False, default=str)
                for event_key, event_type in event_specs:
                    attempted += 1
                    cursor = conn.execute("""
                        INSERT OR IGNORE INTO intraday_discovery_events (
                            trading_date, observed_at, symbol, name, sector,
                            event_key, event_type, stage, state, current_price,
                            current_change_pct, high_change_pct, volume_progress_ratio,
                            turnover_million, liquidity_tier, quote_source,
                            discovery_type, in_radar, observation_only,
                            payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        date_value,
                        observed_value,
                        symbol,
                        str(row.get("name") or ""),
                        str(row.get("sector") or ""),
                        event_key,
                        event_type,
                        stage,
                        state,
                        self.safe_float(row.get("currentPrice")),
                        current_change,
                        high_change,
                        self.safe_float(row.get("volumeProgressRatio")),
                        self.safe_float(row.get("turnoverMillion")),
                        str(row.get("liquidityTier") or ""),
                        str(row.get("quoteSource") or ""),
                        str(row.get("discoveryType") or "intraday_market_discovery"),
                        1 if row.get("inRadar") else 0,
                        0 if row.get("canBuy") else 1,
                        payload,
                        created_at,
                    ))
                    inserted += max(0, int(cursor.rowcount or 0))
            if inserted:
                self.set_meta(conn, "last_intraday_discovery_event_at", observed_value)
                self.set_meta(conn, "last_intraday_discovery_event_count", str(inserted))
        return {
            "ok": True,
            "date": date_value,
            "observedAt": observed_value,
            "attempted": attempted,
            "inserted": inserted,
        }

    def record_intraday_discovery_audit(
        self, rows, trading_date=None, observed_at=None,
    ):
        """Upsert every evaluated symbol/reason for post-close miss analysis."""
        observed_value = str(observed_at or now_text()).replace("T", " ")[:19]
        date_value = str(trading_date or observed_value[:10] or today_key())[:10]
        written = 0
        evaluated = 0
        created_at = now_text()
        with self.connect() as conn:
            for raw in rows or []:
                row = dict(raw or {})
                symbol = str(row.get("symbol") or "").strip()
                if not (symbol.isdigit() and len(symbol) == 4):
                    continue
                evaluated += 1
                reasons = row.get("exclusionReasons") or []
                if not reasons:
                    reasons = [{
                        "code": "qualified",
                        "label": "通過盤中召回層",
                    }]
                payload = json.dumps(row, ensure_ascii=False, default=str)
                for reason in reasons:
                    if isinstance(reason, dict):
                        code = str(reason.get("code") or "unknown_exclusion")
                        label = str(reason.get("label") or code)
                    else:
                        code = str(reason or "unknown_exclusion")
                        label = code
                    excluded = code != "qualified"
                    conn.execute("""
                        INSERT INTO intraday_discovery_exclusion_audit (
                            trading_date, symbol, reason_code, reason_label,
                            excluded, first_observed_at, last_observed_at,
                            occurrences, payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(trading_date, symbol, reason_code) DO UPDATE SET
                            reason_label = excluded.reason_label,
                            excluded = excluded.excluded,
                            last_observed_at = excluded.last_observed_at,
                            occurrences = intraday_discovery_exclusion_audit.occurrences + 1,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                    """, (
                        date_value, symbol, code, label, 1 if excluded else 0,
                        observed_value, observed_value, payload,
                        created_at, created_at,
                    ))
                    written += 1
            if evaluated:
                self.set_meta(conn, "last_intraday_discovery_audit_at", observed_value)
                self.set_meta(conn, "last_intraday_discovery_audit_symbols", str(evaluated))
        return {
            "ok": True,
            "date": date_value,
            "evaluated": evaluated,
            "reasonRows": written,
        }

    def record_intraday_candidate_signals(
        self, rows, signal_date=None, signaled_at=None,
    ):
        """Persist first executable quote for newly discovered formal candidates."""
        signaled_value = str(signaled_at or now_text()).replace("T", " ")[:19]
        date_value = str(signal_date or signaled_value[:10] or today_key())[:10]
        records = []
        for raw in rows or []:
            row = dict(raw or {})
            if not row.get("candidateSignal"):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if not (symbol.isdigit() and len(symbol) == 4):
                continue
            entry_price = self.safe_float(row.get("executionEntryPrice"))
            if entry_price is None or entry_price <= 0:
                continue
            records.append((
                date_value,
                symbol,
                signaled_value,
                str(row.get("priceDate") or "")[:10],
                self.safe_float(row.get("formalScore") or row.get("score")),
                entry_price,
                "intraday_new_candidate_execution",
                str(row.get("quoteSource") or row.get("source") or ""),
                json.dumps(row, ensure_ascii=False, default=str),
                now_text(),
            ))
        if not records:
            return {"ok": True, "prepared": 0, "inserted": 0}
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany("""
                INSERT OR IGNORE INTO intraday_candidate_signals (
                    signal_date, symbol, signaled_at, price_date, score,
                    entry_price, entry_mode, quote_source, gate_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)
            inserted = conn.total_changes - before
            self.set_meta(conn, "last_intraday_candidate_signal_at", signaled_value)
        return {
            "ok": True,
            "prepared": len(records),
            "inserted": inserted,
            "duplicates": len(records) - inserted,
        }

    def compute_intraday_candidate_accuracy(self, lookback_days=365):
        """Settle new intraday-candidate signals with the production 10-day policy."""
        lookback_days = max(30, min(int(lookback_days or 365), 1095))
        cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            signals = conn.execute("""
                SELECT * FROM intraday_candidate_signals
                WHERE signal_date >= ?
                ORDER BY signal_date, symbol
            """, (cutoff,)).fetchall()
            symbols = sorted({str(row["symbol"]) for row in signals})
            prices_by_symbol = {}
            for offset in range(0, len(symbols), 400):
                chunk = symbols[offset:offset + 400]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                price_rows = conn.execute(
                    f"SELECT symbol, date, open, high, low, close FROM prices "
                    f"WHERE symbol IN ({placeholders}) AND date >= ? "
                    "ORDER BY symbol, date",
                    [*chunk, cutoff],
                ).fetchall()
                for price_row in price_rows:
                    prices_by_symbol.setdefault(
                        str(price_row["symbol"]), []
                    ).append(dict(price_row))
        observations = []
        for db_row in signals:
            signal = dict(db_row)
            symbol = str(signal.get("symbol") or "")
            signal_date = str(signal.get("signal_date") or "")[:10]
            future_rows = [
                row for row in (prices_by_symbol.get(symbol) or [])
                if str(row.get("date") or "")[:10] > signal_date
            ][:MONSTER_TARGET_HORIZON_DAYS]
            outcome = simulate_radar_trade_path(
                signal.get("entry_price"), future_rows,
            )
            if not outcome:
                continue
            observations.append({
                "symbol": symbol,
                "signalDate": signal_date,
                "signaledAt": signal.get("signaled_at"),
                "score": self.safe_float(signal.get("score")),
                "entryMode": signal.get("entry_mode"),
                "outcome": outcome,
            })
        summary = self._radar_outcome_summary(observations)
        settled_records = []
        for item in observations:
            outcome = item["outcome"]
            if not outcome.get("settled"):
                continue
            settled_records.append({
                "score": item.get("score"),
                "targetHit": outcome.get("targetHit"),
                "netReturn": outcome.get("netReturn"),
            })
        pr = precision_recall_thresholds(
            settled_records,
            RADAR_THRESHOLD_CANDIDATES,
        )
        recent = []
        for item in reversed(observations[-30:]):
            outcome = item["outcome"]
            recent.append({
                "symbol": item["symbol"],
                "signalDate": item["signalDate"],
                "score": item.get("score"),
                "settled": bool(outcome.get("settled")),
                "targetHit": outcome.get("targetHit"),
                "maxAdverse": outcome.get("maxAdverse"),
                "netReturn": outcome.get("netReturn"),
                "exitReason": outcome.get("exitReason"),
            })
        return {
            "ok": True,
            "definition": (
                "連續兩次新鮮報價且通過正式規則後，以真實盤中可成交價記錄；"
                "後續 10 個交易日先到 +10% 為命中、先到 -7% 為失敗，"
                "損益扣手續費、證交稅與滑價"
            ),
            "sameDayPathExcluded": True,
            "lookbackDays": lookback_days,
            **summary,
            "precisionRecall": pr,
            "recent": recent,
            "independentModelUsed": False,
        }

    def intraday_hot_symbols(self, trading_date=None, limit=60, max_age_minutes=15):
        date_value = str(trading_date or today_key())[:10]
        cutoff = (dt.datetime.now() - dt.timedelta(
            minutes=max(1, int(max_age_minutes or 15))
        )).strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                WITH latest AS (
                    SELECT symbol, observed_at, stage, state,
                           current_change_pct, high_change_pct, turnover_million,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol
                               ORDER BY observed_at DESC, event_id DESC
                           ) AS rn
                    FROM intraday_discovery_events
                    WHERE trading_date = ? AND observed_at >= ?
                )
                SELECT * FROM latest
                WHERE rn = 1
                ORDER BY
                    CASE state
                        WHEN 'near_limit' THEN 0
                        WHEN 'active' THEN 1
                        WHEN 'early' THEN 2
                        ELSE 3
                    END,
                    CASE stage WHEN 'strong' THEN 0 ELSE 1 END,
                    COALESCE(high_change_pct, current_change_pct, 0) DESC,
                    COALESCE(turnover_million, 0) DESC,
                    symbol
                LIMIT ?
            """, (date_value, cutoff, max(1, int(limit or 60)))).fetchall()
        return [str(row["symbol"]) for row in rows]

    def settle_intraday_discovery_recall(self, trading_date=None):
        """Compare durable discoveries with verified post-close daily highs."""
        date_value = str(trading_date or self.latest_complete_price_date() or "")[:10]
        if not date_value:
            return {"ok": False, "pending": True, "reason": "尚無完整官方收盤日線"}
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            price_rows = conn.execute("""
                SELECT p.symbol, p.high, p.close, p.volume, si.name,
                       (
                           SELECT p2.close FROM prices p2
                           WHERE p2.symbol = p.symbol AND p2.date < p.date
                           ORDER BY p2.date DESC LIMIT 1
                       ) AS previous_close,
                       (
                           SELECT COUNT(*) FROM prices p3
                           WHERE p3.symbol = p.symbol AND p3.date < p.date
                       ) AS prior_history_days,
                       (
                           SELECT AVG(p4.volume) / 1000.0 FROM (
                               SELECT volume FROM prices p4i
                               WHERE p4i.symbol = p.symbol AND p4i.date < p.date
                               ORDER BY p4i.date DESC LIMIT 20
                           ) p4
                       ) AS prior_avg_volume_lots,
                       (
                           SELECT AVG(p5.volume * p5.close) / 1000000.0 FROM (
                               SELECT volume, close FROM prices p5i
                               WHERE p5i.symbol = p.symbol AND p5i.date < p.date
                               ORDER BY p5i.date DESC LIMIT 20
                           ) p5
                       ) AS prior_avg_turnover_million
                FROM prices p
                LEFT JOIN stock_info si ON si.symbol = p.symbol
                WHERE p.date = ?
            """, (date_value,)).fetchall()
            event_rows = conn.execute("""
                SELECT event_id, symbol, observed_at, event_type, stage, state,
                       current_change_pct, high_change_pct, payload_json
                FROM intraday_discovery_events
                WHERE trading_date = ?
                ORDER BY observed_at, event_id
            """, (date_value,)).fetchall()
            scanner_rows = conn.execute("""
                SELECT DISTINCT symbol
                FROM intraday_scanner_staging
                WHERE trading_date = ?
            """, (date_value,)).fetchall()
            rotation_rows = conn.execute("""
                SELECT * FROM intraday_rotation_staging
                WHERE trading_date = ?
            """, (date_value,)).fetchall()
            audit_rows = conn.execute("""
                SELECT symbol, reason_code, reason_label, occurrences,
                       last_observed_at
                FROM intraday_discovery_exclusion_audit
                WHERE trading_date = ? AND excluded = 1
                ORDER BY last_observed_at DESC
            """, (date_value,)).fetchall()
        coverage = self.intraday_scanner_coverage(date_value)
        rotation_coverage = self.intraday_rotation_coverage(date_value)
        if not coverage.get("valid") or not rotation_coverage.get("valid"):
            coverage_reason = "；".join(
                reason for reason in (
                    coverage.get("reason"), rotation_coverage.get("reason")
                ) if reason
            )
            return {
                "ok": True,
                "skipped": True,
                "valid": False,
                "date": date_value,
                "coverage": coverage,
                "rotationCoverage": rotation_coverage,
                "reason": (
                    coverage_reason
                    or "該交易日沒有完整盤中探索觀測資料"
                ) + "，不納入找到率",
            }
        listed = set(self.listed_symbols())
        actual = {}
        for row in price_rows:
            symbol = str(row["symbol"] or "")
            if symbol not in listed:
                continue
            previous_close = self.safe_float(row["previous_close"])
            high = self.safe_float(row["high"])
            close = self.safe_float(row["close"])
            volume = self.safe_float(row["volume"])
            if not previous_close or previous_close <= 0 or not high or high <= 0:
                continue
            high_change = (high / previous_close - 1.0) * 100.0
            turnover = max(0.0, (close or high) * (volume or 0.0) / 1_000_000.0)
            if high_change >= 5.0 and turnover >= 5.0:
                actual[symbol] = {
                    "symbol": symbol,
                    "name": str(row["name"] or ""),
                    "highChangePct": round(high_change, 2),
                    "turnoverMillion": round(turnover, 2),
                    "priorHistoryDays": int(row["prior_history_days"] or 0),
                    "priorAvgVolumeLots": round(
                        self.safe_float(row["prior_avg_volume_lots"]) or 0.0, 2
                    ),
                    "priorAvgTurnoverMillion": round(
                        self.safe_float(row["prior_avg_turnover_million"]) or 0.0, 2
                    ),
                }
        discovered = {}
        for db_row in event_rows:
            row = dict(db_row)
            symbol = str(row.get("symbol") or "")
            if symbol in discovered:
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            discovered[symbol] = {
                **row,
                "payload": payload,
            }
        scanner_symbols = {str(row["symbol"]) for row in scanner_rows}
        rotation_by_symbol = {
            str(row["symbol"]): dict(row) for row in rotation_rows
        }
        audit_by_symbol = {}
        exclusion_reason_counts = Counter()
        for audit_row in audit_rows:
            item = dict(audit_row)
            audit_by_symbol.setdefault(str(item.get("symbol") or ""), []).append(item)
            code = str(item.get("reason_code") or "unknown_exclusion")
            exclusion_reason_counts[code] += max(1, int(item.get("occurrences") or 1))
        detected_symbols = sorted(set(actual) & set(discovered))
        early_symbols = sorted(
            symbol for symbol in detected_symbols
            if str(discovered[symbol].get("stage") or "") in {"emerging", "early"}
            or (
                self.safe_float(discovered[symbol].get("high_change_pct"))
                or self.safe_float(discovered[symbol].get("current_change_pct"))
                or 99.0
            ) < 5.0
        )
        actionable_symbols = sorted(
            symbol for symbol in detected_symbols
            if bool(discovered[symbol].get("payload", {}).get(
                "actionableAtObservation"
            ))
        )
        late_symbols = sorted(
            symbol for symbol in detected_symbols
            if bool(discovered[symbol].get("payload", {}).get("lateDiscovery"))
            or (
                self.safe_float(discovered[symbol].get("high_change_pct"))
                or self.safe_float(discovered[symbol].get("current_change_pct"))
                or 0.0
            ) >= 8.5
        )
        missed = []
        for symbol in sorted(set(actual) - set(discovered)):
            item = dict(actual[symbol])
            rotation = rotation_by_symbol.get(symbol)
            symbol_audits = audit_by_symbol.get(symbol) or []
            audit_codes = {
                str(reason.get("reason_code") or "") for reason in symbol_audits
            }
            if "stale_or_invalid_quote_time" in audit_codes:
                item["reasonCode"] = "stale_or_invalid_quote_time"
                item["reason"] = "報價缺漏或過期"
            elif "missing_broker_quote" in audit_codes:
                item["reasonCode"] = "missing_broker_quote"
                item["reason"] = "報價缺漏或未完成全市場輪巡"
            elif "missing_verified_daily_baseline" in audit_codes:
                item["reasonCode"] = "missing_verified_daily_baseline"
                item["reason"] = "新上市或歷史不足"
            elif "suspended_quote" in audit_codes or "zero_intraday_volume" in audit_codes:
                item["reasonCode"] = (
                    "suspended_quote" if "suspended_quote" in audit_codes
                    else "zero_intraday_volume"
                )
                item["reason"] = "暫停交易或無成交"
            elif rotation is None and symbol not in scanner_symbols:
                item["reasonCode"] = "not_seen_by_recall_sources"
                item["reason"] = "報價缺漏或未完成全市場輪巡"
            elif int(item.get("priorHistoryDays") or 0) < 1:
                item["reasonCode"] = "insufficient_daily_history"
                item["reason"] = "新上市或歷史不足"
            elif rotation and (
                bool(rotation.get("is_suspended"))
                or (self.safe_float(rotation.get("total_volume_lots")) or 0) <= 0
            ):
                item["reasonCode"] = "rotation_no_trade"
                item["reason"] = "暫停交易或無成交"
            elif rotation and (
                self.safe_float(rotation.get("reference_price")) or 0
            ) > 0 and (
                (self.safe_float(rotation.get("high_price")) or 0)
                / (self.safe_float(rotation.get("reference_price")) or 1) - 1
            ) * 100 >= 8.5:
                item["reasonCode"] = "rotation_discovered_too_late"
                item["reason"] = "輪巡發現時已接近漲停"
            elif (
                (self.safe_float(item.get("priorAvgVolumeLots")) or 0) < 1000
                or (self.safe_float(item.get("priorAvgTurnoverMillion")) or 0) < 30
            ):
                item["reasonCode"] = "liquidity_exception_failed"
                item["reason"] = "不在原流動性母體且未通過例外門檻"
            elif symbol in scanner_symbols:
                item["reasonCode"] = "recall_threshold_failed"
                item["reason"] = "觀察門檻未通過"
            else:
                item["reasonCode"] = "all_market_recall_threshold_failed"
                item["reason"] = "未進永豐五類排行，且全市場輪巡觀察門檻未通過"
            item["exclusionReasons"] = [
                {
                    "code": str(reason.get("reason_code") or ""),
                    "label": str(reason.get("reason_label") or ""),
                    "occurrences": int(reason.get("occurrences") or 0),
                    "lastObservedAt": reason.get("last_observed_at"),
                }
                for reason in symbol_audits
            ]
            missed.append(item)
        actual_count = len(actual)
        detected_count = len(detected_symbols)
        early_count = len(early_symbols)
        actionable_count = len(actionable_symbols)
        late_count = len(late_symbols)
        discovered_count = len(discovered)
        recall = detected_count / actual_count if actual_count else 0.0
        early_recall = early_count / actual_count if actual_count else 0.0
        actionable_recall = actionable_count / actual_count if actual_count else 0.0
        late_rate = late_count / detected_count if detected_count else 0.0
        precision = detected_count / discovered_count if discovered_count else 0.0
        report = {
            "date": date_value,
            "definition": "盤中最高漲幅至少 5%，且收盤估算成交金額至少 500 萬元",
            "actualMovers": actual_count,
            "detectedMovers": detected_count,
            "earlyDetected": early_count,
            "actionableDetected": actionable_count,
            "lateDetected": late_count,
            "missedMovers": len(missed),
            "discoveredSymbols": discovered_count,
            "recall": round(recall, 6),
            "earlyRecall": round(early_recall, 6),
            "actionableRecall": round(actionable_recall, 6),
            "lateRate": round(late_rate, 6),
            "precision": round(precision, 6),
            "detectedSymbols": detected_symbols,
            "earlySymbols": early_symbols,
            "actionableSymbols": actionable_symbols,
            "lateSymbols": late_symbols,
            "missed": missed,
            "exclusionReasonSummary": [
                {"code": code, "occurrences": count}
                for code, count in exclusion_reason_counts.most_common()
            ],
            "coverage": coverage,
            "rotationCoverage": rotation_coverage,
            "settledAt": now_text(),
        }
        now_value = now_text()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO intraday_discovery_daily_stats (
                    trading_date, actual_movers, detected_movers, early_detected,
                    actionable_detected, late_detected, missed_movers,
                    discovered_symbols, recall, early_recall, actionable_recall,
                    late_rate, precision, report_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trading_date) DO UPDATE SET
                    actual_movers = excluded.actual_movers,
                    detected_movers = excluded.detected_movers,
                    early_detected = excluded.early_detected,
                    actionable_detected = excluded.actionable_detected,
                    late_detected = excluded.late_detected,
                    missed_movers = excluded.missed_movers,
                    discovered_symbols = excluded.discovered_symbols,
                    recall = excluded.recall,
                    early_recall = excluded.early_recall,
                    actionable_recall = excluded.actionable_recall,
                    late_rate = excluded.late_rate,
                    precision = excluded.precision,
                    report_json = excluded.report_json,
                    updated_at = excluded.updated_at
            """, (
                date_value, actual_count, detected_count, early_count,
                actionable_count, late_count, len(missed), discovered_count,
                recall, early_recall, actionable_recall, late_rate, precision,
                json.dumps(report, ensure_ascii=False), now_value, now_value,
            ))
            self.set_meta(conn, "last_intraday_discovery_recall_date", date_value)
            self.set_meta(conn, "last_intraday_discovery_recall_at", now_value)
        return {"ok": True, **report}

    def intraday_discovery_recall_history(self, days=30, refresh_latest=False):
        settlement = None
        if refresh_latest:
            settlement = self.settle_intraday_discovery_recall()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM intraday_discovery_daily_stats
                ORDER BY trading_date DESC
                LIMIT ?
            """, (max(1, min(int(days or 30), 365)),)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                report = json.loads(item.get("report_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                report = {}
            output.append({
                "date": str(item.get("trading_date") or ""),
                "actualMovers": int(item.get("actual_movers") or 0),
                "detectedMovers": int(item.get("detected_movers") or 0),
                "earlyDetected": int(item.get("early_detected") or 0),
                "actionableDetected": int(item.get("actionable_detected") or 0),
                "lateDetected": int(item.get("late_detected") or 0),
                "missedMovers": int(item.get("missed_movers") or 0),
                "discoveredSymbols": int(item.get("discovered_symbols") or 0),
                "recall": float(item.get("recall") or 0),
                "earlyRecall": float(item.get("early_recall") or 0),
                "actionableRecall": float(item.get("actionable_recall") or 0),
                "lateRate": float(item.get("late_rate") or 0),
                "precision": float(item.get("precision") or 0),
                "missed": report.get("missed") or [],
                "exclusionReasonSummary": report.get("exclusionReasonSummary") or [],
                "settledAt": report.get("settledAt") or item.get("updated_at"),
            })
        totals = {
            "actualMovers": sum(item["actualMovers"] for item in output),
            "detectedMovers": sum(item["detectedMovers"] for item in output),
            "earlyDetected": sum(item["earlyDetected"] for item in output),
            "actionableDetected": sum(item["actionableDetected"] for item in output),
            "lateDetected": sum(item["lateDetected"] for item in output),
            "discoveredSymbols": sum(item["discoveredSymbols"] for item in output),
        }
        aggregate = {
            **totals,
            "recall": (
                totals["detectedMovers"] / totals["actualMovers"]
                if totals["actualMovers"] else 0.0
            ),
            "earlyRecall": (
                totals["earlyDetected"] / totals["actualMovers"]
                if totals["actualMovers"] else 0.0
            ),
            "actionableRecall": (
                totals["actionableDetected"] / totals["actualMovers"]
                if totals["actualMovers"] else 0.0
            ),
            "lateRate": (
                totals["lateDetected"] / totals["detectedMovers"]
                if totals["detectedMovers"] else 0.0
            ),
            "precision": (
                totals["detectedMovers"] / totals["discoveredSymbols"]
                if totals["discoveredSymbols"] else 0.0
            ),
        }
        gate_days = output[:20]
        gate_actual = sum(item["actualMovers"] for item in gate_days)
        gate_detected = sum(item["detectedMovers"] for item in gate_days)
        gate_early = sum(item["earlyDetected"] for item in gate_days)
        gate_recall = gate_detected / gate_actual if gate_actual else 0.0
        gate_early_recall = gate_early / gate_actual if gate_actual else 0.0
        required_days = 20
        recall_target = 0.95
        early_recall_target = 0.80
        readiness_reasons = []
        if len(gate_days) < required_days:
            readiness_reasons.append(f"完整交易日不足({len(gate_days)}/{required_days})")
        if gate_recall < recall_target:
            readiness_reasons.append(f"找到率不足({gate_recall:.1%}/{recall_target:.0%})")
        if gate_early_recall < early_recall_target:
            readiness_reasons.append(
                f"提早找到率不足({gate_early_recall:.1%}/{early_recall_target:.0%})"
            )
        readiness = {
            "paperSimulationEligible": not readiness_reasons,
            "automaticTradingEligible": False,
            "validDays": len(gate_days),
            "requiredDays": required_days,
            "recall": round(gate_recall, 6),
            "requiredRecall": recall_target,
            "earlyRecall": round(gate_early_recall, 6),
            "requiredEarlyRecall": early_recall_target,
            "reasons": readiness_reasons,
            "note": "達標只允許考慮盤中新發現的模擬買進，不會開放正式交易",
        }
        candidate_accuracy = self.compute_intraday_candidate_accuracy(
            lookback_days=365,
        )
        radar_track_record = self.compute_radar_score_track_record(
            lookback_days=365,
        )
        existing_intraday_accuracy = (
            ((radar_track_record.get("entryModePerformance") or {})
             .get("intradayConfirmed") or {})
            .get("eligible") or {}
        )
        return {
            "ok": True,
            "mode": "real_market_rules_only",
            "evaluationDefinitions": {
                "discoveryRecall": (
                    "找到率：收盤後驗證盤中最高漲幅至少 5% 且成交額至少 "
                    "500 萬元的股票，是否曾被召回層提早找到"
                ),
                "tradableAccuracy": (
                    "可買準確率：通過兩次新鮮報價與完整正式規則後，後續 "
                    "10 個交易日是否先達 +10%，並統計最大不利幅度與扣成本損益"
                ),
            },
            "days": output,
            "latest": output[0] if output else None,
            "aggregate": aggregate,
            "tradableAccuracy": {
                "newIntradayCandidates": candidate_accuracy,
                "existingRadarIntradayConfirmed": existing_intraday_accuracy,
                "independentModelUsed": False,
                "productionScoreChanged": False,
            },
            "readiness": readiness,
            "settlement": settlement,
        }

    def save_strategy_signal(self, conn, row):
        symbol = str(row.get("symbol") or "").replace(".TWO", "").replace(".TW", "").strip()
        strategy = str(row.get("strategy") or "").strip()
        side = str(row.get("side") or "").strip()
        if not symbol or not strategy or not side:
            raise ValueError("strategy signal requires symbol, strategy, and side")
        if strategy.casefold() in RESERVED_PRODUCTION_STRATEGIES:
            raise ValueError("reserved test strategy cannot be stored in production signals")

        signal_date = str(row.get("signalDate") or row.get("signal_date") or today_key())[:10]
        signal_session = str(row.get("signalSession") or row.get("signal_session") or "").strip()
        if signal_session and not strategy.endswith(f"_{signal_session}"):
            strategy = f"{strategy}_{signal_session}"
        evidence = row.get("evidence")
        if evidence is None:
            evidence = {}
        conn.execute("""
            INSERT INTO strategy_signals (
                signal_date, strategy, side, symbol,
                signal_session, signal_session_label, signal_time,
                name, decision, score, model_version,
                price, buy_point, stop_price, target_price,
                trade_horizon, trade_horizon_label, trade_horizon_days, trade_horizon_score,
                data_date, data_source,
                decision_source, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_date, strategy, side, symbol) DO UPDATE SET
                signal_session = excluded.signal_session,
                signal_session_label = excluded.signal_session_label,
                signal_time = excluded.signal_time,
                name = excluded.name,
                decision = excluded.decision,
                score = excluded.score,
                model_version = excluded.model_version,
                price = excluded.price,
                buy_point = excluded.buy_point,
                stop_price = excluded.stop_price,
                target_price = excluded.target_price,
                trade_horizon = excluded.trade_horizon,
                trade_horizon_label = excluded.trade_horizon_label,
                trade_horizon_days = excluded.trade_horizon_days,
                trade_horizon_score = excluded.trade_horizon_score,
                data_date = excluded.data_date,
                data_source = excluded.data_source,
                decision_source = excluded.decision_source,
                evidence_json = excluded.evidence_json,
                updated_at = excluded.updated_at
        """, (
            signal_date,
            strategy,
            side,
            symbol,
            signal_session,
            row.get("signalSessionLabel") or row.get("signal_session_label"),
            row.get("signalTime") or row.get("signal_time"),
            row.get("name"),
            row.get("decision"),
            row.get("score"),
            row.get("modelVersion") or row.get("model_version"),
            row.get("price"),
            row.get("buyPoint") or row.get("buy_point"),
            row.get("stopPrice") or row.get("stop_price"),
            row.get("targetPrice") or row.get("target_price"),
            row.get("tradeHorizon") or row.get("trade_horizon"),
            row.get("tradeHorizonLabel") or row.get("trade_horizon_label"),
            row.get("tradeHorizonDays") or row.get("trade_horizon_days"),
            row.get("tradeHorizonScore") or row.get("trade_horizon_score"),
            row.get("dataDate") or row.get("data_date") or signal_date,
            row.get("dataSource") or row.get("data_source"),
            row.get("decisionSource") or row.get("decision_source"),
            json.dumps(evidence, ensure_ascii=False),
            now_text(),
            now_text(),
        ))

    def record_strategy_signals(self, payload):
        signals = payload.get("signals") if isinstance(payload, dict) else payload
        if isinstance(signals, dict):
            signals = [signals]
        if not isinstance(signals, list):
            raise ValueError("signals must be a list")
        count = 0
        errors = []
        with self.connect() as conn:
            for signal in signals[:500]:
                try:
                    self.save_strategy_signal(conn, signal or {})
                    count += 1
                except Exception as exc:
                    errors.append({
                        "symbol": str((signal or {}).get("symbol") or ""),
                        "error": str(exc),
                    })
        return {"ok": True, "count": count, "errors": errors[:20]}


    def update_strategy_signal_outcomes(self, limit=600):
        limit = max(1, min(int(limit or 600), 5000))
        reserved = tuple(sorted(RESERVED_PRODUCTION_STRATEGIES))
        placeholders = ",".join("?" for _ in reserved)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            signals = conn.execute(f"""
                SELECT *
                FROM strategy_signals
                WHERE return_60d IS NULL
                  AND LOWER(TRIM(strategy)) NOT IN ({placeholders})
                ORDER BY signal_date ASC, updated_at ASC
                LIMIT ?
            """, (*reserved, limit)).fetchall()
            inactive_periods = self.inactive_symbol_periods(conn)

        signals = [
            signal for signal in signals
            if not self.symbol_inactive_on(
                signal["symbol"], signal["signal_date"], inactive_periods
            )
        ]
        if not signals:
            return 0

        symbols = sorted({str(signal["symbol"]) for signal in signals})
        minimum_date = min(str(signal["signal_date"]) for signal in signals)
        price_rows = {}
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            for offset in range(0, len(symbols), 400):
                chunk = symbols[offset:offset + 400]
                symbol_placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(f"""
                    SELECT * FROM prices
                    WHERE symbol IN ({symbol_placeholders}) AND date >= ?
                    ORDER BY symbol, date
                """, (*chunk, minimum_date)).fetchall()
                for row in rows:
                    item = dict(row)
                    if not is_official_source(item.get("price_source")):
                        continue
                    if self.symbol_inactive_on(item.get("symbol"), item.get("date"), inactive_periods):
                        continue
                    price_rows.setdefault(str(item["symbol"]), []).append(item)

        date_indexes = {
            symbol: [str(row.get("date") or "") for row in rows]
            for symbol, rows in price_rows.items()
        }
        outcome_columns = [
            *(f"return_{days}d" for days in (1, 3, 5, 10, 20, 60)),
            *(f"hit_{days}d" for days in (1, 3, 5, 10, 20, 60)),
            "max_drawdown_10d",
            "stopped_first",
        ]
        update_rows = []
        outcome_at = now_text()
        for signal in signals:
            symbol = str(signal["symbol"])
            all_rows = price_rows.get(symbol) or []
            dates = date_indexes.get(symbol) or []
            start_index = bisect.bisect_left(dates, str(signal["signal_date"]))
            rows = all_rows[start_index:]
            if len(rows) < 2:
                continue
            base_close = self.safe_float(signal["price"]) or self.safe_float(rows[0].get("close")) or 0
            if base_close <= 0:
                continue
            updates = {}
            side_text = str(signal["side"] or "").upper()
            is_exit_signal = "SELL" in side_text or "EXIT" in side_text
            for days, buy_hit_threshold in ((1, 0.0), (3, 0.0), (5, 0.02), (10, 0.05), (20, 0.08), (60, 0.15)):
                if len(rows) <= days:
                    continue
                outcome_close = self.safe_float(rows[days].get("close"))
                if outcome_close is None:
                    continue
                outcome_return = (outcome_close - base_close) / base_close
                updates[f"return_{days}d"] = outcome_return
                if is_exit_signal:
                    updates[f"hit_{days}d"] = 1 if outcome_return <= 0 else 0
                else:
                    updates[f"hit_{days}d"] = 1 if outcome_return >= buy_hit_threshold else 0

            future_window = rows[1:min(len(rows), 11)]
            if future_window:
                lows = [
                    self.safe_float(row.get("low")) or self.safe_float(row.get("close"))
                    for row in future_window
                ]
                lows = [value for value in lows if value is not None]
                if lows:
                    updates["max_drawdown_10d"] = (min(lows) - base_close) / base_close
                stop_price = self.safe_float(signal["stop_price"])
                target_price = self.safe_float(signal["target_price"] or signal["buy_point"])
                if stop_price and stop_price > 0:
                    stop_index = None
                    target_index = None
                    for index, row in enumerate(future_window, start=1):
                        low = self.safe_float(row.get("low")) or self.safe_float(row.get("close")) or 0
                        high = self.safe_float(row.get("high")) or self.safe_float(row.get("close")) or 0
                        if stop_index is None and low <= stop_price:
                            stop_index = index
                        if target_price and target_index is None and high >= target_price:
                            target_index = index
                    if stop_index is not None:
                        updates["stopped_first"] = 1 if target_index is None or stop_index <= target_index else 0
                    elif target_index is not None:
                        updates["stopped_first"] = 0

            if not updates:
                continue
            values = [
                updates.get(column, signal[column])
                for column in outcome_columns
            ]
            values.extend([
                outcome_at,
                signal["signal_date"], signal["strategy"], signal["side"], signal["symbol"],
            ])
            update_rows.append(tuple(values))

        if not update_rows:
            return 0
        assignments = ", ".join(f"{column} = ?" for column in outcome_columns)
        with self.connect() as conn:
            conn.executemany(f"""
                UPDATE strategy_signals
                SET {assignments}, outcome_updated_at = ?
                WHERE signal_date = ? AND strategy = ? AND side = ? AND symbol = ?
            """, update_rows)
        return len(update_rows)

    def is_actionable_strategy_side(self, side):
        text = str(side or "").upper()
        if not text:
            return False
        non_actionable = ("WATCH", "WAIT", "OBSERVE", "HOLD", "RISK_GUARD")
        if any(key in text for key in non_actionable):
            return False
        return any(key in text for key in ("BUY", "SELL", "EXIT", "CONFIRM"))

    def strategy_return_for_side(self, side, raw_return):
        value = float(raw_return or 0)
        side_text = str(side or "").upper()
        return -value if ("SELL" in side_text or "EXIT" in side_text) else value

    def strategy_horizon_metrics(self, rows, days):
        signal_count = len(rows)
        pending = sum(1 for row in rows if row[f"return_{days}d"] is None)
        completed_rows = [row for row in rows if row[f"return_{days}d"] is not None]
        actionable_rows = [
            row for row in completed_rows
            if self.is_actionable_strategy_side(row["side"])
        ]
        returns = [
            self.strategy_return_for_side(row["side"], row[f"return_{days}d"])
            for row in actionable_rows
        ]
        hits = sum(1 for row in actionable_rows if row[f"hit_{days}d"])
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        gain = sum(wins)
        loss = abs(sum(losses))
        drawdowns = [
            float(row["max_drawdown_10d"])
            for row in actionable_rows
            if row["max_drawdown_10d"] is not None
        ]
        return {
            "days": days,
            "signals": signal_count,
            "pending": pending,
            "completed": len(completed_rows),
            "actionableSamples": len(actionable_rows),
            "hits": hits,
            "precision": hits / len(actionable_rows) if actionable_rows else None,
            "averageReturn": sum(returns) / len(returns) if returns else None,
            "profitFactor": gain / loss if loss else (None if not gain else 999.0),
            "maxDrawdown": min(drawdowns) if drawdowns else None,
        }

    def strategy_signal_session_groups(self, rows):
        session_meta = {
            "open_0905": {"label": "開盤初篩", "time": "09:05", "primary": "5d", "order": 10},
            "volume_0915": {"label": "量能確認", "time": "09:15", "primary": "5d", "order": 20},
            "intraday_0930": {"label": "盤中確認", "time": "09:30", "primary": "5d", "order": 30},
            "preclose_1320": {"label": "收盤前", "time": "13:20", "primary": "10d", "order": 40},
            "close_1520": {"label": "收盤後", "time": "15:20", "primary": "20d", "order": 50},
            "manual": {"label": "手動快照", "time": "", "primary": "5d", "order": 60},
            "legacy": {"label": "未分時段", "time": "", "primary": "5d", "order": 90},
        }
        buckets = {}
        for row in rows:
            key = str(row["signal_session"] or "").strip() or "legacy"
            meta = session_meta.get(key, {})
            bucket = buckets.setdefault(key, {
                "key": key,
                "label": str(row["signal_session_label"] or meta.get("label") or key),
                "time": str(row["signal_time"] or meta.get("time") or ""),
                "primary": meta.get("primary") or "5d",
                "order": int(meta.get("order") or 80),
                "rows": [],
            })
            bucket["rows"].append(row)
        result = []
        for bucket in buckets.values():
            bucket_rows = bucket.pop("rows")
            primary = bucket.get("primary") or "5d"
            horizons = {
                f"{days}d": self.strategy_horizon_metrics(bucket_rows, days)
                for days in (1, 3, 5, 10, 20, 60)
            }
            result.append({
                **bucket,
                "signals": len(bucket_rows),
                "actionableSignals": sum(1 for row in bucket_rows if self.is_actionable_strategy_side(row["side"])),
                "horizons": horizons,
                "primaryMetrics": horizons.get(primary) or horizons.get("5d") or {},
            })
        return sorted(result, key=lambda item: (item.get("order", 80), item.get("key") or ""))

    def strategy_signal_detail(self, row):
        side = str(row["side"] or "")

        def raw(key):
            value = row[key]
            return float(value) if value is not None else None

        horizons = {}
        for days in (1, 3, 5, 10, 20, 60):
            ret = raw(f"return_{days}d")
            horizons[f"{days}d"] = {
                "return": ret,
                "adjustedReturn": self.strategy_return_for_side(side, ret) if ret is not None else None,
                "hit": None if row[f"hit_{days}d"] is None else bool(row[f"hit_{days}d"]),
            }
        return {
            "signalDate": row["signal_date"],
            "signalSession": row["signal_session"] or "",
            "signalSessionLabel": row["signal_session_label"] or "",
            "signalTime": row["signal_time"] or "",
            "strategy": row["strategy"],
            "side": side,
            "symbol": row["symbol"],
            "name": row["name"] or "",
            "decision": row["decision"] or "",
            "score": raw("score"),
            "modelVersion": row["model_version"] or "",
            "price": raw("price"),
            "buyPoint": raw("buy_point"),
            "stopPrice": raw("stop_price"),
            "targetPrice": raw("target_price"),
            "tradeHorizon": row["trade_horizon"] or "",
            "tradeHorizonLabel": row["trade_horizon_label"] or "",
            "tradeHorizonDays": row["trade_horizon_days"] or "",
            "tradeHorizonScore": raw("trade_horizon_score"),
            "dataDate": row["data_date"] or "",
            "dataSource": row["data_source"] or "",
            "decisionSource": row["decision_source"] or "",
            "maxDrawdown10d": raw("max_drawdown_10d"),
            "stoppedFirst": None if row["stopped_first"] is None else bool(row["stopped_first"]),
            "outcomeUpdatedAt": row["outcome_updated_at"] or "",
            "horizons": horizons,
        }

    def strategy_signal_paper_trades(self, rows):
        paper_cash = float(PAPER_INITIAL_CAPITAL)
        peak_open_positions = 0

        def round_twd(value):
            """Round simulated fees to whole TWD, with .5 rounded upward."""
            return int(math.floor(max(0.0, float(value or 0)) + 0.5))

        def paper_pnl(entry_price, valuation_price):
            entry = self.safe_float(entry_price) or 0
            valuation = self.safe_float(valuation_price) or 0
            if entry <= 0 or valuation <= 0:
                return {
                    "shares": PAPER_TRADE_SHARES,
                    "entryAmount": None,
                    "exitAmount": None,
                    "buyCommission": None,
                    "sellCommission": None,
                    "sellTax": None,
                    "buySlippageCost": None,
                    "sellSlippageCost": None,
                    "totalSlippageCost": None,
                    "totalCosts": None,
                    "grossPnlPerLot": None,
                    "netPnlPerLot": None,
                    "netReturnPct": None,
                }
            entry_amount = entry * PAPER_TRADE_SHARES
            exit_amount = valuation * PAPER_TRADE_SHARES
            buy_commission = round_twd(entry_amount * PAPER_BUY_COMMISSION_RATE)
            sell_commission = round_twd(exit_amount * PAPER_SELL_COMMISSION_RATE)
            sell_tax = round_twd(exit_amount * PAPER_SELL_TAX_RATE)
            buy_slippage = round_twd(entry_amount * PAPER_BASE_SLIPPAGE_RATE)
            sell_slippage = round_twd(exit_amount * PAPER_BASE_SLIPPAGE_RATE)
            total_slippage = buy_slippage + sell_slippage
            total_costs = buy_commission + sell_commission + sell_tax + total_slippage
            gross_pnl = exit_amount - entry_amount
            net_pnl = gross_pnl - total_costs
            return {
                "shares": PAPER_TRADE_SHARES,
                "entryAmount": entry_amount,
                "exitAmount": exit_amount,
                "buyCommission": buy_commission,
                "sellCommission": sell_commission,
                "sellTax": sell_tax,
                "buySlippageCost": buy_slippage,
                "sellSlippageCost": sell_slippage,
                "totalSlippageCost": total_slippage,
                "totalCosts": total_costs,
                "grossPnlPerLot": gross_pnl,
                "netPnlPerLot": net_pnl,
                "netReturnPct": (
                    net_pnl / (entry_amount + buy_commission + buy_slippage)
                    if entry_amount > 0 else None
                ),
            }

        def row_text(row, key):
            return str(row[key] or "").strip()

        def is_buy(row):
            return "BUY" in row_text(row, "side").upper()

        def is_sell(row):
            side = row_text(row, "side").upper()
            return "EXIT" in side

        def days_between(start, end):
            try:
                d0 = dt.datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
                d1 = dt.datetime.strptime(str(end)[:10], "%Y-%m-%d").date()
                return (d1 - d0).days
            except Exception:
                return None

        price_rows_cache = {}

        def price_rows(symbol):
            key = str(symbol or "").strip()
            if not key:
                return []
            if key not in price_rows_cache:
                try:
                    price_rows_cache[key] = self.rows_with_verified_sources(self.load_price_rows(key))
                except Exception:
                    price_rows_cache[key] = []
            return price_rows_cache[key]

        def row_price(row, key):
            return self.safe_float(row.get(key) if hasattr(row, "get") else row[key])

        def signal_row_index(symbol, signal_date):
            rows_for_symbol = price_rows(symbol)
            for index, price_row in enumerate(rows_for_symbol):
                if str(price_row.get("date") or "") >= str(signal_date or "")[:10]:
                    return rows_for_symbol, index
            return rows_for_symbol, None

        def same_day_fill_session(row):
            return row_text(row, "signal_session") in {"open_0905", "volume_0915", "intraday_0930", "preclose_1320"}

        def volume_capacity(price_row):
            volume_shares = self.safe_float(price_row.get("volume"))
            if volume_shares is None or volume_shares <= 0:
                return False, None, volume_shares
            participation = PAPER_TRADE_SHARES / volume_shares
            return participation <= PAPER_MAX_VOLUME_PARTICIPATION, participation, volume_shares

        def is_locked_limit(rows_for_symbol, index, direction):
            if index <= 0 or index >= len(rows_for_symbol):
                return False
            previous_close = self.safe_float(rows_for_symbol[index - 1].get("close")) or 0
            price_row = rows_for_symbol[index]
            if previous_close <= 0:
                return False
            if direction == "up":
                low = self.safe_float(price_row.get("low")) or 0
                return low >= previous_close * (1 + PAPER_LIMIT_MOVE_THRESHOLD)
            high = self.safe_float(price_row.get("high")) or 0
            return 0 < high <= previous_close * (1 - PAPER_LIMIT_MOVE_THRESHOLD)

        def fill_metadata(price_row, participation, volume_shares):
            return {
                "volumeShares": volume_shares,
                "volumeParticipation": participation,
                "maxVolumeParticipation": PAPER_MAX_VOLUME_PARTICIPATION,
            }

        def entry_fill(row, signal_price):
            symbol = row_text(row, "symbol")
            signal_date = row_text(row, "signal_date")[:10]
            rows_for_symbol, index = signal_row_index(symbol, signal_date)
            if index is None or not rows_for_symbol:
                return {
                    "filled": False,
                    "date": "",
                    "price": None,
                    "mode": "missing_market_data",
                    "note": "缺少官方日K，不能假設訊號價可成交",
                }
            exact_signal_day = str(rows_for_symbol[index].get("date") or "")[:10] == signal_date
            same_day = same_day_fill_session(row) and exact_signal_day
            fill_index = index if same_day or not exact_signal_day else index + 1
            if fill_index >= len(rows_for_symbol):
                return {
                    "filled": False,
                    "date": "",
                    "price": None,
                    "mode": "pending_next_open",
                    "note": "等待下一個交易日開盤資料",
                }
            price_row = rows_for_symbol[fill_index]
            open_price = self.safe_float(price_row.get("open")) or self.safe_float(price_row.get("close")) or signal_price
            high = self.safe_float(price_row.get("high")) or open_price
            low = self.safe_float(price_row.get("low")) or open_price
            capacity_ok, participation, volume_shares = volume_capacity(price_row)
            metadata = fill_metadata(price_row, participation, volume_shares)
            if is_locked_limit(rows_for_symbol, fill_index, "up"):
                return {
                    "filled": False,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": None,
                    "mode": "locked_limit_up",
                    "note": "整日鎖在漲停附近，買單不假設能排隊成交",
                    **metadata,
                }
            if not capacity_ok:
                return {
                    "filled": False,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": None,
                    "mode": "insufficient_liquidity" if volume_shares else "missing_volume",
                    "note": "一張超過當日成交量 5%，不假設可完整成交",
                    **metadata,
                }
            trigger = row_price(row, "buy_point") or signal_price
            if trigger and low <= trigger <= high:
                return {
                    "filled": True,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": trigger,
                    "mode": "touch_buy_point",
                    "note": "日K區間觸及模型買點",
                    **metadata,
                }
            if same_day:
                if signal_price and low <= signal_price <= high:
                    return {
                        "filled": True,
                        "date": str(price_row.get("date") or "")[:10],
                        "price": signal_price,
                        "mode": "same_day_signal_price",
                        "note": "盤中時段用訊號價成交模擬",
                        **metadata,
                    }
                return {
                    "filled": False,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": None,
                    "mode": "not_touched",
                    "note": "當日高低價未觸及買點/訊號價",
                    **metadata,
                }
            return {
                "filled": True,
                "date": str(price_row.get("date") or "")[:10],
                "price": open_price,
                "mode": "next_open",
                "note": "收盤後/未分時段訊號，採下一交易日開盤成交",
                **metadata,
            }

        def exit_fill(row, signal_price):
            symbol = row_text(row, "symbol")
            signal_date = row_text(row, "signal_date")[:10]
            rows_for_symbol, index = signal_row_index(symbol, signal_date)
            if index is None or not rows_for_symbol:
                return {
                    "filled": False,
                    "date": "",
                    "price": None,
                    "mode": "missing_market_data",
                    "note": "缺少官方日K，不能假設模型賣價可成交",
                }
            exact_signal_day = str(rows_for_symbol[index].get("date") or "")[:10] == signal_date
            same_day = same_day_fill_session(row) and exact_signal_day
            fill_index = index if same_day or not exact_signal_day else index + 1
            if fill_index >= len(rows_for_symbol):
                return {
                    "filled": False,
                    "date": "",
                    "price": None,
                    "mode": "pending_next_open",
                    "note": "等待下一個交易日開盤資料",
                }
            price_row = rows_for_symbol[fill_index]
            open_price = self.safe_float(price_row.get("open")) or self.safe_float(price_row.get("close")) or signal_price
            high = self.safe_float(price_row.get("high")) or open_price
            low = self.safe_float(price_row.get("low")) or open_price
            capacity_ok, participation, volume_shares = volume_capacity(price_row)
            metadata = fill_metadata(price_row, participation, volume_shares)
            if is_locked_limit(rows_for_symbol, fill_index, "down"):
                return {
                    "filled": False,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": None,
                    "mode": "locked_limit_down",
                    "note": "整日鎖在跌停附近，賣單不假設能成交",
                    **metadata,
                }
            if not capacity_ok:
                return {
                    "filled": False,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": None,
                    "mode": "insufficient_liquidity" if volume_shares else "missing_volume",
                    "note": "一張超過當日成交量 5%，不假設可完整成交",
                    **metadata,
                }
            if same_day:
                if signal_price and low <= signal_price <= high:
                    return {
                        "filled": True,
                        "date": str(price_row.get("date") or "")[:10],
                        "price": signal_price,
                        "mode": "same_day_signal_price",
                        "note": "盤中模型賣訊以訊號價成交模擬",
                        **metadata,
                    }
                return {
                    "filled": False,
                    "date": str(price_row.get("date") or "")[:10],
                    "price": None,
                    "mode": "not_touched",
                    "note": "當日高低價未觸及模型賣價",
                    **metadata,
                }
            return {
                "filled": True,
                "date": str(price_row.get("date") or "")[:10],
                "price": open_price,
                "mode": "next_open",
                "note": "收盤後/未分時段賣訊，採下一交易日開盤成交",
                **metadata,
            }

        def max_holding_sessions(entry):
            horizon = str(entry.get("entryTradeHorizon") or "").strip().lower()
            mapped = {
                "short_trade": 5,
                "mid_swing": 20,
                "long_trend": 60,
            }
            if horizon in mapped:
                return mapped[horizon]
            day_text = str(entry.get("entryTradeHorizonDays") or "")
            values = [int(value) for value in re.findall(r"\d+", day_text)]
            return max(values) if values else PAPER_DEFAULT_MAX_HOLD_SESSIONS

        def risk_exit(entry, until_date=None):
            stop = self.safe_float(entry.get("entryStopPrice"))
            target = self.safe_float(entry.get("entryTargetPrice"))
            max_sessions = max_holding_sessions(entry)
            rows_for_symbol = price_rows(entry.get("symbol"))
            start_date = str(entry.get("entryFillDate") or entry.get("entryDate") or "")[:10]
            end_date = str(until_date or "")[:10]
            blocked_exit_days = 0
            held_sessions = 0
            for index, price_row in enumerate(rows_for_symbol):
                date = str(price_row.get("date") or "")[:10]
                if date <= start_date:
                    continue
                if end_date and date > end_date:
                    break
                held_sessions += 1
                low = self.safe_float(price_row.get("low")) or self.safe_float(price_row.get("close")) or 0
                high = self.safe_float(price_row.get("high")) or self.safe_float(price_row.get("close")) or 0
                open_price = self.safe_float(price_row.get("open")) or self.safe_float(price_row.get("close")) or 0
                if stop and low and low <= stop:
                    capacity_ok, participation, volume_shares = volume_capacity(price_row)
                    if is_locked_limit(rows_for_symbol, index, "down") or not capacity_ok:
                        blocked_exit_days += 1
                        continue
                    exit_price = open_price if open_price and open_price < stop else stop
                    return {
                        "exitDate": date,
                        "exitPrice": exit_price,
                        "exitReason": "stop_loss",
                        "exitDecision": "跳空後以開盤價停損" if exit_price < stop else "觸及模型停損",
                        "exitFillMode": "gap_open" if exit_price < stop else "stop_touch",
                        "blockedExitDays": blocked_exit_days,
                        **fill_metadata(price_row, participation, volume_shares),
                    }
                if target and high and high >= target:
                    capacity_ok, participation, volume_shares = volume_capacity(price_row)
                    if not capacity_ok:
                        blocked_exit_days += 1
                        continue
                    exit_price = open_price if open_price and open_price > target else target
                    return {
                        "exitDate": date,
                        "exitPrice": exit_price,
                        "exitReason": "take_profit",
                        "exitDecision": "跳空後以開盤價停利" if exit_price > target else "觸及模型停利",
                        "exitFillMode": "gap_open" if exit_price > target else "target_touch",
                        "blockedExitDays": blocked_exit_days,
                        **fill_metadata(price_row, participation, volume_shares),
                    }
                if held_sessions > max_sessions:
                    capacity_ok, participation, volume_shares = volume_capacity(price_row)
                    if is_locked_limit(rows_for_symbol, index, "down") or not capacity_ok:
                        blocked_exit_days += 1
                        continue
                    return {
                        "exitDate": date,
                        "exitPrice": open_price,
                        "exitReason": "time_exit",
                        "exitDecision": f"持有週期已滿 {max_sessions} 個交易日，隔日開盤出場",
                        "exitFillMode": "horizon_next_open",
                        "maxHoldingSessions": max_sessions,
                        "heldSessions": held_sessions,
                        "blockedExitDays": blocked_exit_days,
                        **fill_metadata(price_row, participation, volume_shares),
                    }
            return None

        def close_trade(entry, exit_payload):
            nonlocal paper_cash
            exit_price = self.safe_float(exit_payload.get("exitPrice"))
            ret = exit_price / entry["entryPrice"] - 1 if exit_price and entry["entryPrice"] > 0 else None
            pnl = paper_pnl(entry["entryPrice"], exit_price)
            cash_before_exit = paper_cash
            if pnl["exitAmount"] is not None:
                paper_cash += (
                    pnl["exitAmount"]
                    - pnl["sellCommission"]
                    - pnl["sellTax"]
                    - pnl["sellSlippageCost"]
                )
            return {
                **entry,
                **exit_payload,
                "returnPct": ret,
                # Keep historical gross fields for calibration/API compatibility.
                "pnlPerLot": pnl["grossPnlPerLot"],
                **pnl,
                "cashBeforeExit": cash_before_exit,
                "cashAfterExit": paper_cash,
                "holdDays": days_between(entry["entryFillDate"] or entry["entryDate"], exit_payload.get("exitDate")),
            }

        orphan_reason_labels = {
            "real_holding_exit_without_paper_entry": "真實持股出場，紙上帳無模型買進",
            "prior_buy_not_filled": "先前買訊未成交，後續賣訊無持倉",
            "position_closed_by_risk_before_sell": "已被停利/停損/週期到期先平倉",
            "sell_without_model_entry": "賣訊前沒有模型買進",
        }

        def orphan_reason(row, symbol):
            strategy = row_text(row, "strategy").lower()
            if strategy.startswith("portfolio_exit"):
                return "real_holding_exit_without_paper_entry"
            if symbol in risk_closed_symbols:
                return "position_closed_by_risk_before_sell"
            if symbol in unfilled_by_symbol:
                return "prior_buy_not_filled"
            return "sell_without_model_entry"

        def register_unfilled_buy(row, signal_price, fill):
            symbol = row_text(row, "symbol")
            unfilled = {
                "entryStrategy": row_text(row, "strategy"),
                "symbol": symbol,
                "name": row["name"] or "",
                "entryDate": row_text(row, "signal_date"),
                "entrySide": row_text(row, "side"),
                "entryDecision": row["decision"] or "",
                "signalPrice": signal_price,
                "entryFillMode": fill.get("mode"),
                "entryFillNote": fill.get("note"),
                "entryVolumeShares": fill.get("volumeShares"),
                "entryVolumeParticipation": fill.get("volumeParticipation"),
                "entryCapitalRequired": fill.get("capitalRequired"),
                "paperCashAvailable": fill.get("cashAvailable"),
            }
            unfilled_buys.append(unfilled)
            unfilled_by_symbol.setdefault(symbol, []).append(unfilled)

        ordered = sorted(
            [row for row in rows if self.is_actionable_strategy_side(row["side"])],
            key=lambda row: (
                row_text(row, "signal_date"),
                row_text(row, "updated_at"),
                row_text(row, "strategy"),
                row_text(row, "symbol"),
            ),
        )
        open_positions = {}
        closed = []
        ignored_buys = 0
        unfilled_buys = []
        unfilled_sells = []
        unfilled_by_symbol = {}
        risk_closed_symbols = set()
        orphan_sells = []

        for row in ordered:
            price = self.safe_float(row["price"])
            if price is None or price <= 0:
                continue
            key = row_text(row, "symbol")
            existing = open_positions.get(key)
            if existing:
                triggered = risk_exit(existing, row_text(row, "signal_date"))
                if triggered:
                    open_positions.pop(key, None)
                    closed.append(close_trade(existing, {
                        "exitStrategy": "risk_exit",
                        "exitSide": "RISK_EXIT",
                        "exitSession": "",
                        "exitSessionLabel": "",
                        "exitSignalTime": "",
                        "exitScore": None,
                        "exitTradeHorizon": "",
                        "exitTradeHorizonLabel": "",
                        "exitTradeHorizonDays": "",
                        **triggered,
                    }))
                    risk_closed_symbols.add(key)
            if is_buy(row):
                if key in open_positions:
                    ignored_buys += 1
                    continue
                fill = entry_fill(row, price)
                if not fill.get("filled"):
                    register_unfilled_buy(row, price, fill)
                    continue
                entry_price = self.safe_float(fill.get("price")) or price
                entry_amount = entry_price * PAPER_TRADE_SHARES
                entry_buy_commission = round_twd(entry_amount * PAPER_BUY_COMMISSION_RATE)
                entry_slippage = round_twd(entry_amount * PAPER_BASE_SLIPPAGE_RATE)
                capital_required = entry_amount + entry_buy_commission + entry_slippage
                if len(open_positions) >= PAPER_MAX_OPEN_POSITIONS:
                    register_unfilled_buy(row, price, {
                        **fill,
                        "filled": False,
                        "mode": "portfolio_position_limit",
                        "note": f"紙上帳同時持股上限 {PAPER_MAX_OPEN_POSITIONS} 檔，這筆不成交",
                        "capitalRequired": capital_required,
                        "cashAvailable": paper_cash,
                    })
                    continue
                if capital_required > paper_cash:
                    register_unfilled_buy(row, price, {
                        **fill,
                        "filled": False,
                        "mode": "insufficient_paper_cash",
                        "note": "紙上帳可用現金不足，不使用虛構槓桿成交",
                        "capitalRequired": capital_required,
                        "cashAvailable": paper_cash,
                    })
                    continue
                cash_before_entry = paper_cash
                paper_cash -= capital_required
                open_positions[key] = {
                    "entryStrategy": row_text(row, "strategy"),
                    "symbol": row_text(row, "symbol"),
                    "name": row["name"] or "",
                    "entryDate": row_text(row, "signal_date"),
                    "entryFillDate": fill.get("date") or row_text(row, "signal_date"),
                    "entryFillMode": fill.get("mode"),
                    "entryFillNote": fill.get("note"),
                    "entryVolumeShares": fill.get("volumeShares"),
                    "entryVolumeParticipation": fill.get("volumeParticipation"),
                    "maxVolumeParticipation": fill.get("maxVolumeParticipation"),
                    "entrySignalPrice": price,
                    "entrySession": row_text(row, "signal_session"),
                    "entrySessionLabel": row_text(row, "signal_session_label"),
                    "entrySignalTime": row_text(row, "signal_time"),
                    "entrySide": row_text(row, "side"),
                    "entryDecision": row["decision"] or "",
                    "entryPrice": entry_price,
                    "entryAmount": entry_amount,
                    "entryBuyCommission": entry_buy_commission,
                    "entrySlippageCost": entry_slippage,
                    "entryCapitalRequired": capital_required,
                    "cashBeforeEntry": cash_before_entry,
                    "cashAfterEntry": paper_cash,
                    "entryStopPrice": row_price(row, "stop_price"),
                    "entryTargetPrice": row_price(row, "target_price") or row_price(row, "buy_point"),
                    "entryScore": self.safe_float(row["score"]),
                    "entryTradeHorizon": row_text(row, "trade_horizon"),
                    "entryTradeHorizonLabel": row_text(row, "trade_horizon_label"),
                    "entryTradeHorizonDays": row_text(row, "trade_horizon_days"),
                }
                peak_open_positions = max(peak_open_positions, len(open_positions))
            elif is_sell(row):
                entry = open_positions.get(key)
                if not entry:
                    reason = orphan_reason(row, key)
                    orphan_sells.append({
                        "exitStrategy": row_text(row, "strategy"),
                        "symbol": row_text(row, "symbol"),
                        "name": row["name"] or "",
                        "exitDate": row_text(row, "signal_date"),
                        "exitSession": row_text(row, "signal_session"),
                        "exitSessionLabel": row_text(row, "signal_session_label"),
                        "exitSignalTime": row_text(row, "signal_time"),
                        "exitSide": row_text(row, "side"),
                        "exitDecision": row["decision"] or "",
                        "exitPrice": price,
                        "exitTradeHorizon": row_text(row, "trade_horizon"),
                        "exitTradeHorizonLabel": row_text(row, "trade_horizon_label"),
                        "exitTradeHorizonDays": row_text(row, "trade_horizon_days"),
                        "exitReason": "model_exit_without_position",
                        "orphanReason": reason,
                        "orphanReasonLabel": orphan_reason_labels.get(reason, reason),
                        "isModelOrphan": reason != "real_holding_exit_without_paper_entry",
                    })
                    continue
                fill = exit_fill(row, price)
                if not fill.get("filled"):
                    unfilled_sells.append({
                        "exitStrategy": row_text(row, "strategy"),
                        "symbol": row_text(row, "symbol"),
                        "name": row["name"] or "",
                        "exitDate": row_text(row, "signal_date"),
                        "exitSide": row_text(row, "side"),
                        "exitDecision": row["decision"] or "",
                        "signalPrice": price,
                        "exitFillMode": fill.get("mode"),
                        "exitFillNote": fill.get("note"),
                        "exitVolumeShares": fill.get("volumeShares"),
                        "exitVolumeParticipation": fill.get("volumeParticipation"),
                    })
                    continue
                open_positions.pop(key, None)
                closed.append(close_trade(entry, {
                    "exitDate": fill.get("date") or row_text(row, "signal_date"),
                    "exitSession": row_text(row, "signal_session"),
                    "exitSessionLabel": row_text(row, "signal_session_label"),
                    "exitSignalTime": row_text(row, "signal_time"),
                    "exitStrategy": row_text(row, "strategy"),
                    "exitSide": row_text(row, "side"),
                    "exitDecision": row["decision"] or "",
                    "exitPrice": self.safe_float(fill.get("price")) or price,
                    "exitSignalPrice": price,
                    "exitFillMode": fill.get("mode"),
                    "exitFillNote": fill.get("note"),
                    "exitVolumeShares": fill.get("volumeShares"),
                    "exitVolumeParticipation": fill.get("volumeParticipation"),
                    "exitScore": self.safe_float(row["score"]),
                    "exitTradeHorizon": row_text(row, "trade_horizon"),
                    "exitTradeHorizonLabel": row_text(row, "trade_horizon_label"),
                    "exitTradeHorizonDays": row_text(row, "trade_horizon_days"),
                    "exitReason": "model_exit",
                }))
                risk_closed_symbols.discard(key)

        for key, entry in list(open_positions.items()):
            triggered = risk_exit(entry)
            if triggered:
                open_positions.pop(key, None)
                closed.append(close_trade(entry, {
                    "exitStrategy": "risk_exit",
                    "exitSide": "RISK_EXIT",
                    "exitSession": "",
                    "exitSessionLabel": "",
                    "exitSignalTime": "",
                    "exitScore": None,
                    "exitTradeHorizon": "",
                    "exitTradeHorizonLabel": "",
                    "exitTradeHorizonDays": "",
                    **triggered,
                }))
                risk_closed_symbols.add(key)

        latest_by_symbol = {}
        symbols = sorted({entry["symbol"] for entry in open_positions.values() if entry.get("symbol")})
        if symbols:
            try:
                placeholders = ",".join("?" for _ in symbols)
                with self.connect() as conn:
                    conn.row_factory = sqlite3.Row
                    latest_rows = conn.execute(f"""
                        SELECT p.symbol, p.date, p.close
                        FROM prices p
                        JOIN (
                            SELECT symbol, MAX(date) AS max_date
                            FROM prices
                            WHERE symbol IN ({placeholders})
                            GROUP BY symbol
                        ) latest
                          ON p.symbol = latest.symbol
                         AND p.date = latest.max_date
                    """, symbols).fetchall()
                latest_by_symbol = {
                    str(row["symbol"]): {
                        "date": str(row["date"] or ""),
                        "close": self.safe_float(row["close"]),
                    }
                    for row in latest_rows
                }
            except Exception:
                latest_by_symbol = {}

        open_list = []
        for entry in open_positions.values():
            latest = latest_by_symbol.get(entry["symbol"]) or {}
            latest_price = latest.get("close")
            latest_date = latest.get("date") or ""
            unrealized = latest_price / entry["entryPrice"] - 1 if latest_price and entry["entryPrice"] > 0 else None
            pnl = paper_pnl(entry["entryPrice"], latest_price)
            estimated_liquidation_value = None
            if pnl["exitAmount"] is not None:
                estimated_liquidation_value = (
                    pnl["exitAmount"]
                    - pnl["sellCommission"]
                    - pnl["sellTax"]
                    - pnl["sellSlippageCost"]
                )
            open_list.append({
                **entry,
                "latestDate": latest_date,
                "latestPrice": latest_price,
                "unrealizedReturnPct": unrealized,
                # Backward-compatible gross mark-to-market fields.
                "unrealizedPnlPerLot": pnl["grossPnlPerLot"],
                "unrealizedGrossPnlPerLot": pnl["grossPnlPerLot"],
                "unrealizedNetPnlPerLot": pnl["netPnlPerLot"],
                "unrealizedNetReturnPct": pnl["netReturnPct"],
                "estimatedLiquidationValue": estimated_liquidation_value,
                **pnl,
            })

        returns = [trade["returnPct"] for trade in closed if trade["returnPct"] is not None]
        net_returns = [trade["netReturnPct"] for trade in closed if trade["netReturnPct"] is not None]
        wins = [value for value in net_returns if value > 0]
        losses = [value for value in net_returns if value < 0]
        break_even = [value for value in net_returns if value == 0]

        def wilson_interval(successes, samples, z=1.959963984540054):
            if samples <= 0:
                return {"low": None, "high": None, "samples": 0, "method": "wilson_95"}
            proportion = successes / samples
            denominator = 1 + (z * z / samples)
            center = (proportion + z * z / (2 * samples)) / denominator
            margin = z * math.sqrt(
                (proportion * (1 - proportion) / samples) + (z * z / (4 * samples * samples))
            ) / denominator
            return {
                "low": max(0.0, center - margin),
                "high": min(1.0, center + margin),
                "samples": samples,
                "method": "wilson_95",
            }

        confidence = wilson_interval(len(wins), len(net_returns))
        confidence_level = (
            "insufficient" if len(net_returns) < 10
            else "low" if len(net_returns) < 30
            else "medium" if len(net_returns) < 100
            else "high"
        )
        unfilled_buy_reason_counts = {}
        for trade in unfilled_buys:
            reason = str(trade.get("entryFillMode") or "unknown")
            unfilled_buy_reason_counts[reason] = unfilled_buy_reason_counts.get(reason, 0) + 1
        unfilled_sell_reason_counts = {}
        for trade in unfilled_sells:
            reason = str(trade.get("exitFillMode") or "unknown")
            unfilled_sell_reason_counts[reason] = unfilled_sell_reason_counts.get(reason, 0) + 1
        exit_reason_counts = {}
        for trade in closed:
            reason = str(trade.get("exitReason") or "unknown")
            exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1
        orphan_reason_counts = {}
        orphan_symbol_counts = {}
        for sell in orphan_sells:
            reason = str(sell.get("orphanReason") or "unknown")
            orphan_reason_counts[reason] = orphan_reason_counts.get(reason, 0) + 1
            symbol = str(sell.get("symbol") or "")
            if symbol:
                bucket = orphan_symbol_counts.setdefault(symbol, {
                    "symbol": symbol,
                    "name": sell.get("name") or "",
                    "count": 0,
                    "modelCount": 0,
                    "firstDate": sell.get("exitDate") or "",
                    "latestDate": sell.get("exitDate") or "",
                    "latestSession": sell.get("exitSessionLabel") or "",
                    "latestSignalTime": sell.get("exitSignalTime") or "",
                    "latestDecision": sell.get("exitDecision") or "",
                    "latestPrice": sell.get("exitPrice"),
                    "reason": reason,
                    "reasonLabel": sell.get("orphanReasonLabel") or reason,
                    "_dates": set(),
                })
                bucket["count"] += 1
                if sell.get("exitDate"):
                    bucket["_dates"].add(sell.get("exitDate"))
                    if not bucket["firstDate"] or str(sell.get("exitDate") or "") < bucket["firstDate"]:
                        bucket["firstDate"] = sell.get("exitDate") or ""
                if sell.get("isModelOrphan"):
                    bucket["modelCount"] += 1
                if str(sell.get("exitDate") or "") >= bucket["latestDate"]:
                    bucket["latestDate"] = sell.get("exitDate") or ""
                    bucket["latestSession"] = sell.get("exitSessionLabel") or ""
                    bucket["latestSignalTime"] = sell.get("exitSignalTime") or ""
                    bucket["latestDecision"] = sell.get("exitDecision") or ""
                    bucket["latestPrice"] = sell.get("exitPrice")
                    bucket["reason"] = reason
                    bucket["reasonLabel"] = sell.get("orphanReasonLabel") or reason
        model_orphan_sells = [sell for sell in orphan_sells if sell.get("isModelOrphan")]
        orphan_groups = []
        for item in orphan_symbol_counts.values():
            dates = item.pop("_dates", set())
            item["repeatDays"] = len(dates)
            orphan_groups.append(item)
        external_holding_states = [
            item for item in orphan_groups
            if item.get("reason") == "real_holding_exit_without_paper_entry"
        ]
        closed_gross_pnl = sum(
            trade["grossPnlPerLot"] for trade in closed if trade["grossPnlPerLot"] is not None
        )
        closed_net_pnl = sum(
            trade["netPnlPerLot"] for trade in closed if trade["netPnlPerLot"] is not None
        )
        open_gross_pnl = sum(
            trade["unrealizedGrossPnlPerLot"]
            for trade in open_list if trade["unrealizedGrossPnlPerLot"] is not None
        )
        open_net_pnl = sum(
            trade["unrealizedNetPnlPerLot"]
            for trade in open_list if trade["unrealizedNetPnlPerLot"] is not None
        )
        total_net_pnl = closed_net_pnl + open_net_pnl
        open_capital_committed = sum(
            float(trade.get("entryCapitalRequired") or 0) for trade in open_list
        )
        account_equity = PAPER_INITIAL_CAPITAL + total_net_pnl
        estimated_open_liquidation = max(0.0, account_equity - paper_cash)
        capital_skip_count = sum(
            unfilled_buy_reason_counts.get(reason, 0)
            for reason in ("insufficient_paper_cash", "portfolio_position_limit")
        )
        return {
            "simulation": {
                "shares": PAPER_TRADE_SHARES,
                "initialCapital": PAPER_INITIAL_CAPITAL,
                "maxOpenPositions": PAPER_MAX_OPEN_POSITIONS,
                "buyCommissionRate": PAPER_BUY_COMMISSION_RATE,
                "sellCommissionRate": PAPER_SELL_COMMISSION_RATE,
                "sellTaxRate": PAPER_SELL_TAX_RATE,
                "slippageRatePerSide": PAPER_BASE_SLIPPAGE_RATE,
                "maxVolumeParticipation": PAPER_MAX_VOLUME_PARTICIPATION,
                "limitMoveThreshold": PAPER_LIMIT_MOVE_THRESHOLD,
                "feeRounding": "nearest_twd",
                "note": "固定一張；雙邊滑價、成交量容量及漲跌停可成交性已納入，未套用券商折扣或當沖稅率",
            },
            "closedCount": len(closed),
            "openCount": len(open_list),
            "wins": len(wins),
            "losses": len(losses),
            "breakEven": len(break_even),
            "winRate": len(wins) / len(net_returns) if net_returns else None,
            "winRateBasis": "net_after_fees_tax_slippage",
            "winRateConfidence95": confidence,
            "confidenceLevel": confidence_level,
            "avgReturn": sum(net_returns) / len(net_returns) if net_returns else None,
            "avgGrossReturn": sum(returns) / len(returns) if returns else None,
            "avgNetReturn": sum(net_returns) / len(net_returns) if net_returns else None,
            "totalPnlPerLot": sum(trade["pnlPerLot"] for trade in closed),
            "closedGrossPnlPerLot": closed_gross_pnl,
            "closedNetPnlPerLot": closed_net_pnl,
            "closedTotalCosts": sum(trade["totalCosts"] for trade in closed if trade["totalCosts"] is not None),
            "openGrossPnlPerLot": open_gross_pnl,
            "openNetPnlPerLot": open_net_pnl,
            "totalNetPnlPerLot": total_net_pnl,
            "cashBalance": paper_cash,
            "openCapitalCommitted": open_capital_committed,
            "estimatedOpenLiquidationValue": estimated_open_liquidation,
            "accountEquity": account_equity,
            "accountReturnPct": total_net_pnl / PAPER_INITIAL_CAPITAL,
            "capitalUtilization": open_capital_committed / PAPER_INITIAL_CAPITAL,
            "peakOpenPositions": peak_open_positions,
            "capitalRejectedBuySignals": capital_skip_count,
            "ignoredBuySignals": ignored_buys,
            "unfilledBuySignals": len(unfilled_buys),
            "unfilledSellSignals": len(unfilled_sells),
            "unfilledBuyReasonCounts": unfilled_buy_reason_counts,
            "unfilledSellReasonCounts": unfilled_sell_reason_counts,
            "orphanSellSignals": len(orphan_sells),
            "modelOrphanSellSignals": len(model_orphan_sells),
            "externalHoldingExitSignals": orphan_reason_counts.get("real_holding_exit_without_paper_entry", 0),
            "externalHoldingExitSymbols": len(external_holding_states),
            "exitReasonCounts": exit_reason_counts,
            "orphanSellBreakdown": [
                {
                    "reason": reason,
                    "label": orphan_reason_labels.get(reason, reason),
                    "count": count,
                    "modelCount": 0 if reason == "real_holding_exit_without_paper_entry" else count,
                }
                for reason, count in sorted(orphan_reason_counts.items(), key=lambda item: item[1], reverse=True)
            ],
            "orphanSellGroups": sorted(
                orphan_groups,
                key=lambda item: (item["modelCount"], item["count"], item["latestDate"]),
                reverse=True,
            )[:60],
            "externalHoldingExitStates": sorted(
                external_holding_states,
                key=lambda item: (item["latestDate"], item["count"]),
                reverse=True,
            )[:80],
            "closed": sorted(closed, key=lambda trade: trade["exitDate"], reverse=True)[:120],
            "open": sorted(open_list, key=lambda trade: trade["entryDate"], reverse=True)[:120],
            "unfilledBuys": sorted(unfilled_buys, key=lambda trade: trade["entryDate"], reverse=True)[:80],
            "unfilledSells": sorted(unfilled_sells, key=lambda trade: trade["exitDate"], reverse=True)[:80],
            "orphanSells": sorted(orphan_sells, key=lambda trade: trade["exitDate"], reverse=True)[:80],
        }

    def strategy_signal_real_trade_alignment(self, signal_rows, window_days=3, limit=300):
        """Compare real local trades with model paper signals.

        BUY trades should have a recent BUY_CANDIDATE model signal. SELL/closed
        trades should have a recent EXIT/SELL model signal. This is observation
        only: it measures whether real trading lined up with the model, without
        changing model decisions or orders.
        """
        def row_text(row, key):
            try:
                return str(row[key] or "").strip()
            except Exception:
                return ""

        def parse_date(value):
            try:
                return dt.datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
            except Exception:
                return None

        def side_group(side):
            text = str(side or "").upper()
            if "BUY" in text:
                return "BUY"
            if "SELL" in text or "EXIT" in text:
                return "SELL"
            return ""

        def find_signal(symbol, group, trade_date):
            candidates = signal_index.get((symbol, group), [])
            for signal in candidates:
                signal_date = parse_date(row_text(signal, "signal_date"))
                if not trade_date or not signal_date:
                    continue
                delta = (trade_date - signal_date).days
                if 0 <= delta <= int(window_days or 3):
                    return signal, delta
            return None, None

        signal_index = {}
        for row in signal_rows or []:
            group = side_group(row_text(row, "side"))
            symbol = row_text(row, "symbol")
            signal_date = parse_date(row_text(row, "signal_date"))
            if not group or not symbol or not signal_date:
                continue
            signal_index.setdefault((symbol, group), []).append(row)
        for bucket in signal_index.values():
            bucket.sort(key=lambda item: (row_text(item, "signal_date"), row_text(item, "updated_at")), reverse=True)

        real_trades = []
        seen = set()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT *
                FROM trades
                WHERE status != 'paper'
                  AND (status IN ('filled', 'partial', 'closed') OR COALESCE(filled_shares, 0) > 0)
                ORDER BY COALESCE(filled_at, exit_at, buy_at, created_at) DESC, id DESC
                LIMIT ?
            """, (max(1, min(int(limit or 300), 1000)),)).fetchall()
        for row in rows:
            symbol = str(row["symbol"] or "").strip()
            if not symbol:
                continue
            if str(row["side"] or "").upper() == "BUY":
                trade_date = str(row["filled_at"] or row["buy_at"] or "")[:10]
                key = (symbol, "BUY", trade_date, row["price"], row["shares"], row["id"])
                real_trades.append({
                    "id": int(row["id"]),
                    "symbol": symbol,
                    "side": "BUY",
                    "tradeDate": trade_date,
                    "price": self.safe_float(row["price"]),
                    "shares": int(row["filled_shares"] or row["shares"] or 0),
                    "status": row["status"] or "",
                    "source": "trades.buy_at",
                })
                seen.add(key)
                if row["exit_at"] and row["exit_price"]:
                    exit_date = str(row["exit_at"] or "")[:10]
                    exit_key = (symbol, "SELL", exit_date, row["exit_price"], row["shares"])
                    if exit_key not in seen:
                        real_trades.append({
                            "id": int(row["id"]),
                            "symbol": symbol,
                            "side": "SELL",
                            "tradeDate": exit_date,
                            "price": self.safe_float(row["exit_price"]),
                            "shares": int(row["filled_shares"] or row["shares"] or 0),
                            "status": row["status"] or "",
                            "source": "trades.exit_at",
                        })
                        seen.add(exit_key)
            elif str(row["side"] or "").upper() == "SELL":
                trade_date = str(row["filled_at"] or "")[:10]
                key = (symbol, "SELL", trade_date, row["price"], row["shares"])
                if key in seen:
                    continue
                real_trades.append({
                    "id": int(row["id"]),
                    "symbol": symbol,
                    "side": "SELL",
                    "tradeDate": trade_date,
                    "price": self.safe_float(row["exit_price"] or row["price"]),
                    "shares": int(row["filled_shares"] or row["shares"] or 0),
                    "status": row["status"] or "",
                    "source": "trades.sell",
                })
                seen.add(key)

        aligned = []
        for trade in real_trades:
            trade_date = parse_date(trade.get("tradeDate"))
            match, lag_days = find_signal(trade["symbol"], trade["side"], trade_date)
            aligned.append({
                **trade,
                "modelMatched": match is not None,
                "modelSignalDate": row_text(match, "signal_date") if match else "",
                "modelStrategy": row_text(match, "strategy") if match else "",
                "modelSide": row_text(match, "side") if match else "",
                "modelDecision": row_text(match, "decision") if match else "",
                "modelSignalPrice": self.safe_float(match["price"]) if match else None,
                "lagDays": lag_days,
            })

        buy_rows = [row for row in aligned if row["side"] == "BUY"]
        sell_rows = [row for row in aligned if row["side"] == "SELL"]
        buy_aligned = sum(1 for row in buy_rows if row["modelMatched"])
        sell_aligned = sum(1 for row in sell_rows if row["modelMatched"])

        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            closed_rows = conn.execute("""
                SELECT *
                FROM trades
                WHERE side = 'BUY'
                  AND status != 'paper'
                  AND exit_at IS NOT NULL
                  AND exit_price IS NOT NULL
                ORDER BY exit_at DESC, id DESC
                LIMIT ?
            """, (max(1, min(int(limit or 300), 1000)),)).fetchall()
        round_trips = []
        for row in closed_rows:
            symbol = str(row["symbol"] or "").strip()
            buy_date = parse_date(row["filled_at"] or row["buy_at"])
            sell_date = parse_date(row["exit_at"])
            buy_match, buy_lag = find_signal(symbol, "BUY", buy_date)
            sell_match, sell_lag = find_signal(symbol, "SELL", sell_date)
            buy_price = self.safe_float(row["price"]) or 0
            sell_price = self.safe_float(row["exit_price"]) or 0
            shares = int(row["filled_shares"] or row["shares"] or 0)
            pnl = self.safe_float(row["pnl"])
            if pnl is None and buy_price > 0 and sell_price > 0 and shares > 0:
                pnl = (sell_price - buy_price) * shares
            return_pct = (sell_price / buy_price - 1) if buy_price > 0 and sell_price > 0 else None
            round_trips.append({
                "id": int(row["id"]),
                "symbol": symbol,
                "buyDate": str(row["filled_at"] or row["buy_at"] or "")[:10],
                "sellDate": str(row["exit_at"] or "")[:10],
                "buyPrice": buy_price or None,
                "sellPrice": sell_price or None,
                "shares": shares,
                "pnl": pnl,
                "returnPct": return_pct,
                "buyModelMatched": buy_match is not None,
                "buyModelSignalDate": row_text(buy_match, "signal_date") if buy_match else "",
                "buyModelStrategy": row_text(buy_match, "strategy") if buy_match else "",
                "buyLagDays": buy_lag,
                "sellModelMatched": sell_match is not None,
                "sellModelSignalDate": row_text(sell_match, "signal_date") if sell_match else "",
                "sellModelStrategy": row_text(sell_match, "strategy") if sell_match else "",
                "sellLagDays": sell_lag,
                "bothModelMatched": buy_match is not None and sell_match is not None,
            })
        pnl_values = [row["pnl"] for row in round_trips if row["pnl"] is not None]
        return_values = [row["returnPct"] for row in round_trips if row["returnPct"] is not None]
        both_rows = [row for row in round_trips if row["bothModelMatched"]]
        both_pnl_values = [row["pnl"] for row in both_rows if row["pnl"] is not None]
        both_return_values = [row["returnPct"] for row in both_rows if row["returnPct"] is not None]
        return {
            "windowDays": int(window_days or 3),
            "realTrades": len(aligned),
            "buyTrades": len(buy_rows),
            "buyAligned": buy_aligned,
            "buyMissed": len(buy_rows) - buy_aligned,
            "buyAlignmentRate": buy_aligned / len(buy_rows) if buy_rows else None,
            "sellTrades": len(sell_rows),
            "sellAligned": sell_aligned,
            "sellMissed": len(sell_rows) - sell_aligned,
            "sellAlignmentRate": sell_aligned / len(sell_rows) if sell_rows else None,
            "rows": aligned[:120],
            "missed": [row for row in aligned if not row["modelMatched"]][:80],
            "roundTrips": len(round_trips),
            "roundTripWins": sum(1 for row in round_trips if (row["pnl"] or 0) > 0),
            "roundTripLosses": sum(1 for row in round_trips if (row["pnl"] or 0) < 0),
            "roundTripWinRate": (
                sum(1 for row in round_trips if (row["pnl"] or 0) > 0) / len(round_trips)
                if round_trips else None
            ),
            "roundTripTotalPnl": sum(pnl_values) if pnl_values else None,
            "roundTripAvgReturn": sum(return_values) / len(return_values) if return_values else None,
            "bothAlignedRoundTrips": len(both_rows),
            "bothAlignedTotalPnl": sum(both_pnl_values) if both_pnl_values else None,
            "bothAlignedAvgReturn": sum(both_return_values) / len(both_return_values) if both_return_values else None,
            "roundTripRows": round_trips[:120],
        }

    def strategy_signal_performance(self, strategy=None, refresh_outcomes=True, strategy_prefix=None):
        if refresh_outcomes:
            self.update_strategy_signal_outcomes()
        reserved = tuple(sorted(RESERVED_PRODUCTION_STRATEGIES))
        reserved_placeholders = ",".join("?" for _ in reserved)
        conditions = [f"LOWER(TRIM(strategy)) NOT IN ({reserved_placeholders})"]
        params = list(reserved)
        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)
        elif strategy_prefix:
            conditions.append("strategy LIKE ?")
            params.append(f"{str(strategy_prefix)}%")
        where = "WHERE " + " AND ".join(conditions)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            totals = conn.execute(f"""
                SELECT strategy,
                       COUNT(*) AS signals,
                       SUM(CASE WHEN return_5d IS NULL THEN 1 ELSE 0 END) AS pending_5d
                FROM strategy_signals
                {where}
                GROUP BY strategy
            """, params).fetchall()
            rows = conn.execute(f"""
                SELECT *
                FROM strategy_signals
                {where}
            """, params).fetchall()
        groups = {}
        for row in totals:
            groups[row["strategy"]] = {
                "strategy": row["strategy"],
                "signals": int(row["signals"] or 0),
                "pending5d": int(row["pending_5d"] or 0),
                "samples": 0,
                "hits": 0,
                "returns": [],
                "rows": [],
            }
        for row in rows:
            key = row["strategy"]
            bucket = groups.setdefault(key, {
                "strategy": key,
                "signals": 0,
                "pending5d": 0,
                "samples": 0,
                "hits": 0,
                "returns": [],
                "rows": [],
            })
            bucket["rows"].append(row)
            if row["return_5d"] is not None and self.is_actionable_strategy_side(row["side"]):
                adjusted_return = self.strategy_return_for_side(row["side"], row["return_5d"])
                bucket["samples"] += 1
                bucket["hits"] += 1 if row["hit_5d"] else 0
                bucket["returns"].append(adjusted_return)
        summary = []
        for bucket in groups.values():
            returns = bucket.pop("returns")
            bucket_rows = bucket.pop("rows")
            wins = [value for value in returns if value > 0]
            losses = [value for value in returns if value < 0]
            gain = sum(wins)
            loss = abs(sum(losses))
            bucket["precision5d"] = bucket["hits"] / bucket["samples"] if bucket["samples"] else None
            bucket["averageReturn5d"] = sum(returns) / len(returns) if returns else None
            bucket["profitFactor5d"] = gain / loss if loss else None
            bucket["horizons"] = {
                f"{days}d": self.strategy_horizon_metrics(bucket_rows, days)
                for days in (1, 3, 5, 10, 20, 60)
            }
            summary.append(bucket)
        overall_rows = list(rows)
        actionable_rows = [row for row in overall_rows if self.is_actionable_strategy_side(row["side"])]
        recent_rows = sorted(
            actionable_rows,
            key=lambda row: (str(row["signal_date"] or ""), str(row["updated_at"] or "")),
            reverse=True,
        )[:80]
        overall = {
            "signals": len(overall_rows),
            "actionableSignals": len(actionable_rows),
            "strategies": len(summary),
            "horizons": {
                f"{days}d": self.strategy_horizon_metrics(overall_rows, days)
                for days in (1, 3, 5, 10, 20, 60)
            },
            "horizonGroups": {
                "short": {
                    "label": "短期",
                    "days": [1, 3, 5],
                    "primary": "5d",
                    "metrics": self.strategy_horizon_metrics(overall_rows, 5),
                },
                "mid": {
                    "label": "中期",
                    "days": [10, 20],
                    "primary": "20d",
                    "metrics": self.strategy_horizon_metrics(overall_rows, 20),
                },
                "long": {
                    "label": "長期",
                    "days": [60],
                    "primary": "60d",
                    "metrics": self.strategy_horizon_metrics(overall_rows, 60),
                },
            },
        }
        session_groups = self.strategy_signal_session_groups(overall_rows)
        overall["sessionGroups"] = session_groups
        paper_trades = self.strategy_signal_paper_trades(overall_rows)
        real_trade_alignment = self.strategy_signal_real_trade_alignment(overall_rows)
        paper_trades["realTradeAlignment"] = real_trade_alignment
        return {
            "ok": True,
            "signalCount": sum(item.get("signals", 0) for item in summary),
            "outcomeCount": sum(item["samples"] for item in summary),
            "overall": overall,
            "sessionGroups": session_groups,
            "strategies": summary,
            "recentSignals": [self.strategy_signal_detail(row) for row in recent_rows],
            "paperTrades": paper_trades,
            "realTradeAlignment": real_trade_alignment,
        }

    def strategy_calibration_advice(self, item, min_samples=20):
        strategy = str(item.get("strategy") or "")
        samples = int(item.get("samples") or 0)
        pending = int(item.get("pending5d") or 0)
        precision = item.get("precision5d")
        avg_return = item.get("averageReturn5d")
        profit_factor = item.get("profitFactor5d")
        precision_bad = precision is not None and precision < 0.45
        return_bad = avg_return is not None and avg_return < 0
        precision_good = precision is not None and precision >= 0.55
        return_good = avg_return is not None and avg_return > 0
        if samples < int(min_samples or 20):
            return {
                "strategy": strategy,
                "sampleCount": samples,
                "pending5d": pending,
                "precision5d": precision,
                "averageReturn5d": avg_return,
                "profitFactor5d": profit_factor,
                "suggestedAction": "observe_more",
                "weightMultiplier": 1.0,
                "thresholdDelta": 0.0,
                "reason": f"5日已驗證樣本 {samples} 筆，低於 {int(min_samples or 20)} 筆，先只觀察不調整。",
            }
        if precision_bad and return_bad:
            return {
                "strategy": strategy,
                "sampleCount": samples,
                "pending5d": pending,
                "precision5d": precision,
                "averageReturn5d": avg_return,
                "profitFactor5d": profit_factor,
                "suggestedAction": "lower_weight_and_raise_threshold",
                "weightMultiplier": 0.85,
                "thresholdDelta": 0.03,
                "reason": "5日勝率低於45%且平均方向報酬為負，建議觀察性降低權重並提高進場門檻。",
            }
        if precision_bad:
            return {
                "strategy": strategy,
                "sampleCount": samples,
                "pending5d": pending,
                "precision5d": precision,
                "averageReturn5d": avg_return,
                "profitFactor5d": profit_factor,
                "suggestedAction": "raise_threshold",
                "weightMultiplier": 1.0,
                "thresholdDelta": 0.02,
                "reason": "5日勝率低於45%，建議觀察性提高進場門檻。",
            }
        if return_bad:
            return {
                "strategy": strategy,
                "sampleCount": samples,
                "pending5d": pending,
                "precision5d": precision,
                "averageReturn5d": avg_return,
                "profitFactor5d": profit_factor,
                "suggestedAction": "lower_weight",
                "weightMultiplier": 0.9,
                "thresholdDelta": 0.0,
                "reason": "5日平均方向報酬為負，建議觀察性降低權重。",
            }
        if precision_good and return_good:
            return {
                "strategy": strategy,
                "sampleCount": samples,
                "pending5d": pending,
                "precision5d": precision,
                "averageReturn5d": avg_return,
                "profitFactor5d": profit_factor,
                "suggestedAction": "keep_or_consider_lift",
                "weightMultiplier": 1.0,
                "thresholdDelta": 0.0,
                "reason": "5日勝率與平均方向報酬皆為正向，觀察模式建議維持；累積5-10天穩定後可再評估是否加權。",
            }
        return {
            "strategy": strategy,
            "sampleCount": samples,
            "pending5d": pending,
            "precision5d": precision,
            "averageReturn5d": avg_return,
            "profitFactor5d": profit_factor,
            "suggestedAction": "keep_observing",
            "weightMultiplier": 1.0,
            "thresholdDelta": 0.0,
            "reason": "5日績效沒有觸發調整條件，維持觀察。",
        }

    def save_strategy_calibration_suggestions(
        self, calibration_date=None, min_samples=20, schedule_job_id=None
    ):
        calibration_date = str(calibration_date or dt.datetime.now().strftime("%Y-%m-%d"))[:10]
        performance = self.strategy_signal_performance(refresh_outcomes=True)
        strategy_items = performance.get("strategies") or []
        suggestions = [self.strategy_calibration_advice(item, min_samples=min_samples) for item in strategy_items]
        risky_actions = {"lower_weight_and_raise_threshold", "raise_threshold", "lower_weight"}
        risky_count = sum(1 for item in suggestions if item.get("suggestedAction") in risky_actions)
        saved = 0
        now_value = now_text()
        schedule_message = (
            f"策略校準觀察：已寫入 {len(suggestions)} 筆建議"
            f"，需觀察調整 {risky_count} 筆；未套用正式判斷"
        )
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            for suggestion in suggestions:
                strategy = suggestion["strategy"]
                prior_rows = conn.execute("""
                    SELECT suggested_action
                    FROM strategy_calibration
                    WHERE strategy = ?
                      AND calibration_date < ?
                    ORDER BY calibration_date DESC
                    LIMIT 9
                """, (strategy, calibration_date)).fetchall()
                recent_actions = [suggestion["suggestedAction"], *[str(row["suggested_action"] or "") for row in prior_rows]]
                observation_days = len(recent_actions)
                stable_bad_actions = {
                    "lower_weight_and_raise_threshold",
                    "raise_threshold",
                    "lower_weight",
                }
                apply_ready = (
                    observation_days >= 5
                    and suggestion["suggestedAction"] in stable_bad_actions
                    and all(action == suggestion["suggestedAction"] for action in recent_actions[:5])
                )
                metrics = {
                    "performanceStrategy": next((item for item in strategy_items if item.get("strategy") == strategy), {}),
                    "overallSignalCount": performance.get("signalCount"),
                    "overallOutcomeCount": performance.get("outcomeCount"),
                    "horizonGroups": (performance.get("overall") or {}).get("horizonGroups") or {},
                    "sessionGroups": performance.get("sessionGroups") or [],
                }
                conn.execute("""
                    INSERT INTO strategy_calibration (
                        calibration_date, strategy, mode, sample_count, precision_5d,
                        average_return_5d, profit_factor_5d, pending_5d, suggested_action,
                        weight_multiplier, threshold_delta, reason, observation_days,
                        apply_ready, applied, metrics_json, created_at, updated_at
                    ) VALUES (?, ?, 'observation', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(calibration_date, strategy) DO UPDATE SET
                        mode = excluded.mode,
                        sample_count = excluded.sample_count,
                        precision_5d = excluded.precision_5d,
                        average_return_5d = excluded.average_return_5d,
                        profit_factor_5d = excluded.profit_factor_5d,
                        pending_5d = excluded.pending_5d,
                        suggested_action = excluded.suggested_action,
                        weight_multiplier = excluded.weight_multiplier,
                        threshold_delta = excluded.threshold_delta,
                        reason = excluded.reason,
                        observation_days = excluded.observation_days,
                        apply_ready = excluded.apply_ready,
                        metrics_json = excluded.metrics_json,
                        updated_at = excluded.updated_at
                """, (
                    calibration_date,
                    strategy,
                    int(suggestion["sampleCount"] or 0),
                    suggestion["precision5d"],
                    suggestion["averageReturn5d"],
                    suggestion["profitFactor5d"],
                    int(suggestion["pending5d"] or 0),
                    suggestion["suggestedAction"],
                    suggestion["weightMultiplier"],
                    suggestion["thresholdDelta"],
                    suggestion["reason"],
                    observation_days,
                    1 if apply_ready else 0,
                    json.dumps(metrics, ensure_ascii=False),
                    now_value,
                    now_value,
                ))
                saved += 1
            self.set_meta(conn, "last_strategy_calibration_at", now_value)
            self.set_meta(conn, "last_strategy_calibration_date", calibration_date)
            self.set_meta(conn, "last_strategy_calibration_saved", str(saved))
            self.set_meta(conn, "last_strategy_calibration_adjustment_candidates", str(risky_count))
            if schedule_job_id:
                job_id = str(schedule_job_id)
                self.set_meta(conn, f"auto_schedule_{job_id}_date", calibration_date)
                self.set_meta(conn, f"auto_schedule_{job_id}_status", "success")
                self.set_meta(conn, f"auto_schedule_{job_id}_message", schedule_message)
                self.set_meta(conn, f"auto_schedule_{job_id}_at", now_value)
                self.set_meta(conn, f"auto_schedule_{job_id}_attempt_date", calibration_date)
                self.set_meta(conn, f"auto_schedule_{job_id}_attempt_count", "0")
        return {
            "ok": True,
            "date": calibration_date,
            "mode": "observation",
            "saved": saved,
            "riskyCount": risky_count,
            "scheduleMessage": schedule_message,
            "suggestions": suggestions,
        }

    def list_strategy_calibration(self, limit=80):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            try:
                reserved = tuple(sorted(RESERVED_PRODUCTION_STRATEGIES))
                placeholders = ",".join("?" for _ in reserved)
                rows = conn.execute("""
                    SELECT *
                    FROM strategy_calibration
                    WHERE LOWER(TRIM(strategy)) NOT IN ({placeholders})
                    ORDER BY calibration_date DESC, strategy ASC
                    LIMIT ?
                """.format(placeholders=placeholders), (*reserved, int(limit or 80))).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def reconcile_strategy_calibration_schedule_state(
        self, today=None, job_id="1705_strategy_calibration"
    ):
        today = str(today or dt.date.today().isoformat())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute("""
                SELECT calibration_date, COUNT(1) AS row_count,
                       MAX(updated_at) AS latest_at
                FROM strategy_calibration
                WHERE LOWER(TRIM(strategy)) NOT IN ('test', '__test__')
                GROUP BY calibration_date
                ORDER BY calibration_date DESC
                LIMIT 1
            """).fetchone()
            if not latest:
                return {"ok": True, "recovered": False, "date": None, "rows": 0}
            latest_date = str(latest["calibration_date"] or "")[:10]
            row_count = int(latest["row_count"] or 0)
            latest_at = str(latest["latest_at"] or now_text())
            self.set_meta(conn, "last_strategy_calibration_at", latest_at)
            self.set_meta(conn, "last_strategy_calibration_date", latest_date)
            self.set_meta(conn, "last_strategy_calibration_saved", str(row_count))
            schedule_row = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?",
                (f"auto_schedule_{job_id}_date",),
            ).fetchone()
            recovered = latest_date == today and (not schedule_row or str(schedule_row[0]) != today)
            if recovered:
                message = f"策略校準狀態依實際產物修復：{latest_date} 已有 {row_count} 筆"
                self.set_meta(conn, f"auto_schedule_{job_id}_date", today)
                self.set_meta(conn, f"auto_schedule_{job_id}_status", "success_recovered")
                self.set_meta(conn, f"auto_schedule_{job_id}_message", message)
                self.set_meta(conn, f"auto_schedule_{job_id}_at", latest_at)
                self.set_meta(conn, f"auto_schedule_{job_id}_attempt_date", today)
                self.set_meta(conn, f"auto_schedule_{job_id}_attempt_count", "0")
        return {
            "ok": True,
            "recovered": recovered,
            "date": latest_date,
            "rows": row_count,
        }

    def new_stock_radar_item(self, symbol, rows, reference_date="", stock_info=None):
        rows = self.rows_with_verified_sources(rows or [])
        if not (30 <= len(rows) < 120):
            return None
        latest = rows[-1]
        if reference_date and str(latest.get("date") or "")[:10] < str(reference_date)[:10]:
            return None
        index = len(rows) - 1
        previous = rows[-2] if len(rows) > 1 else latest
        close = self.safe_float(latest.get("close")) or 0
        previous_close = self.safe_float(previous.get("close")) or close
        if close <= 0:
            return None
        closes = [self.safe_float(row.get("close")) for row in rows]
        closes = [value for value in closes if value is not None and value > 0]
        volumes = [self.safe_float(row.get("volume")) for row in rows[-21:-1]]
        volumes = [value for value in volumes if value is not None and value >= 0]
        latest_volume = self.safe_float(latest.get("volume")) or 0
        avg_volume = sum(volumes) / len(volumes) if volumes else 0
        avg_volume_lots = avg_volume / 1000 if avg_volume else 0
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 0
        turnover_million = latest_volume * close / 1_000_000
        ret5_base = self.safe_float(rows[max(0, index - 5)].get("close")) or close
        ret20_base = self.safe_float(rows[max(0, index - 20)].get("close")) or close
        ret5 = (close / ret5_base - 1) * 100 if ret5_base > 0 else 0
        ret20 = (close / ret20_base - 1) * 100 if ret20_base > 0 else 0
        change1 = (close / previous_close - 1) * 100 if previous_close > 0 else 0
        recent_rows = rows[max(0, index - 20):index]
        recent_high = max((self.safe_float(row.get("high")) or 0 for row in recent_rows), default=close)
        breakout = bool(recent_high > 0 and close >= recent_high * 0.995)
        ma5 = sum(closes[-5:]) / min(5, len(closes))
        ma20 = sum(closes[-20:]) / min(20, len(closes))
        trend_ok = bool(close >= ma20 and ma5 >= ma20)
        liquidity_ok = bool(
            avg_volume_lots >= MIN_MONSTER_AVG_VOLUME_LOTS
            and turnover_million >= MIN_MONSTER_TURNOVER_MILLION
        )
        risk_flags = self._monster_risk_flags(rows, index, volume_ratio)
        danger_risk = any(flag.get("severity") == "danger" for flag in risk_flags)
        overheated = bool(ret5 > 20 or change1 > 9 or volume_ratio > 6)
        score = (
            min(volume_ratio / 3, 1) * 30
            + (25 if breakout else 0)
            + (15 if trend_ok else 0)
            + (15 if 0 <= ret5 <= 15 else 5 if ret5 > 15 else 0)
            + (15 if liquidity_ok else 0)
        )
        strict_eligible = bool(
            liquidity_ok and volume_ratio >= 1.2 and breakout and trend_ok
            and not danger_risk and not overheated
        )
        info = (stock_info or {}).get(symbol) or {}
        reasons = [
            f"上市歷史 {len(rows)} 個交易日",
            f"量能 {volume_ratio:.1f} 倍",
            "接近 20 日新高" if breakout else "尚未突破 20 日高點",
            "短均線維持多頭" if trend_ok else "短均線尚未轉強",
            f"5日 {ret5:+.1f}% / 20日 {ret20:+.1f}%",
            f"20日均量 {avg_volume_lots:.0f} 張 / 成交金額 {turnover_million:.0f} 百萬",
        ]
        return {
            "symbol": symbol,
            "name": info.get("name") or info.get("stockName") or "",
            "sector": info.get("sector") or "上市櫃新股",
            "listingDate": str(rows[0].get("date") or "")[:10],
            "priceDate": str(latest.get("date") or "")[:10],
            "historyDays": len(rows),
            "close": close,
            "score": round(max(0, min(100, score)), 2),
            "change1": change1,
            "change5": ret5,
            "change20": ret20,
            "volumeRatio": volume_ratio,
            "avgVolume20Lots": avg_volume_lots,
            "turnoverMillion": turnover_million,
            "breakout": breakout,
            "trendOk": trend_ok,
            "liquidityOk": liquidity_ok,
            "overheated": overheated,
            "riskFlags": risk_flags,
            "strictEligible": strict_eligible,
            "watchOnly": True,
            "buyAllowed": False,
            "status": (
                "新股強勢候選，獨立觀察" if strict_eligible
                else "高風險型態，只觀察" if danger_risk or overheated
                else "新股資料累積中"
            ),
            "reasons": reasons,
        }

    def new_stock_radar(self, limit=20):
        limit = max(1, min(int(limit or 20), 80))
        with self.connect() as conn:
            reference_row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
            reference_date = str(reference_row[0] or "")[:10] if reference_row else ""
            symbol_rows = conn.execute("""
                SELECT symbol, COUNT(*) AS row_count, MIN(date) AS listing_date, MAX(date) AS latest_date
                FROM prices
                GROUP BY symbol
                HAVING COUNT(*) >= 30 AND COUNT(*) < 120
                ORDER BY latest_date DESC, row_count DESC
                LIMIT 500
            """).fetchall()
        stock_info = self.load_stock_info()
        candidates = []
        errors = []
        for row in symbol_rows:
            symbol = str(row[0] or "")
            if not symbol or symbol.startswith("00"):
                continue
            try:
                item = self.new_stock_radar_item(
                    symbol, self.load_price_rows(symbol), reference_date=reference_date,
                    stock_info=stock_info,
                )
                if item:
                    candidates.append(item)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": type(exc).__name__})
        candidates.sort(key=lambda item: (
            1 if item.get("strictEligible") else 0,
            float(item.get("score") or 0),
            float(item.get("turnoverMillion") or 0),
        ), reverse=True)
        return {
            "ok": True,
            "mode": "observe_only",
            "referenceDate": reference_date,
            "count": min(len(candidates), limit),
            "candidates": candidates[:limit],
            "errors": errors[:20],
            "policy": "30-119 個交易日；獨立觀察，不繞過主雷達 120 日與盤中守門",
        }

    def _monster_risk_flags(self, rows, index, volume_ratio):
        """盤後日K高風險型態旗標：標出使用者明確要避開的倒貨/轉弱/過熱型態。
        danger 旗標會降級可買判斷，warn 旗標只提醒；缺資料的旗標自動不觸發。"""
        flags = []
        latest = rows[index]
        prev = rows[index - 1] if index > 0 else latest
        o = self.safe_float(latest.get("open")); h = self.safe_float(latest.get("high"))
        lo = self.safe_float(latest.get("low")); c = self.safe_float(latest.get("close"))
        pc = self.safe_float(prev.get("close")); pl = self.safe_float(prev.get("low"))
        vr = self.safe_float(volume_ratio) or 0.0
        rng = (h - lo) if (h is not None and lo is not None and h > lo) else 0.0
        # 1) 長上影線爆大量 = 主力可能倒貨
        if rng > 0 and o is not None and c is not None:
            upper = (h - max(o, c)) / rng
            if upper >= 0.40 and vr >= 2.0:
                flags.append({"code": "long_upper_volume", "label": "長上影爆量·疑倒貨", "severity": "danger"})
        # 2) 開高走低破昨低 = 轉弱
        if None not in (o, c, lo, pc, pl) and o >= pc and c < o and lo < pl:
            flags.append({"code": "open_high_close_low", "label": "開高走低破昨低·轉弱", "severity": "danger"})
        # 3) 連續漲停後爆量打開 = 高風險(今日前2日≥2根近漲停 + 今日爆量收在相對低)
        limitups = 0
        for j in range(max(1, index - 2), index):
            cj = self.safe_float(rows[j].get("close")); cj0 = self.safe_float(rows[j - 1].get("close"))
            if cj is not None and cj0 and cj0 > 0 and (cj - cj0) / cj0 * 100 >= 9.0:
                limitups += 1
        if limitups >= 2 and vr >= 2.0 and rng > 0 and c is not None and (h - c) / rng >= 0.30:
            flags.append({"code": "limitup_exhaust", "label": "連漲停後爆量打開·高風險", "severity": "danger"})
        # 4) 融資5日爆增 = 過熱
        base = index - 5
        mb = self.safe_float(latest.get("margin_balance"))
        mb0 = self.safe_float(rows[base].get("margin_balance")) if base >= 0 else None
        if mb is not None and mb0 is not None and mb0 > 0 and (mb - mb0) / mb0 >= 0.30:
            flags.append({"code": "margin_surge", "label": "融資5日爆增30%+·過熱", "severity": "warn"})
        # 5) 短線已漲多 = 追高風險(純顯示旗標,不改可不可買/排名/門檻)。2026-07-09 實測:近期
        #    buy_allowed 推薦追高組(5日已漲>=5%)後續 -4.13% vs 沒追高 -0.58%,漲多回檔明顯;
        #    過熱閘門是 5日>22% 才硬排除,中間「已漲 15~22%」這段合法通過閘門但回檔風險高——
        #    這裡標出來讓使用者一眼看到「這檔已經漲多了、是追高」,自己決定要不要進。要真正
        #    「不推追高」得收緊 buy_allowed 門檻=策略變動,必須先回測證明變好才改,不在此做。
        base5 = index - 5
        c5 = self.safe_float(rows[base5].get("close")) if base5 >= 0 else None
        if c is not None and c5 is not None and c5 > 0:
            ret5 = (c - c5) / c5 * 100
            if ret5 >= 15:
                flags.append({"code": "extended_runup", "label": f"5日已漲{ret5:.0f}%·追高風險", "severity": "warn"})
        return flags

    def monster_score_for_symbol(
        self, symbol, prediction=None, repair=True, sector_momentum=None,
        stock_info=None, use_model=False, market_regime=None,
    ):
        rows, quality = (
            self.ensure_model_ready_rows(symbol, repair=repair)
            if use_model else self.ensure_rule_analysis_rows(symbol, repair=repair, min_price_rows=120)
        )
        if not quality["ok"]:
            detail = (
                f"chipCoverage={quality.get('chipCoverage', 0):.2f}, "
                f"financeCoverage={quality.get('financeCoverage', 0):.2f}, "
                if use_model else ""
            )
            raise RuntimeError(
                f"{symbol} {'model' if use_model else 'rule analysis'} data incomplete: "
                f"{', '.join(quality['missing'])}; rows={quality['rows']}, "
                f"{detail}source={quality.get('priceSource')}"
            )
        features = self.build_features_for_rows(rows)
        if not features:
            # build_features_for_rows 對不滿120個交易日的資料(通常是剛上市
            # 的新股)直接回傳空清單。訊息裡帶上實際筆數跟明確原因，
            # 使用者/未來排查者才看得出這是「新股歷史不足」而非未知錯誤。
            raise RuntimeError(f"{symbol} insufficient_history: only {len(rows)} rows (need >= 120)")
        latest_feature = features[-1]
        index = latest_feature["index"]
        latest = rows[index]
        previous = rows[index - 1] if index > 0 else latest
        # 妖股掃描固定傳 use_model=False，不呼叫 predict_symbol。模型由獨立的
        # 批量預測、紙上交易與績效校準流程運行，不進妖股候選或買賣判斷。
        if prediction is None and use_model:
            prediction = self.predict_symbol(symbol, save=True)
        if prediction:
            probability = float(prediction["probability"])
            threshold = float(prediction["threshold"])
            model_probabilities = prediction.get("modelProbabilities") or {}
            model_version = prediction.get("modelVersion")
            market_gate = prediction.get("marketGate") or self.market_gate(latest_feature.get("market", {}))
        else:
            probability = None
            threshold = None
            model_probabilities = {}
            model_version = None
            market_gate = self.market_gate(latest_feature.get("market", {}))
        # 用 is None 而非 or：learning_to_rank/isolation_forest 目前的下限都鎖在
        # 0.01 附近算不出真正的 0.0，但用 or 判斷缺值是防禦性不足的寫法，未來
        # 換模型或調下限時，「模型真的判定極度不看好」可能被誤讀成「缺資料」。
        rank_probability_raw = model_probabilities.get("learning_to_rank")
        rank_probability = (
            float(rank_probability_raw) if rank_probability_raw is not None
            else (float(probability) if probability is not None else None)
        )
        anomaly_probability_raw = model_probabilities.get("isolation_forest")
        anomaly_probability = float(anomaly_probability_raw) if anomaly_probability_raw is not None else None
        ret5 = latest_feature["x"][4] * 100
        ret20 = latest_feature["x"][5] * 100
        volume_ratio = latest_feature["x"][7]
        latest_volume = self.safe_float(latest.get("volume"))
        volume_window = rows[max(0, index - 19):index + 1]
        volume_values = [self.safe_float(row.get("volume")) for row in volume_window]
        volume_values = [value for value in volume_values if value is not None]
        latest_volume_lots = latest_volume / 1000 if latest_volume is not None else None
        avg_volume20_lots = (sum(volume_values) / len(volume_values)) / 1000 if volume_values else 0
        turnover_million = ((latest_volume or 0) * float(latest.get("close") or 0)) / 1_000_000 if latest_volume is not None else 0
        liquidity_ok = bool(
            latest_volume is not None and
            avg_volume20_lots >= MIN_MONSTER_AVG_VOLUME_LOTS and
            turnover_million >= MIN_MONSTER_TURNOVER_MILLION
        )
        rsi = latest_feature["x"][1] * 100
        atr_pct = latest_feature["x"][3] * 100
        stock_stronger = bool(market_gate.get("stockStrongerThanTaiex"))
        market_ok = bool(market_gate.get("allowBuy"))
        hot_market = bool(market_gate.get("hotMarket"))
        # 題材／類股輪動：純用當天候選池的官方價量資料算出的產業超額報酬，
        # 不是新聞或 AI 猜的「熱度」。沒有 sector_momentum 快照時（例如單股
        # 查詢、非全市場掃描的呼叫路徑）優雅退化成 0，不影響既有行為。
        sector = ((stock_info or {}).get(symbol) or {}).get("sector") or "台股"
        sector_stat = ((sector_momentum or {}).get("sectors") or {}).get(sector) or {}
        sector_excess_ret5 = float(sector_stat.get("excessRet5") or 0)
        sector_hot = sector in ((sector_momentum or {}).get("hotSectors") or [])
        sector_theme_heat = float(sector_stat.get("themeHeat") or 0)
        sector_theme_streak = int(sector_stat.get("streakDays") or 0)
        market_snapshot = latest_feature.get("market") or {}
        taiex_ret20 = self.safe_float(market_snapshot.get("taiex_ret_20"))
        taiex_ma_gap = self.safe_float(market_snapshot.get("taiex_ma_gap"))
        weak_market = bool(
            (not market_ok) or
            (taiex_ret20 is not None and taiex_ret20 < -0.02) or
            (taiex_ma_gap is not None and taiex_ma_gap < -0.02)
        )
        recent_high = max((row["high"] for row in rows[max(0, index - 20):index]), default=latest["high"])
        breakout = latest["close"] >= recent_high * 0.995
        change1 = ((latest["close"] - previous["close"]) / previous["close"]) * 100 if previous.get("close") else 0
        trend_ok = latest_feature["x"][0] > 0 and latest_feature["x"][2] > 0
        volume_ok = (1.15 if hot_market else 1.4) <= volume_ratio <= 5.5
        momentum_ok = ret5 >= (1.5 if hot_market else 3) and ret20 >= (2.5 if hot_market else 4)
        # 妖股判定收緊：門檻地板拉高、對全域門檻的減讓幅度縮小，正式模型
        # 這條路徑不再那麼容易放行。
        # monster_threshold 現在只作回傳/顯示的參考門檻;拆模型後不再有任何買賣閘門讀它。
        # 原本的 model_ok/rank_ok/anomaly_ok 三個模型閘門計算後從未被引用(死碼),已移除。
        # 掃描去模型時 threshold 為 None → 參考門檻也 None。
        monster_threshold = max(0.42, threshold - 0.05) if threshold is not None else None
        risk_ok = 1.2 <= atr_pct <= 8.5 and rsi <= 82
        month_high_strength = bool(breakout and ret5 >= 0 and volume_ratio >= 1.15)
        limit_up_strength = bool(change1 >= 8.5 and stock_stronger and volume_ratio >= 1.2)
        counter_trend_strength = bool(
            weak_market and
            stock_stronger and
            liquidity_ok and
            risk_ok and
            volume_ratio >= 1.15 and
            (month_high_strength or limit_up_strength or change1 >= 3.5 or ret5 >= 5) and
            ret20 >= -3 and
            rsi <= 86 and
            volume_ratio <= 6.5
        )
        overheated = bool(
            (rsi > 82 or volume_ratio > 5.5 or ret5 > 22 or change1 > 9) and
            not counter_trend_strength
        )
        surge_setup = bool(
            ret5 >= 7.5 and
            ret20 >= 10 and
            1.5 <= volume_ratio <= 5.5 and
            breakout and
            stock_stronger and
            trend_ok and
            rsi <= 82
        )
        # surge/counter/sector 型態不再灌進推薦分數(無回測依據，見下方分數
        # 註解)，僅保留為標籤/狀態/盤中判斷與 reasons 顯示用途。
        sector_persistent_hot = bool(sector_stat.get("persistentHot"))
        # 分數歷史：曾用「快篩動能分50% + 模型機率50%混合」,但 2026-07-07 妖股重定義
        # 後改為純型態量能(見下方 score 註解)——回測 radar_like(純規則)0.742% 勝過
        # blend(含 logistic proxy)0.553%。因此模型不混入型態分，也不進候選排序。
        # 舊版的手調權重(24/24/12/20/16/10)與 surge/counter/sector 加分項
        # 沒有回測依據，全部移出分數——那些型態仍保留為標籤/狀態/盤中判斷
        # 用途，只是不再灌進推薦分數。overheated 不扣分：它由 buy_allowed
        # 與盤中否決硬性把關，排序上 buy_allowed 優先已讓過熱股沉底。
        # 注意：回測用 logistic 機率當 proxy，正式環境用 ensemble 機率
        # (同目標、資訊更多)。真正決定「哪些股入選/排序」的是 quick_monster_filter
        # 的 7 項 rawScore(含 month_high+12/counter+4)——那一份才必須與
        # backtest_top10.py 的 quick_score 保持一致、改任一邊都要重跑回測。
        # 下面這個 quick_score_result 是模型階段之後、僅供顯示與同組次序用的分數，
        # 已依上方(surge/counter/sector 無回測依據)刻意精簡成 5 項、移除
        # month_high/counter 加分，本就與回測的 7 項不同，不需、也不應對齊。
        # 2026-07-07 顯示分同步改用 pattern_strict(型態，非漲幅 magnitude)，與
        # quick_monster_filter 的 rawScore 一致：量能/突破月高/比大盤強/逆勢。
        # 量能比改用「含當日」(vr_incl)算顯示/決策分,對齊 quick_monster_filter 的選股 rawScore
        # 與 backtest_top10 的 quick_score(2026-07-03 實驗:含當日 0.794% > 排除當日 0.551%)。
        # 上面 volume_ratio=x[7] 是特徵的「排除當日」語意,留給模型/量能閘門,兩者刻意分開不混用。
        _score_vols = [v for v in (self.safe_float(r.get("volume")) for r in volume_window) if v is not None]
        _avg_incl = sum(_score_vols) / len(_score_vols) if _score_vols else 0
        vr_incl = (latest_volume / _avg_incl) if (latest_volume is not None and _avg_incl > 0) else 0
        quick_score_result = radar_rule_score_components(
            vr_incl,
            month_high_strength,
            stock_stronger,
            surge_setup,
            counter_trend_strength,
        )
        # 2026-07-07 妖股重定義:分數=純型態量能,拿掉模型那一半(舊= quick_score/100*0.5
        # + prob*0.5)。回測 radar_like(純規則)OOS avgNet 0.742% > blend(規則+模型各半)
        # 0.553%。模型不進這個分數、候選排序或門檻。分數固定維持 0-100；
        # walk-forward 未通過精準度與淨損益門檻時，設定載入器會自動保留內建權重。
        score = quick_score_result["score"]
        if market_regime:
            radar_regime = dict(market_regime)
        else:
            local_market = dict(market_snapshot)
            local_market["otc_ma_gap"] = 0.0
            radar_regime = classify_radar_market_regime(local_market, sector_momentum)
        regime_key = str(radar_regime.get("key") or "theme_rotation")
        regime_label = RADAR_REGIME_LABELS.get(regime_key, RADAR_REGIME_LABELS["theme_rotation"])
        regime_threshold = radar_regime_threshold(regime_key)
        # 篩選偏向飆股：逆勢型態要同時有模型證據(排名/異常)和價格強勢
        # (月高/漲停)才能觸發覆寫，勝率門檻也比照一般標準，不再享有
        # 和 surge_setup 相同的低門檻。判定收緊：門檻整體提高一階。
        # 2026-07-07 純規則:拿掉模型(prob/rank/anomaly),覆寫只看型態量能。
        counter_override = bool(
            counter_trend_strength and
            (month_high_strength or limit_up_strength)
        )
        risk_flags = self._monster_risk_flags(rows, index, volume_ratio)
        danger_risk = any(flag.get("severity") == "danger" for flag in risk_flags)
        # score 已改純型態量能(數值較舊 blend 高),門檻 60→70 校準避免變太鬆。
        radar_override = bool(
            stock_stronger and
            score >= 70 and
            (surge_setup or counter_override or month_high_strength)
        )
        # 2026-07-07 妖股重定義:「能不能進場」也靠型態/量能,拿掉 momentum_ok
        # (原要求 ret5>=3 且 ret20>=4 的「幾天漲幾%」進場條件)。仍保留 volume_ok
        # (量能)/trend_ok(均線型態)/risk_ok/not overheated/流動性——進場改由型態量能
        # 把關。momentum_ok 變數保留計算供 reasons 顯示,不再是 setup_ok 必要條件。
        setup_ok = bool(
            (market_ok or stock_stronger or counter_trend_strength) and
            (trend_ok or counter_trend_strength) and
            volume_ok and
            risk_ok and
            liquidity_ok and
            not overheated
        )
        # Model output remains isolated. A structural trigger must also clear a
        # real score floor; previously month_high_strength bypassed the intended
        # floor and allowed 37-43 point rows into the formal watch list.
        buy_allowed = radar_daily_watch_allowed(
            score,
            setup_ok,
            danger_risk,
            radar_override,
            surge_setup,
            counter_override,
            month_high_strength,
            minimum_score=regime_threshold,
        )
        entry_guardrail = radar_entry_guardrail_decision(surge_setup, ret5)
        if entry_guardrail["vetoed"]:
            buy_allowed = False
        atr = latest["close"] * latest_feature["x"][3]
        buy_trigger = max(latest["close"], recent_high + atr * 0.1)
        pullback_price = max(latest["close"] - atr * 0.55, latest["close"] * 0.97)
        exit_levels = radar_exit_levels(buy_trigger, latest["close"], atr)
        stop_price = exit_levels["stopPrice"]
        # 2026-07-07 純規則:狀態文字拿掉 model_ok/rank/anomaly 引用,只講型態量能。
        if buy_allowed and surge_setup:
            status = "短線妖股型態，隔日開盤二次確認"
        elif buy_allowed and counter_trend_strength:
            status = "逆勢強勢，列入低接/V轉觀察"
        elif buy_allowed:
            status = "型態量能啟動，等盤中量價確認"
        elif danger_risk:
            status = "高風險型態，只觀察不追"
        elif not liquidity_ok:
            status = "流動性不足，不追"
        elif overheated:
            status = "短線過熱，不追高"
        elif entry_guardrail["vetoed"]:
            status = "不追價進場防線否決，只觀察"
        elif score < regime_threshold:
            status = f"{regime_label}門檻 {regime_threshold:.0f} 分未達，只觀察"
        elif setup_ok and score >= 55:
            status = "型態分數高，尚未突破買點"
        elif not market_ok and not stock_stronger:
            status = "大盤偏弱，需更嚴格確認"
        else:
            status = "條件不足"

        # 妖股短線規則引擎(燈號式，非加權分數)——第一階段只收集資料，
        # 完全不影響上面已經算好的 buy_allowed/score/status，另外存進
        # brain_v2_snapshots 的 rule_* 欄位(跟 Brain v2 的 soft_gate 合併成
        # 同一張實驗性訊號快照表)，等累積幾週真實命中率後才考慮拿來當
        # 降級守門員用。用延遲匯入避免跟 modules.brain 循環匯入
        # (modules/brain/engine.py 本身會 import 這個檔案裡的 backend)。
        from modules.brain.monster_rule_engine import monster_rule_engine
        # 拿5列(不是剛好3列)當緩衝：monster_rule_engine內部會先濾掉None
        # 再取最近3筆非None值判斷連續買超，只給剛好3列的話，只要中間有
        # 一天缺籌碼資料(假日/來源延遲很常見)，濾完就不足3筆，
        # chip_streak_strong永遠是False，過熱降級的例外情境形同虛設。
        chip_window = rows[max(0, index - 4):index + 1]
        rule_engine_result = monster_rule_engine({
            "volume_ratio": volume_ratio,
            "breakout": breakout,
            "counter_trend_strength": counter_trend_strength,
            "day_trade_ratio": self.safe_float(latest.get("day_trade_ratio")),
            "main_force_buy_sell_recent": [self.safe_float(row.get("main_force_buy_sell")) for row in chip_window],
            "broker_branch_net_buy_recent": [self.safe_float(row.get("broker_branch_net_buy")) for row in chip_window],
            "rsi": rsi,
            "ret5": ret5,
            "change1": change1,
            "stock_stronger": stock_stronger,
            "data_confidence": quality.get("advancedFlowSourceCoverage"),
        })
        try:
            self.save_monster_rule_snapshot(symbol, latest["date"], rule_engine_result)
        except Exception:
            pass

        reasons = [
            f"型態量能分數 {score:.1f}（量能＋突破月高＋比大盤強＋逆勢）",
            f"市場狀態「{regime_label}」正式門檻 {regime_threshold:.0f} 分"
            + ("，已通過" if score >= regime_threshold else "，未通過"),
            "符合短線妖股型態" if surge_setup else "尚未形成完整妖股型態",
            "大盤弱但個股逆勢強" if counter_trend_strength else "未形成逆勢強勢",
            "接近月高或漲停強勢" if (month_high_strength or limit_up_strength) else "未接近月高",
            "大盤允許或個股強於大盤" if (market_ok or stock_stronger) else "大盤偏弱且個股未明顯轉強",
            f"流動性 {'足夠' if liquidity_ok else '不足'}：20日均量 {avg_volume20_lots:.0f} 張，成交金額 {turnover_million:.0f} 百萬",
            f"量能 {volume_ratio:.1f} 倍",
            f"5日 {ret5:.1f}% / 20日 {ret20:.1f}%",
            "接近 20 日新高突破" if breakout else "還沒突破 20 日高點",
            "強於大盤" if stock_stronger else "未強於大盤",
            "短線過熱" if overheated else "未明顯過熱",
            f"所屬產業「{sector}」{'今日熱門族群' if sector_hot else '超額動能' if sector_excess_ret5 > 0 else '未明顯輪動'}"
            f"（題材熱度 {sector_theme_heat:.0f}，連續發酵 {sector_theme_streak} 日；產業5日超額 {sector_excess_ret5:+.1f}%）"
            if sector_momentum else f"所屬產業「{sector}」（無族群輪動資料）",
        ]
        if use_model and probability is not None:
            reasons.insert(
                1,
                f"獨立模型參考：{probability * 100:.1f}%"
                + (f" / 排名 {rank_probability * 100:.1f}%" if rank_probability is not None else "")
                + (f" / 異常 {anomaly_probability * 100:.1f}%" if anomaly_probability is not None else ""),
            )
        for guardrail_reason in entry_guardrail["reasons"]:
            reasons.append(f"進場防線：{guardrail_reason}")
        result = {
            "symbol": symbol,
            "priceDate": latest["date"],
            "score": score,
            "scoreComponents": quick_score_result["components"],
            "scoreWeights": quick_score_result["weights"],
            "action": "NEXT_DAY_WATCH" if buy_allowed else "WAIT",
            "buyAllowed": buy_allowed,
            "overheated": overheated,
            "status": status,
            "close": latest["close"],
            "change1": change1,
            "change5": ret5,
            "change20": ret20,
            "volumeRatio": volume_ratio,
            "latestVolumeLots": latest_volume_lots,
            "avgVolume20Lots": avg_volume20_lots,
            "turnoverMillion": turnover_million,
            "liquidityOk": liquidity_ok,
            "surgeSetup": surge_setup,
            "entryGuardrailVetoed": entry_guardrail["vetoed"],
            "entryGuardrailReasons": entry_guardrail["reasons"],
            "entryGuardrailRules": entry_guardrail["rules"],
            "counterTrendStrength": counter_trend_strength,
            "monthHighStrength": month_high_strength,
            "limitUpStrength": limit_up_strength,
            "weakMarket": weak_market,
            "sector": sector,
            "sectorExcessRet5": round(sector_excess_ret5, 2),
            "sectorHot": sector_hot,
            "sectorPersistentHot": sector_persistent_hot,
            "themeHeat": round(sector_theme_heat, 1),
            "sectorThemeStreak": sector_theme_streak,
            "themeSnapshot": sector_stat,
            "marketRegime": regime_key,
            "marketRegimeLabel": regime_label,
            "marketRegimeSnapshot": radar_regime,
            "regimeThreshold": regime_threshold,
            "buyTrigger": buy_trigger,
            "pullbackPrice": pullback_price,
            "stopPrice": stop_price,
            "takeProfit": exit_levels["takeProfit"],
            "trailingStop": exit_levels["trailingStop"],
            "rewardRiskRatio": exit_levels["rewardRiskRatio"],
            "targetReturn": MONSTER_TARGET_RETURN,
            "stopLossReturn": RADAR_STOP_LOSS_RETURN,
            "minimumFormalScore": regime_threshold,
            "baseMinimumFormalScore": RADAR_MIN_FORMAL_SCORE,
            "gapLimit": 0.05,
            "reasons": reasons,
            "ruleEngine": rule_engine_result,
            "riskFlags": risk_flags,
        }
        if use_model:
            result.update({
                "probability": probability,
                "threshold": monster_threshold,
                "modelVersion": model_version,
            })
        return result

    def save_monster_score(self, conn, row):
        conn.execute("""
            INSERT OR REPLACE INTO monster_scores (
                scan_date, symbol, price_date, score, probability, threshold, action, buy_allowed,
                recorded_buy_allowed, invalid_for_trading, status, close, buy_trigger, pullback_price, stop_price, take_profit, trailing_stop,
                gap_limit, change1, change5, change20, volume_ratio, latest_volume_lots,
                avg_volume20_lots, turnover_million, liquidity_ok, surge_setup, counter_trend_strength,
                sector_excess_ret5, overheated, market_regime, regime_threshold, theme_heat,
                sector_theme_streak, theme_snapshot, reasons, risk_flags, model_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today_key(), row["symbol"], row["priceDate"], row["score"], row.get("probability"), row.get("threshold"),
            row["action"], 1 if row["buyAllowed"] else 0,
            1 if row.get("recordedBuyAllowed", row.get("buyAllowed")) else 0,
            1 if row.get("invalidForTrading") else 0,
            row["status"], row["close"], row["buyTrigger"],
            row["pullbackPrice"], row["stopPrice"], row["takeProfit"], row["trailingStop"], row["gapLimit"],
            row.get("change1"), row.get("change5"), row.get("change20"), row.get("volumeRatio"),
            row.get("latestVolumeLots"), row.get("avgVolume20Lots"), row.get("turnoverMillion"),
            1 if row.get("liquidityOk") else 0, 1 if row.get("surgeSetup") else 0,
            1 if row.get("counterTrendStrength") else 0, row.get("sectorExcessRet5"),
            1 if row.get("overheated") else 0,
            row.get("marketRegime"), row.get("regimeThreshold"), row.get("themeHeat"),
            int(row.get("sectorThemeStreak") or 0),
            json.dumps(row.get("themeSnapshot") or {}, ensure_ascii=False),
            json.dumps(row["reasons"], ensure_ascii=False),
            json.dumps(row.get("riskFlags") or [], ensure_ascii=False),
            row.get("modelVersion"), now_text()
        ))
        self.save_strategy_signal(conn, {
            "signalDate": today_key(),
            "strategy": "monster",
            "side": "BUY_WATCH",
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "decision": row.get("status"),
            "score": row.get("score"),
            "modelVersion": "",
            "price": row.get("close"),
            "buyPoint": row.get("buyTrigger"),
            "stopPrice": row.get("stopPrice"),
            "targetPrice": row.get("takeProfit"),
            "dataDate": row.get("priceDate"),
            "dataSource": "官方/授權日線價量",
            "decisionSource": "妖股流動性、型態量能與風險規則",
            "evidence": {
                "buyAllowed": row.get("buyAllowed"),
                "reasons": row.get("reasons"),
                "change1": row.get("change1"),
                "change5": row.get("change5"),
                "volumeRatio": row.get("volumeRatio"),
                "turnoverMillion": row.get("turnoverMillion"),
            },
        })

    def quick_monster_filter(self, symbol):
        # 快篩不能看到「有舊資料」就直接沿用。先以全市場最後完整交易日檢查
        # 新鮮度；沒有資料、來源不可信、OHLCV 不完整或日期落後時，會在
        # ensure_rule_analysis_rows() 內強制繞過當日快取補抓一次，再回來判斷。
        rows, quality = self.ensure_rule_analysis_rows(
            symbol,
            repair=True,
            min_price_rows=120,
        )
        if not rows:
            return {
                "ok": False,
                "symbol": symbol,
                "score": 0,
                "reason": "missing_verified_source",
                "ret5": 0,
                "ret20": 0,
                "change1": 0,
                "volumeRatio": 0,
                "latestVolumeLots": None,
                "avgVolume20Lots": 0,
                "turnoverMillion": 0,
                "liquidityOk": False,
                "volumeExpanded": False,
                "fiveDayTurning": False,
                "stockStronger": False,
            }
        unresolved = set(quality.get("missing") or []) - {"priceRowsEnough"}
        if unresolved:
            return {
                "ok": False,
                "symbol": symbol,
                "score": 0,
                "reason": "data_incomplete_after_repair(" + ",".join(sorted(unresolved)) + ")",
                "ret5": 0,
                "ret20": 0,
                "change1": 0,
                "volumeRatio": 0,
                "latestVolumeLots": None,
                "avgVolume20Lots": 0,
                "turnoverMillion": 0,
                "liquidityOk": False,
                "volumeExpanded": False,
                "fiveDayTurning": False,
                "stockStronger": False,
            }
        features = self.build_features_for_rows(rows)
        if not features:
            # build_features_for_rows 對 len(rows)<120(通常是剛上市不滿120個
            # 交易日的新股)直接回傳空清單。舊版這裡直接 return None，跟其他
            # 拒絕原因(missing_verified_source等)不一致地完全消失在掃描結果
            # 裡，使用者只看到候選數變化，無法分辨是「真的不夠強」還是
            # 「新股被硬性排除」。改成跟其他拒絕原因同樣的結構回傳，方便
            # 追蹤，不改變任何篩選/排序邏輯本身。
            return {
                "ok": False,
                "symbol": symbol,
                "score": 0,
                "reason": "insufficient_history",
                "rowCount": len(rows),
                "ret5": 0,
                "ret20": 0,
                "change1": 0,
                "volumeRatio": 0,
                "latestVolumeLots": None,
                "avgVolume20Lots": 0,
                "turnoverMillion": 0,
                "liquidityOk": False,
                "volumeExpanded": False,
                "fiveDayTurning": False,
                "stockStronger": False,
            }
        latest_feature = features[-1]
        index = latest_feature["index"]
        latest = rows[index]
        # 個股資料新鮮度護欄(比照大盤的 MARKET_DATA_MAX_STALE_DAYS)：停牌/
        # 下市/斷更的股票最新一根K可能是好幾週前的，而妖股末段常伴隨暴漲
        # 爆量——不擋的話會以凍結的高分舊資料入選「今天」的掃描結果，
        # 給出根本無法成交的買點。
        if calendar_days_between(latest.get("date"), today_key()) > MARKET_DATA_MAX_STALE_DAYS:
            return {
                "ok": False,
                "symbol": symbol,
                "score": 0,
                "reason": f"stale_price_data({latest.get('date')})",
                "ret5": 0, "ret20": 0, "change1": 0, "volumeRatio": 0,
                "latestVolumeLots": None, "avgVolume20Lots": 0, "turnoverMillion": 0,
                "liquidityOk": False, "volumeExpanded": False,
                "fiveDayTurning": False, "stockStronger": False,
            }
        previous = rows[index - 1] if index > 0 else latest
        values = latest_feature["x"]
        ret5 = values[4] * 100
        ret20 = values[5] * 100
        stock_stronger = (latest_feature.get("market") or {}).get("stock_vs_taiex_20", values[18]) > 0
        latest_volume = self.safe_float(latest.get("volume"))
        volume_window = rows[max(0, index - 19):index + 1]
        volume_values = [self.safe_float(row.get("volume")) for row in volume_window]
        volume_values = [value for value in volume_values if value is not None]
        latest_volume_lots = latest_volume / 1000 if latest_volume is not None else None
        avg_volume20_lots = (sum(volume_values) / len(volume_values)) / 1000 if volume_values else 0
        # 雷達快篩的量能比用「舊語意」(分母含當日)，跟模型特徵values[7]的
        # 「新語意」(分母排除當日)刻意分開——2026-07-03 backtest_top10 決定性
        # 實驗：模型特徵用新語意 model_prob OOS 較好(1.439→1.713%)，但雷達
        # 快篩排序品質依賴舊語意(radar_vr_incl 0.794% vs 新語意 0.551%，
        # 逐月6/8個月較好；調飽和上限/閘門的5組變體全部無效)。兩個消費端
        # 各用各的定義，不是bug，改之前先重跑 backtest_top10.py 驗證。
        avg_volume_incl = sum(volume_values) / len(volume_values) if volume_values else 0
        volume_ratio = (latest_volume / avg_volume_incl) if (latest_volume is not None and avg_volume_incl > 0) else 0
        turnover_million = ((latest_volume or 0) * float(latest.get("close") or 0)) / 1_000_000 if latest_volume is not None else 0
        change1 = ((latest["close"] - previous["close"]) / previous["close"]) * 100 if previous.get("close") else 0
        recent_high = max((row["high"] for row in rows[max(0, index - 20):index]), default=latest["high"])
        month_high_strength = float(latest.get("close") or 0) >= float(recent_high or 0) * 0.995 if recent_high else False
        # counter_strength 是 five_day_turning 的替代條件，不能讓「5日淨跌+
        # 單日死貓跳」繞過防單日噪音設計：change1>=3.5 這條腿要求 ret5 至少
        # 不是負的(單日噴出只在近5日整體沒走弱時才算逆勢強勢)；接近月高與
        # ret5>=5 兩條腿本身就隱含多日強度，維持不變。
        counter_strength = bool(stock_stronger and (
            month_high_strength or ret5 >= 5 or (change1 >= 3.5 and ret5 >= 0)
        ))
        trend_ok = values[0] > 0 and values[2] > 0
        rsi = values[1] * 100
        feature_volume_ratio = values[7]
        surge_setup = bool(
            ret5 >= 7.5 and
            ret20 >= 10 and
            1.5 <= feature_volume_ratio <= 5.5 and
            month_high_strength and
            stock_stronger and
            trend_ok and
            rsi <= 82
        )
        liquidity_ok = latest_volume is not None and avg_volume20_lots >= MIN_MONSTER_AVG_VOLUME_LOTS and turnover_million >= MIN_MONSTER_TURNOVER_MILLION
        volume_expanded = volume_ratio >= 1.2
        # 2026-07-07 妖股重定義：靠型態/量能判定「是不是妖股」,拿掉「幾天漲幾%」選股條件
        # (原入選必要條件 five_day_turning: ret5>=2)。回測 backtest_top10 radar_pure：
        # OOS avgNet 0.720→0.742%、命中/PF 皆持平或微升——拿掉%-gain閘門選股品質沒崩還微好。
        # five_day_turning 保留計算供 reasons 顯示,但不再是入選必要條件。務必同步
        # backtest_top10.radar_gate（radar_like 現= 純型態量能）。
        five_day_turning = (ret5 >= 2 and ret5 >= change1) or counter_strength
        ok = bool(liquidity_ok and volume_expanded and stock_stronger)
        # 2026-07-07 pattern_strict：拿掉 ret5/ret20 漲幅 magnitude，只留妖股型態
        # (量能/突破月高/比大盤強/逆勢)。回測 backtest_top10.py OOS avgNet
        # 0.522%→0.720%、獲利因子 1.11→1.16(改良面在「少賠」不在「多命中」，命
        # 中率 33.2%→32.9% 幾乎持平)。動機＝追「已漲一大段」＝買在短期反轉點
        # (2026-07-07 IC 實測短期動能顯著負)，改看型態少賠。舊動能版(量能34/
        # ret5 30/ret20 14/強14/月高12/紅6/逆勢4)已存回測紀錄與 git。
        # ⚠️改此處務必同步 backtest_top10.quick_score 與 monster_score_for_symbol
        # 的 quick_score_result(顯示分)——三者共用 radar_rule_score_components，
        # 權重只可由通過 walk-forward 採用門檻的規則設定替換。
        quick_score_result = radar_rule_score_components(
            volume_ratio,
            month_high_strength,
            stock_stronger,
            surge_setup,
            counter_strength,
        )
        return {
            "ok": ok,
            "symbol": symbol,
            "priceDate": latest.get("date"),
            "score": quick_score_result["score"],
            "rawScore": quick_score_result["rawScore"],
            "scoreComponents": quick_score_result["components"],
            "scoreWeights": quick_score_result["weights"],
            "ret5": ret5,
            "ret20": ret20,
            "change1": change1,
            "volumeRatio": volume_ratio,
            "latestVolumeLots": latest_volume_lots,
            "avgVolume20Lots": avg_volume20_lots,
            "turnoverMillion": turnover_million,
            "liquidityOk": liquidity_ok,
            "volumeExpanded": volume_expanded,
            "fiveDayTurning": five_day_turning,
            "stockStronger": stock_stronger,
            "surgeSetup": surge_setup,
            "monthHighStrength": month_high_strength,
            "counterStrength": counter_strength,
        }

    def load_radar_sector_history(self, before_date=None, limit_dates=10):
        before_date = str(before_date or today_key())[:10]
        with self.connect() as conn:
            date_rows = conn.execute(
                """SELECT DISTINCT scan_date FROM radar_sector_history
                   WHERE scan_date < ? ORDER BY scan_date DESC LIMIT ?""",
                (before_date, max(1, int(limit_dates))),
            ).fetchall()
            dates = sorted(str(row[0]) for row in date_rows)
            if not dates:
                return []
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"SELECT scan_date, sector, snapshot FROM radar_sector_history "
                f"WHERE scan_date IN ({placeholders}) ORDER BY scan_date, sector",
                dates,
            ).fetchall()
        by_date = {date: {"date": date, "sectors": {}} for date in dates}
        for scan_date, sector, snapshot in rows:
            try:
                stat = json.loads(snapshot or "{}")
            except (TypeError, ValueError):
                stat = {}
            by_date[str(scan_date)]["sectors"][str(sector)] = stat
        return [by_date[date] for date in dates]

    def save_radar_sector_history(self, conn, scan_date, snapshot):
        scan_date = str(scan_date or today_key())[:10]
        conn.execute("DELETE FROM radar_sector_history WHERE scan_date = ?", (scan_date,))
        for sector, stat in ((snapshot or {}).get("sectors") or {}).items():
            conn.execute("""
                INSERT INTO radar_sector_history (
                    scan_date, sector, candidate_count, turnover_share,
                    avg_ret5, avg_ret20, excess_ret5, excess_ret20,
                    avg_volume_ratio, hot, streak_days, theme_heat,
                    snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                scan_date, sector, int(stat.get("count") or 0),
                float(stat.get("turnoverShare") or 0), stat.get("avgRet5"),
                stat.get("avgRet20"), stat.get("excessRet5"), stat.get("excessRet20"),
                stat.get("avgVolumeRatio"), 1 if stat.get("hot") else 0,
                int(stat.get("streakDays") or 0), float(stat.get("themeHeat") or 0),
                json.dumps(stat, ensure_ascii=False), now_text(),
            ))

    def compute_sector_momentum(
        self, quick_candidates, stock_info=None, min_sector_count=3, top_n=6,
        previous_hot_sectors=None, history=None,
    ):
        """Point-in-time sector heat shared by live scans and walk-forward records."""
        if not quick_candidates:
            return {"sectors": {}, "hotSectors": [], "overallRet5": 0.0}
        stock_info = stock_info if stock_info is not None else self.load_stock_info()
        if history is None and previous_hot_sectors is not None:
            history = [{
                "date": "legacy_previous_scan",
                "sectors": {sector: {"hot": True} for sector in previous_hot_sectors},
            }]
        elif history is None:
            history = self.load_radar_sector_history(before_date=today_key(), limit_dates=10)
        return compute_sector_theme_snapshot(
            quick_candidates,
            stock_info=stock_info,
            history=history,
            min_sector_count=min_sector_count,
            top_n=top_n,
        )

    def radar_market_regime_snapshot(self, date, sector_snapshot, market_context=None):
        context = market_context or MarketContext(self.load_market_rows())
        reference_date = str(date or "")[:10]
        if not reference_date:
            taiex_dates = context.dates_by_key.get("TAIEX") or []
            reference_date = taiex_dates[-1] if taiex_dates else today_key()
        market = self.market_features(reference_date, context, 0.0)
        market["otc_ma_gap"] = context.ma_gap("OTC", reference_date, 20)
        result = classify_radar_market_regime(market, sector_snapshot)
        result["date"] = reference_date
        result["minimumFormalScore"] = radar_regime_threshold(result["key"])
        return result

    def scan_monster_scores(self, symbols=None, limit=300, score_limit=100, progress_callback=None):
        quick_limit = min(MONSTER_MAX_UPDATE_SYMBOLS, max(1, int(limit or 300)))
        score_limit = min(MONSTER_MAX_UPDATE_SYMBOLS, max(1, int(score_limit or 100)))
        if symbols:
            symbols = [str(symbol).replace(".TWO", "").replace(".TW", "").strip() for symbol in symbols if str(symbol).strip()]
            symbols = list(dict.fromkeys(symbols))
        else:
            symbols = self.listed_symbols()
        total = len(symbols)
        if progress_callback:
            progress_callback({
                "phase": "快速條件全市場篩選",
                "total": total,
                "processed": 0,
                "saved": 0,
                "errors": 0,
                "current": "",
                "message": f"全市場先用成交金額、量能放大、5日漲幅、強於大盤排名，快篩前 {quick_limit} 檔",
            })
        self.update_market_data()

        candidates = []
        results = []
        errors = []

        for index, symbol in enumerate(symbols, start=1):
            if progress_callback:
                progress_callback({
                    "phase": "快速條件全市場篩選",
                    "total": total,
                    "processed": index - 1,
                    "saved": len(candidates),
                    "errors": len(errors),
                    "current": symbol,
                    "message": f"快速候選已建立 {len(candidates)} 檔，最後取前 {score_limit} 檔作純規則評分",
                })
            try:
                quick = self.quick_monster_filter(symbol)
                if quick and quick["ok"]:
                    candidates.append(quick)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
            if progress_callback:
                progress_callback({
                    "phase": "快速條件全市場篩選",
                    "total": total,
                    "processed": index,
                    "saved": len(candidates),
                    "errors": len(errors),
                    "current": symbol,
                    "message": f"快速候選已建立 {len(candidates)} 檔，最後取前 {score_limit} 檔作純規則評分",
                })

        stock_info = self.load_stock_info()
        # 讀取「上一次掃描」記錄的熱門產業，用來判斷今天的輪動是不是巧合單日噴出。
        try:
            with self.connect() as conn:
                conn.row_factory = sqlite3.Row
                previous_meta_row = conn.execute(
                    "SELECT value FROM model_meta WHERE key = ?", ("last_monster_hot_sectors",)
                ).fetchone()
            previous_hot_sectors = json.loads(previous_meta_row["value"]) if previous_meta_row and previous_meta_row["value"] else []
        except Exception:
            previous_hot_sectors = []
        # 用「今天全部通過快篩的候選股」這個母體算類股輪動強度，樣本比之後
        # 截斷的 quick_candidates 更完整，統計上更有代表性。
        sector_history = self.load_radar_sector_history(before_date=today_key(), limit_dates=10)
        if not sector_history and previous_hot_sectors:
            sector_history = [{
                "date": "legacy_previous_scan",
                "sectors": {sector: {"hot": True} for sector in previous_hot_sectors},
            }]
        sector_momentum = self.compute_sector_momentum(
            candidates, stock_info=stock_info, history=sector_history
        )
        reference_date = max(
            (str(item.get("priceDate") or "")[:10] for item in candidates),
            default=today_key(),
        )
        market_regime = self.radar_market_regime_snapshot(reference_date, sector_momentum)

        candidates.sort(key=lambda item: (
            float(item.get("rawScore", item["score"]) or 0),
            float(item.get("volumeRatio") or 0),
            float(item.get("ret5") or 0),
        ), reverse=True)
        quick_candidates = candidates[:quick_limit]
        scored_candidates = quick_candidates[:min(score_limit, MONSTER_MAX_UPDATE_SYMBOLS)]
        scored_symbols = {item["symbol"] for item in scored_candidates}
        for watch_symbol in sorted(MONSTER_WATCH_SYMBOLS):
            if watch_symbol not in symbols or watch_symbol in scored_symbols:
                continue
            try:
                watch_quick = self.quick_monster_filter(watch_symbol)
                # 2026-07-04 稽核修復：原本只判 `if watch_quick:`，但
                # quick_monster_filter 對 stale_price_data/insufficient_history/
                # missing_verified_source 都回傳「truthy dict + ok=False」而非 None，
                # 導致斷更/歷史不足/低流動的 watch 股繞過快篩閘門進 predict_symbol，
                # 用凍結的舊日線算出無法成交的買點寫進「今天」的掃描結果。改成跟
                # 主候選迴圈(上方 `if quick and quick["ok"]`)一致，只放行 ok=True。
                if watch_quick and watch_quick.get("ok"):
                    scored_candidates.append(watch_quick)
                    scored_symbols.add(watch_symbol)
            except Exception as exc:
                errors.append({"symbol": watch_symbol, "error": str(exc)})
        scored_total = len(scored_candidates)
        update_candidates = [item["symbol"] for item in scored_candidates[:MONSTER_MAX_UPDATE_SYMBOLS]]
        for chunk_start in range(0, len(update_candidates), 25):
            chunk = update_candidates[chunk_start:chunk_start + 25]
            if not chunk:
                continue
            try:
                self.update_prices(chunk, refresh_info=False)
            except Exception as exc:
                errors.append({"symbol": ",".join(chunk), "error": str(exc)})
        for index, item in enumerate(scored_candidates, start=1):
            symbol = item["symbol"]
            if progress_callback:
                progress_callback({
                    "phase": "妖股型態量能排序",
                    "total": scored_total,
                    "processed": index - 1,
                    "saved": len(results),
                    "errors": len(errors),
                    "current": symbol,
                    "message": f"流動性候選 {total} 檔 → 快速候選 {len(candidates)} 檔 → 純規則評分 {scored_total} 檔",
                })
            try:
                # 妖股候選只使用真實價量與確定性風險規則；模型由獨立批量
                # 預測與紙上交易流程運行，不進候選、排序或盤中放行。
                row = self.monster_score_for_symbol(
                    symbol,
                    prediction=None,
                    repair=False,
                    sector_momentum=sector_momentum,
                    stock_info=stock_info,
                    use_model=False,
                    market_regime=market_regime,
                )
                row["quickScore"] = item["score"]
                results.append(row)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
            if progress_callback:
                progress_callback({
                    "phase": "妖股型態量能排序",
                    "total": scored_total,
                    "processed": index,
                    "saved": len(results),
                    "errors": len(errors),
                    "current": symbol,
                    "message": f"流動性候選 {total} 檔 → 快速候選 {len(candidates)} 檔 → 純規則評分 {scored_total} 檔",
                })

        with self.connect() as conn:
            if results:
                scan_date = today_key()
                try:
                    calendar_meta = {
                        str(meta_row[0]): meta_row[1]
                        for meta_row in conn.execute("""
                            SELECT key, value FROM model_meta
                            WHERE key LIKE 'twse_market_calendar_%'
                               OR key = ?
                        """, (f"dgpa_taipei_closure_{scan_date}",)).fetchall()
                    }
                except Exception:
                    calendar_meta = {}
                scan_market = self._cached_market_day_status(scan_date, calendar_meta)
                scan_invalid = not (
                    scan_market.get("known") is True
                    and scan_market.get("isTradingDay") is True
                )
                for row in results:
                    row["recordedBuyAllowed"] = bool(row.get("buyAllowed"))
                    row["invalidForTrading"] = scan_invalid
                    if scan_invalid:
                        row["buyAllowed"] = False
                        row["action"] = "WAIT"
                conn.execute("DELETE FROM monster_scores WHERE scan_date = ?", (scan_date,))
                # 同日重掃時策略訊號也要清舊：save_monster_score 會逐列寫入
                # BUY_WATCH，不清的話當日訊號集合會是所有掃描的聯集，
                # 早盤入選、午盤已跌出名單的股票仍掛著訊號。
                conn.execute(
                    "DELETE FROM strategy_signals WHERE signal_date = ? AND strategy = 'monster' AND side = 'BUY_WATCH'",
                    (scan_date,),
                )
                for row in results:
                    self.save_monster_score(conn, row)
                self.save_radar_sector_history(conn, scan_date, sector_momentum)
                self.set_meta(conn, "last_monster_scan_at", now_text())
                self.set_meta(conn, "last_monster_scan_count", str(len(results)))
                self.set_meta(conn, "last_monster_scan_errors", str(len(errors)))
                self.set_meta(conn, "last_monster_quick_candidates", str(len(candidates)))
                self.set_meta(conn, "last_monster_quick_selected", str(len(quick_candidates)))
                self.set_meta(conn, "last_monster_hot_sectors", json.dumps(sector_momentum.get("hotSectors") or [], ensure_ascii=False))
                self.set_meta(conn, "last_monster_sector_momentum", json.dumps(sector_momentum.get("sectors") or {}, ensure_ascii=False))
                self.set_meta(conn, "last_monster_theme_snapshot", json.dumps(sector_momentum, ensure_ascii=False))
                self.set_meta(conn, "last_monster_market_regime", json.dumps(market_regime, ensure_ascii=False))
            else:
                # 空結果不清舊列表(防止一次壞掃描把好名單洗掉)，但 meta 也要
                # 跟著保持舊值——只更新統計會讓前端出現「舊名單配新統計」的
                # 新舊混合資訊。另記一個空掃描標記供排查。
                self.set_meta(conn, "last_monster_empty_scan_at", now_text())
                self.set_meta(conn, "last_monster_empty_scan_errors", str(len(errors)))
        results.sort(key=lambda item: item["score"], reverse=True)
        if progress_callback:
            progress_callback({
                "phase": "完成",
                "total": total,
                "processed": total,
                "saved": len(results),
                "errors": len(errors),
                "current": "",
                "message": f"完成：流動性候選 {total} 檔 → 快速候選 {len(candidates)} 檔 → 候選評分 {scored_total} 檔",
            })
        return {
            "ok": True,
            "scanDate": today_key(),
            "scanned": total,
            "quickCandidates": len(candidates),
            "quickSelected": len(quick_candidates),
            "quickLimit": quick_limit,
            "modelCandidates": scored_total,
            "scoredCandidates": scored_total,
            "modelLimit": score_limit,
            "scoreLimit": score_limit,
            "watchSymbols": sorted(MONSTER_WATCH_SYMBOLS),
            "count": len(results),
            # errors 本體截斷防止回應過大，errorCount 保留真實總數不失真
            "errors": errors[:20],
            "errorCount": len(errors),
            "candidates": results,
            "hotSectors": sector_momentum.get("hotSectors") or [],
            "sectorMomentum": sector_momentum.get("sectors") or {},
            "themeSnapshot": sector_momentum,
            "marketRegime": market_regime,
        }

    def refresh_attention_disposition(self):
        """抓 TWSE 上市注意股(notetrans)/處置股(punish)免費OpenAPI,存 model_meta
        快取供 list_monster_scores 讀取貼旗標。⚠️只在每日排程/掃描呼叫,不可放進
        list_monster_scores 讀取熱路徑打網路(見 #132)。單邊(處置/注意)抓失敗時沿用
        舊快取的那半,不用空清單覆蓋;兩邊都失敗才完全不覆蓋。"""
        disp, attn = set(), set()
        disp_ok = attn_ok = False
        try:
            for item in self.fetch_openapi_json("https://openapi.twse.com.tw/v1/announcement/punish") or []:
                code = str(item.get("Code") or "").strip()
                if code:
                    disp.add(code)
            disp_ok = True
        except Exception:
            pass
        try:
            for item in self.fetch_openapi_json("https://openapi.twse.com.tw/v1/announcement/notetrans") or []:
                code = str(item.get("Code") or "").strip()
                if code:
                    attn.add(code)
            attn_ok = True
        except Exception:
            pass
        if not disp_ok and not attn_ok:
            return None
        # 只有一邊成功時,失敗那半用空 set 覆蓋會抹掉先前的處置/注意清單(風險旗標無預警消失)。
        # 讀舊快取,對失敗的那半沿用舊值,只更新成功那半。
        if not disp_ok or not attn_ok:
            try:
                with self.connect() as conn:
                    row = conn.execute("SELECT value FROM model_meta WHERE key = ?", ("attention_disposition_cache",)).fetchone()
                old = json.loads(row[0]) if row and row[0] else {}
            except Exception:
                old = {}
            if not disp_ok:
                disp = set(old.get("disposition") or [])
            if not attn_ok:
                attn = set(old.get("attention") or [])
        payload = {"fetchedAt": now_text(), "disposition": sorted(disp), "attention": sorted(attn)}
        with self.connect() as conn:
            self.set_meta(conn, "attention_disposition_cache", json.dumps(payload, ensure_ascii=False))
        return payload

    @staticmethod
    def _parse_attention_disposition(meta_value):
        try:
            d = json.loads(meta_value or "{}")
            return set(d.get("disposition") or []), set(d.get("attention") or [])
        except Exception:
            return set(), set()

    def _cached_market_day_status(self, date_text, meta=None):
        date_text = str(date_text or "")[:10]
        meta = meta if isinstance(meta, dict) else {}
        try:
            overrides = load_market_session_overrides(MARKET_SESSION_OVERRIDES_PATH)
            if date_text in overrides:
                return dict(overrides[date_text])
        except (OSError, ValueError, json.JSONDecodeError):
            pass

        try:
            day = dt.date.fromisoformat(date_text)
        except ValueError:
            return {"known": False, "isTradingDay": None, "date": date_text, "reason": "日期格式錯誤"}
        stored = {}
        try:
            stored = json.loads(meta.get(f"twse_market_calendar_{day.year}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = {}
        calendar = stored.get("calendar") if isinstance(stored, dict) else None
        planned_status = planned_market_day(day, calendar)
        if planned_status.get("known") is True and planned_status.get("isTradingDay") is False:
            return planned_status

        try:
            dgpa = json.loads(meta.get(f"dgpa_taipei_closure_{date_text}") or "{}")
            closure = dgpa.get("closure") if isinstance(dgpa, dict) else None
            if isinstance(closure, dict) and closure.get("marketClosed") is True:
                return {
                    "known": True,
                    "isTradingDay": False,
                    "date": date_text,
                    "reason": closure.get("reason") or "臺北市天然災害停止上班",
                    "source": closure.get("source") or "DGPA Taipei closure",
                    "emergencyClosure": True,
                }
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        if meta.get("last_official_close_sync_target_date") == date_text:
            known = str(meta.get("last_official_close_sync_calendar_known") or "") == "1"
            value = str(meta.get("last_official_close_sync_calendar_is_trading_day") or "")
            if known and value in {"0", "1"}:
                return {
                    "known": True,
                    "isTradingDay": value == "1",
                    "date": date_text,
                    "reason": meta.get("last_official_close_sync_calendar_reason") or "官方交易日狀態",
                    "source": meta.get("last_official_close_sync_calendar_source") or "official close sync",
                }

        return planned_status

    def radar_decision_validity(
        self,
        scan_date,
        price_dates=None,
        latest_complete_price_date=None,
        meta=None,
        current_date=None,
    ):
        scan_date = str(scan_date or "")[:10]
        current_date = str(current_date or today_key())[:10]
        price_dates = sorted({str(value or "")[:10] for value in (price_dates or []) if str(value or "")[:10]})
        latest_complete_price_date = str(latest_complete_price_date or "")[:10]
        meta = meta if isinstance(meta, dict) else {}
        scan_market = self._cached_market_day_status(scan_date, meta) if scan_date else {
            "known": False,
            "isTradingDay": None,
            "reason": "尚無雷達掃描",
        }
        current_market = self._cached_market_day_status(current_date, meta)
        reasons = []

        def add_reason(code, label):
            if not any(row.get("code") == code for row in reasons):
                reasons.append({"code": code, "label": label})

        if not scan_date:
            add_reason("no_scan", "尚無雷達掃描資料")
        elif scan_market.get("known") is not True:
            add_reason("scan_market_unknown", f"無法確認掃描日 {scan_date} 是否為交易日")
        elif scan_market.get("isTradingDay") is not True:
            add_reason("scan_market_closed", f"掃描日 {scan_date} 為休市日：{scan_market.get('reason') or '官方休市'}")

        if not price_dates:
            add_reason("missing_price_date", "雷達候選缺少日 K 日期")
        elif latest_complete_price_date and any(value != latest_complete_price_date for value in price_dates):
            add_reason(
                "daily_bar_stale",
                f"候選日 K 為 {', '.join(price_dates)}，最新完整日 K 為 {latest_complete_price_date}",
            )
        if scan_date and latest_complete_price_date and scan_date < latest_complete_price_date:
            add_reason(
                "scan_stale",
                f"掃描日 {scan_date} 早於最新完整日 K {latest_complete_price_date}",
            )

        if current_market.get("known") is not True:
            add_reason("current_market_unknown", f"無法確認今日 {current_date} 是否為交易日")
        elif current_market.get("isTradingDay") is not True:
            add_reason("current_market_closed", f"今日 {current_date} 非交易日：{current_market.get('reason') or '休市'}")

        reason_codes = [row["code"] for row in reasons]
        scan_valid = not any(code in {
            "no_scan", "scan_market_unknown", "scan_market_closed", "missing_price_date",
            "daily_bar_stale", "scan_stale",
        } for code in reason_codes)
        session_valid = not any(code in {"current_market_unknown", "current_market_closed"} for code in reason_codes)
        valid = bool(scan_valid and session_valid)
        return {
            "validForTrading": valid,
            "invalidForTrading": not valid,
            "scanValid": scan_valid,
            "currentSessionValid": session_valid,
            "scanDate": scan_date or None,
            "priceDates": price_dates,
            "latestCompletePriceDate": latest_complete_price_date or None,
            "currentDate": current_date,
            "scanMarket": scan_market,
            "currentMarket": current_market,
            "invalidReasons": reasons,
            "summary": "雷達決策資料有效" if valid else "；".join(row["label"] for row in reasons),
        }

    def select_radar_decision_scan(self, current_date=None):
        """Select the newest scan that is valid for the current session.

        Closed-day scans stay in the database for audit, but they must not
        shadow the previous valid close when the market opens again.
        """
        current_date = str(current_date or today_key())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            latest_scan_row = conn.execute("SELECT MAX(scan_date) FROM monster_scores").fetchone()
            latest_scan = str(latest_scan_row[0] or "")[:10] if latest_scan_row else ""
            if not latest_scan:
                scan_rows = []
                latest_complete = ""
            else:
                scan_rows = conn.execute("""
                    SELECT scan_date, price_date
                    FROM monster_scores
                    GROUP BY scan_date, price_date
                    ORDER BY scan_date DESC, price_date
                """).fetchall()
                complete = conn.execute(
                    """
                    SELECT date, COUNT(DISTINCT symbol) AS row_count
                    FROM prices
                    GROUP BY date
                    HAVING COUNT(DISTINCT symbol) >= ?
                    ORDER BY date DESC
                    LIMIT 1
                    """,
                    (RADAR_COMPLETE_DAILY_MIN_ROWS,),
                ).fetchone()
                if complete:
                    latest_complete = str(complete["date"] or "")
                else:
                    fallback = conn.execute("SELECT MAX(date) FROM prices").fetchone()
                    latest_complete = str(fallback[0] or "") if fallback else ""
            meta = {str(row[0]): row[1] for row in conn.execute("SELECT key, value FROM model_meta").fetchall()}
        price_dates_by_scan = {}
        for row in scan_rows:
            scan_date = str(row["scan_date"] or "")[:10]
            price_date = str(row["price_date"] or "")[:10]
            if scan_date:
                price_dates_by_scan.setdefault(scan_date, set())
                if price_date:
                    price_dates_by_scan[scan_date].add(price_date)
        scan_dates = sorted(price_dates_by_scan, reverse=True)
        latest_audit_scan = scan_dates[0] if scan_dates else ""
        latest_audit_validity = self.radar_decision_validity(
            latest_audit_scan,
            price_dates=sorted(price_dates_by_scan.get(latest_audit_scan) or []),
            latest_complete_price_date=latest_complete,
            meta=meta,
            current_date=current_date,
        )
        selected_scan = ""
        selected_validity = None
        for scan_date in scan_dates:
            validity = self.radar_decision_validity(
                scan_date,
                price_dates=sorted(price_dates_by_scan.get(scan_date) or []),
                latest_complete_price_date=latest_complete,
                meta=meta,
                current_date=current_date,
            )
            if validity.get("validForTrading") is True:
                selected_scan = scan_date
                selected_validity = validity
                break
        if not selected_scan:
            selected_scan = latest_audit_scan
            selected_validity = latest_audit_validity
        return {
            "selectedScanDate": selected_scan or None,
            "latestAuditScanDate": latest_audit_scan or None,
            "usingFallbackValidScan": bool(
                selected_scan and latest_audit_scan and selected_scan != latest_audit_scan
            ),
            "latestCompletePriceDate": latest_complete or None,
            "decisionValidity": selected_validity,
            "latestAuditValidity": latest_audit_validity,
        }

    def current_radar_decision_validity(self, current_date=None):
        selection = self.select_radar_decision_scan(current_date=current_date)
        validity = dict(selection.get("decisionValidity") or {})
        validity.update({
            "selectedScanDate": selection.get("selectedScanDate"),
            "latestAuditScanDate": selection.get("latestAuditScanDate"),
            "usingFallbackValidScan": selection.get("usingFallbackValidScan") is True,
        })
        return validity

    def validate_radar_order_context(self, symbol, scan_date, current_date=None):
        """Verify that a manual BUY really originated from the current valid radar scan."""
        code = str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
        requested_scan = str(scan_date or "")[:10]
        selection = self.select_radar_decision_scan(
            current_date=current_date or today_key()
        )
        selected_scan = str(selection.get("selectedScanDate") or "")[:10]
        validity = selection.get("decisionValidity") or {}
        if validity.get("validForTrading") is not True:
            return {
                "ok": False,
                "reason": "radar_decision_invalid",
                "symbol": code,
                "requestedScanDate": requested_scan or None,
                "selectedScanDate": selected_scan or None,
                "decisionValidity": validity,
            }
        if not requested_scan or requested_scan != selected_scan:
            return {
                "ok": False,
                "reason": "radar_scan_mismatch",
                "symbol": code,
                "requestedScanDate": requested_scan or None,
                "selectedScanDate": selected_scan or None,
                "decisionValidity": validity,
            }
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT symbol, invalid_for_trading
                FROM monster_scores
                WHERE scan_date = ? AND symbol = ?
                LIMIT 1
                """,
                (selected_scan, code),
            ).fetchone()
        if not row:
            return {
                "ok": False,
                "reason": "symbol_not_in_radar_scan",
                "symbol": code,
                "requestedScanDate": requested_scan,
                "selectedScanDate": selected_scan,
                "decisionValidity": validity,
            }
        if bool(row[1]):
            return {
                "ok": False,
                "reason": "radar_row_invalid_for_trading",
                "symbol": code,
                "requestedScanDate": requested_scan,
                "selectedScanDate": selected_scan,
                "decisionValidity": validity,
            }
        return {
            "ok": True,
            "reason": "verified_current_radar_candidate",
            "symbol": code,
            "requestedScanDate": requested_scan,
            "selectedScanDate": selected_scan,
            "decisionValidity": validity,
        }

    def list_monster_scores(self, limit=80):
        meta_int = read_meta_int  # 模組層函式，惰性 fallback，見其註解
        selection = self.select_radar_decision_scan()
        scan_date = selection.get("selectedScanDate")
        deployment_readiness = (
            self.current_radar_deployment_readiness(refresh_if_stale=True)
            if scan_date else {
                "ok": True,
                "readinessDate": None,
                "enforced": False,
                "formalReady": False,
                "observationOnly": True,
                "reasons": ["尚無雷達掃描資料"],
                "independentModelUsed": False,
            }
        )
        # 樣本不足本身就是觀察期，不能反過來成為放行理由。只有新口徑的
        # 盤中可成交報價確認戰績連續通過後，正式候選才可離開 performance veto。
        performance_veto = deployment_readiness.get("formalReady") is not True
        latest_audit_scan_date = selection.get("latestAuditScanDate")
        latest_complete_price_date = str(selection.get("latestCompletePriceDate") or "")
        decision_validity = selection.get("decisionValidity") or {}
        if not scan_date:
            return {
                "ok": True,
                "scanDate": None,
                "universeTotal": len(self.listed_symbols()),
                "liquidUniverse": len(self.liquid_monster_universe()),
                "scanned": 0,
                "quickCandidates": 0,
                "modelCandidates": 0,
                "scoredCandidates": 0,
                "ruleConfig": copy.deepcopy(RADAR_RULE_CONFIG),
                "entryPolicy": radar_trade_policy_payload(),
                "candidates": [],
                "decisionValidity": decision_validity,
                "deploymentReadiness": deployment_readiness,
                "latestAuditScanDate": latest_audit_scan_date,
                "usingFallbackValidScan": selection.get("usingFallbackValidScan") is True,
            }
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM model_meta").fetchall()}
            rows = []
            if scan_date:
                rows = conn.execute("""
                    SELECT ms.*, si.name, si.sector, si.market_type,
                           bvs.rule_action AS rule_action, bvs.rule_vetoed AS rule_vetoed,
                           bvs.rule_veto_reason AS rule_veto_reason, bvs.rule_overheated AS rule_overheated,
                           bvs.rule_rules AS rule_rules, bvs.rule_bonus_tags AS rule_bonus_tags
                    FROM monster_scores ms
                    LEFT JOIN stock_info si ON si.symbol = ms.symbol
                    LEFT JOIN brain_v2_snapshots bvs
                        ON bvs.symbol = ms.symbol AND bvs.price_date = ms.price_date AND bvs.context = 'monster'
                    WHERE ms.scan_date = ?
                    -- 妖股候選固定使用真實型態量能排序，不讀模型 probability。
                    ORDER BY ms.buy_allowed DESC, ms.score DESC, ms.volume_ratio DESC
                    LIMIT ?
                """, (scan_date, limit)).fetchall()
            # 進榜天數(tenure)：共用同一 conn 單次批量查詢，避免多開連線/ N+1
            # 拖慢這個已知敏感的端點(見下方 :3782 LAG 註解)。純讀 scan_date 歷史。
            tenure_map = {}
            if rows:
                try:
                    tenure_map = self.compute_radar_tenure(
                        symbols=[r["symbol"] for r in rows], conn=conn,
                        as_of_scan_date=scan_date,
                    )
                except Exception:
                    tenure_map = {}
        # listed_symbols()/liquid_monster_universe() 在 stock_info 表為空時會
        # 同步觸發 FinMind 網路請求(最長35秒)，搬到 with 區塊外面才呼叫，
        # 避免 SQLite 連線在網路 I/O 期間持續開著，放大 database is locked 風險。
        disp_set, attn_set = self._parse_attention_disposition(meta.get("attention_disposition_cache"))
        try:
            current_market_regime = json.loads(meta.get("last_monster_market_regime") or "{}")
        except (TypeError, ValueError):
            current_market_regime = {}
        current_regime_key = str(current_market_regime.get("key") or "theme_rotation")
        output = []
        for row in rows:
            item = dict(row)
            # monster_scores 保留舊欄位是為了既有 SQLite schema 相容；正式雷達
            # API 不回傳模型機率、模型門檻或模型版本。
            item.pop("probability", None)
            item.pop("threshold", None)
            item.pop("model_version", None)
            try:
                item["riskFlags"] = json.loads(item.pop("risk_flags", None) or "[]")
            except Exception:
                item["riskFlags"] = []
            _sym = str(item.get("symbol") or "")
            if _sym in disp_set:
                item["riskFlags"].append({"code": "disposition", "label": "已進處置", "severity": "danger"})
            elif _sym in attn_set:
                item["riskFlags"].append({"code": "attention", "label": "注意股", "severity": "warn"})
            if is_etf_like_stock(item.get("symbol"), item.get("name"), item.get("sector"), item.get("market_type")):
                continue
            stored_buy_allowed = bool(item.pop("buy_allowed"))
            recorded_buy_allowed = item.pop("recorded_buy_allowed", None)
            stored_invalid_for_trading = bool(item.pop("invalid_for_trading", 0))
            item["storedBuyAllowed"] = stored_buy_allowed
            item["recordedBuyAllowed"] = (
                stored_buy_allowed
                if recorded_buy_allowed is None
                else bool(recorded_buy_allowed)
            )
            # 稽核用的原始判斷仍需跑過目前風險/門檻政策，最後再由
            # invalidForTrading 強制歸零；資料庫正式 buy_allowed 已是 false。
            item["buyAllowed"] = item["recordedBuyAllowed"]
            danger_risk = any(flag.get("severity") == "danger" for flag in item["riskFlags"])
            regime_key = str(item.pop("market_regime", None) or current_regime_key)
            regime_threshold = self.safe_float(item.pop("regime_threshold", None))
            if regime_threshold is None:
                regime_threshold = radar_regime_threshold(regime_key)
            regime_threshold = max(RADAR_MIN_FORMAL_SCORE, float(regime_threshold))
            score_floor_blocked = (self.safe_float(item.get("score")) or 0.0) < regime_threshold
            if danger_risk:
                item["buyAllowed"] = False
                item["riskVetoed"] = True
                item["action"] = "WAIT"
                item["status"] = "高風險型態，只觀察不追"
            else:
                item["riskVetoed"] = False
            if score_floor_blocked:
                item["buyAllowed"] = False
                item["action"] = "WAIT"
                if not danger_risk:
                    item["status"] = f"{RADAR_REGIME_LABELS.get(regime_key, '題材輪動')}門檻 {regime_threshold:.0f} 分未達，只觀察"
            item["scoreFloorBlocked"] = score_floor_blocked
            item["minimumFormalScore"] = regime_threshold
            item["baseMinimumFormalScore"] = RADAR_MIN_FORMAL_SCORE
            item["marketRegime"] = regime_key
            item["marketRegimeLabel"] = RADAR_REGIME_LABELS.get(regime_key, "題材輪動")
            item["regimeThreshold"] = regime_threshold
            item["themeHeat"] = self.safe_float(item.pop("theme_heat", None)) or 0.0
            item["sectorThemeStreak"] = int(item.pop("sector_theme_streak", 0) or 0)
            try:
                item["themeSnapshot"] = json.loads(item.pop("theme_snapshot", None) or "{}")
            except (TypeError, ValueError):
                item["themeSnapshot"] = {}
            item["liquidityOk"] = bool(item.pop("liquidity_ok", 1))
            item["overheated"] = bool(item.get("overheated"))
            # 正規化成 camelCase：盤中判斷(server.py compute_monster_intraday_state
            # 的呼叫端)讀的是 surgeSetup/counterTrendStrength，舊版這裡原樣輸出
            # snake_case 的 DB 欄位名(且 counter_trend_strength 根本沒存)，導致
            # 盤中的 formal_watch_allowed 放行通道對正式掃描結果永遠不生效。
            item["surgeSetup"] = bool(item.pop("surge_setup", 0) or 0)
            item["recordedSurgeSetup"] = item["surgeSetup"]
            item["counterTrendStrength"] = bool(item.pop("counter_trend_strength", 0) or 0)
            item["reasons"] = json.loads(item.get("reasons") or "[]")
            entry_guardrail = radar_entry_guardrail_decision(
                item["surgeSetup"], self.safe_float(item.get("change5"))
            )
            item["entryGuardrailVetoed"] = entry_guardrail["vetoed"]
            item["entryGuardrailReasons"] = entry_guardrail["reasons"]
            item["entryGuardrailRules"] = entry_guardrail["rules"]
            if entry_guardrail["vetoed"]:
                item["buyAllowed"] = False
                item["action"] = "WAIT"
                if not danger_risk and not item.get("overheated") and not score_floor_blocked:
                    item["status"] = "不追價進場防線否決，只觀察"
                existing_reasons = set(str(reason) for reason in item["reasons"])
                for guardrail_reason in entry_guardrail["reasons"]:
                    text = f"進場防線：{guardrail_reason}"
                    if text not in existing_reasons:
                        item["reasons"].append(text)
            trigger = self.safe_float(item.get("buy_trigger")) or 0.0
            stop = self.safe_float(item.get("stop_price")) or 0.0
            target = self.safe_float(item.get("take_profit")) or 0.0
            item["rewardRiskRatio"] = round((target - trigger) / (trigger - stop), 3) if trigger > stop and target > trigger else None
            rule_action = item.pop("rule_action", None)
            rule_vetoed = item.pop("rule_vetoed", 0)
            rule_veto_reason = item.pop("rule_veto_reason", None)
            rule_overheated = item.pop("rule_overheated", 0)
            rule_rules = item.pop("rule_rules", None)
            rule_bonus_tags = item.pop("rule_bonus_tags", None)
            item["ruleEngine"] = {
                "action": rule_action,
                "vetoed": bool(rule_vetoed),
                "vetoReason": rule_veto_reason,
                "overheated": bool(rule_overheated),
                "rules": json.loads(rule_rules or "[]"),
                "bonusTags": json.loads(rule_bonus_tags or "[]"),
            } if rule_action else None
            item["tenure"] = tenure_map.get(item.get("symbol"))
            item["policyBuyAllowed"] = bool(item.get("buyAllowed"))
            item["performanceVetoed"] = performance_veto
            if performance_veto:
                item["buyAllowed"] = False
                item["action"] = "WAIT"
                if not danger_risk and not item.get("overheated") and not score_floor_blocked:
                    item["status"] = "盤中報價戰績與 walk-forward 尚未通過，只觀察"
            item["invalidForTrading"] = bool(
                stored_invalid_for_trading
                or decision_validity.get("invalidForTrading") is True
            )
            item["invalidReasons"] = copy.deepcopy(decision_validity.get("invalidReasons") or [])
            if item["invalidForTrading"]:
                item["originalStatus"] = item.get("status")
                invalid_codes = {reason.get("code") for reason in item["invalidReasons"]}
                if not danger_risk and not item.get("overheated") and not score_floor_blocked:
                    if "scan_market_closed" in invalid_codes:
                        item["status"] = "休市日掃描，只供稽核"
                    elif "daily_bar_stale" in invalid_codes or "scan_stale" in invalid_codes:
                        item["status"] = "日 K 或掃描日期過期，只觀察"
                    elif "current_market_closed" in invalid_codes:
                        item["status"] = "目前休市，買進建議停用"
                    else:
                        item["status"] = "雷達決策資料無效，只觀察"
                item["buyAllowed"] = False
                item["action"] = "WAIT"
            output.append(item)
        # fallback 一定要包成 lambda：liquid_monster_universe() 是 ~2.6 秒的
        # 全市場流動性掃描，之前寫成一般參數被 Python eager 評估，就算 meta
        # 有現成值每個 /api/monster-scores 請求都白跑一次——這就是 2026-07-03
        # 使用者回報「電腦版很LAG」的根因(單一端點 3 秒、頁面載入時被連環等待)。
        universe_total = meta_int(meta, "last_monster_universe_total", lambda: len(self.listed_symbols()))
        liquid_universe = meta_int(meta, "last_monster_liquid_universe", lambda: len(self.liquid_monster_universe()))
        quick_candidates = meta_int(meta, "last_monster_quick_candidates", len(output))
        # modelCandidates 是舊 API 鍵名，現況代表「完成純規則評分的檔數」；
        # scoredCandidates 提供正確語意，保留舊鍵避免既有前端/測試中斷。
        scored_candidates = meta_int(meta, "last_monster_scan_count", quick_candidates)
        scan_count = meta_int(meta, "last_monster_scan_count", len(output))
        try:
            hot_sectors = json.loads(meta.get("last_monster_hot_sectors") or "[]")
        except (TypeError, ValueError):
            hot_sectors = []
        try:
            sector_momentum = json.loads(meta.get("last_monster_sector_momentum") or "{}")
        except (TypeError, ValueError):
            sector_momentum = {}
        try:
            theme_snapshot = json.loads(meta.get("last_monster_theme_snapshot") or "{}")
        except (TypeError, ValueError):
            theme_snapshot = {}
        return {
            "ok": True,
            "scanDate": scan_date,
            "latestAuditScanDate": latest_audit_scan_date,
            "usingFallbackValidScan": selection.get("usingFallbackValidScan") is True,
            "universeTotal": universe_total,
            "liquidUniverse": liquid_universe,
            "scanned": scan_count,
            "quickCandidates": quick_candidates,
            "modelCandidates": scored_candidates,
            "scoredCandidates": scored_candidates,
            "ruleConfig": copy.deepcopy(RADAR_RULE_CONFIG),
            "entryPolicy": radar_trade_policy_payload(),
            "candidates": output,
            "hotSectors": hot_sectors,
            "sectorMomentum": sector_momentum,
            "themeSnapshot": theme_snapshot,
            "marketRegime": current_market_regime,
            "decisionValidity": decision_validity,
            "deploymentReadiness": deployment_readiness,
        }

    def sector_leader_map(self, limit=200):
        """族群龍頭連動地圖：現況只在候選卡上掛一個「族群輪動」徽章顯示產業名，
        看不到「這個熱門族群裡到底有哪些股票、誰是龍頭、誰在跟」。這裡把上一次
        掃描的候選(已評分、已帶 sector)依熱門族群分組，族群內用 score 由高到低排，
        最高分=龍頭，其餘=跟漲股，讓使用者一眼看出熱門族群的內部結構與輪動候補。

        純讀取 list_monster_scores() 的既有結果(不重掃、不打 FinMind)。只呈現精簡欄位
        (代號/名稱/分數/今日漲幅/5日漲幅/可否買)，不塞不相關的資料維度。"""
        scores = self.list_monster_scores(limit=limit)
        candidates = scores.get("candidates") or []
        hot_sectors = scores.get("hotSectors") or []
        sector_momentum = scores.get("sectorMomentum") or {}
        by_sector = {}
        for c in candidates:
            sector = c.get("sector") or "台股"
            if sector not in hot_sectors:
                continue
            def _round1(v):
                try:
                    return round(float(v), 1)
                except (TypeError, ValueError):
                    return None
            by_sector.setdefault(sector, []).append({
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "score": c.get("score"),
                "change1": _round1(c.get("change1")),   # 今日漲幅%
                "change5": _round1(c.get("change5")),   # 5日漲幅%
                "buyAllowed": bool(c.get("buyAllowed")),
            })
        sectors_out = []
        for sector in hot_sectors:  # 依 hotSectors 既有排序(excessRet5 由高到低)
            members = by_sector.get(sector) or []
            if not members:
                continue
            # 龍頭=系統評分最高者(score 缺當 -inf 排最後)，其餘依序為跟漲候補
            members.sort(key=lambda m: (m["score"] is not None, m["score"] if m["score"] is not None else 0), reverse=True)
            for i, m in enumerate(members):
                m["isLeader"] = (i == 0)
            stat = sector_momentum.get(sector) or {}
            sectors_out.append({
                "sector": sector,
                "excessRet5": stat.get("excessRet5"),
                "avgRet5": stat.get("avgRet5"),
                "persistentHot": bool(stat.get("persistentHot")),
                "memberCount": len(members),
                "leader": members[0],
                "members": members,
            })
        return {
            "ok": True,
            "scanDate": scores.get("scanDate"),
            "sectors": sectors_out,
        }

    def order_suggestion_list(self, limit=200):
        """今日可買清單：把上次掃描裡『系統判定可買(buyAllowed)』的候選集中成一張精簡表，
        每檔帶進場參考價/回檔買點/停損/停利，讓使用者不用在整個雷達清單裡東翻西找。
        **純參考清單，不會自動下單**——要買仍需自己到永豐手動下單面板逐筆送出。
        純讀 list_monster_scores() 既有結果,不重掃、不打 FinMind、不觸發任何委託。"""
        scores = self.list_monster_scores(limit=limit)
        candidates = scores.get("candidates") or []
        deployment_readiness = scores.get("deploymentReadiness") or {}
        validity = scores.get("decisionValidity") if isinstance(scores.get("decisionValidity"), dict) else {
            "validForTrading": False,
            "invalidForTrading": True,
            "invalidReasons": [{"code": "missing_decision_validity", "label": "雷達缺少決策有效性資訊"}],
            "summary": "雷達缺少決策有效性資訊",
        }
        if validity.get("validForTrading") is not True:
            return {
                "ok": True,
                "scanDate": scores.get("scanDate"),
                "count": 0,
                "manualOnly": True,
                "validForTrading": False,
                "reason": validity.get("summary") or "雷達決策資料無效",
                "invalidReasons": validity.get("invalidReasons") or [],
                "suppressedCount": sum(
                    1 for candidate in candidates
                    if candidate.get("policyBuyAllowed") or candidate.get("recordedBuyAllowed")
                ),
                "suggestions": [],
                "decisionValidity": validity,
                "deploymentReadiness": deployment_readiness,
            }
        if (
            deployment_readiness.get("enforced") is True
            and deployment_readiness.get("formalReady") is not True
        ):
            return {
                "ok": True,
                "scanDate": scores.get("scanDate"),
                "count": 0,
                "manualOnly": True,
                "validForTrading": True,
                "reason": "；".join(deployment_readiness.get("reasons") or ["雷達真實戰績尚未通過正式門檻"]),
                "invalidReasons": [],
                "suppressedCount": sum(
                    1 for candidate in candidates if candidate.get("policyBuyAllowed")
                ),
                "suggestions": [],
                "decisionValidity": validity,
                "deploymentReadiness": deployment_readiness,
            }

        def _round2(v):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        suggestions = []
        for c in candidates:
            if not c.get("buyAllowed"):
                continue
            suggestions.append({
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "sector": c.get("sector"),
                "score": c.get("score"),
                "entryTrigger": _round2(c.get("buy_trigger")),   # 觀察買點(突破觸發)
                "pullbackPrice": _round2(c.get("pullback_price")),  # 回檔買點
                "stopPrice": _round2(c.get("stop_price")),        # 建議停損
                "takeProfit": _round2(c.get("take_profit")),      # 參考停利
            })
        # 依 score 由高到低(list_monster_scores 已是 buy_allowed DESC, score DESC，但過濾後再排一次保險)
        suggestions.sort(key=lambda s: (s["score"] is not None, s["score"] if s["score"] is not None else 0), reverse=True)
        return {
            "ok": True,
            "scanDate": scores.get("scanDate"),
            "count": len(suggestions),
            "manualOnly": True,  # 前端據此顯示「僅供參考、需手動下單」提示，杜絕誤解為自動委託
            "suggestions": suggestions,
            "validForTrading": True,
            "reason": "",
            "invalidReasons": [],
            "suppressedCount": 0,
            "decisionValidity": validity,
            "deploymentReadiness": deployment_readiness,
        }

    def compute_radar_tenure(self, symbols=None, lookback_days=30, conn=None, as_of_scan_date=None):
        """妖股雷達進榜天數：純讀 monster_scores.scan_date 歷史，算每檔候選在雷達候選池
        (每次掃描存純規則評分前 ~100 檔)裡的生命週期。回 dict[symbol -> {...}]：
          daysOnRadar：從最新掃描日往回『連續出現的掃描日數』(中斷即停)
          rounds：近 lookback_days 內進出榜的段落數(不連續段落計數)
          firstSeen：近窗內第一次出現的掃描日(可能被窗起點截斷)
          peakScore/peakScoreDate：窗內最高分那天；isPeakToday：最高分就在最新掃描日
        全部 SELECT，絕不寫入、不打 FinMind。**掉出雷達候選池 ≠ 漲浪結束**(只代表跌出
        純規則評分前段)，呈現時要標明範圍。可傳入既有 conn 共用(避免 list_monster_scores
        端點多開連線)；未傳則自開唯讀連線。"""
        if conn is not None:
            return self._radar_tenure_from_conn(
                conn, symbols, lookback_days, as_of_scan_date=as_of_scan_date
            )
        with self.connect() as own:
            own.row_factory = sqlite3.Row
            return self._radar_tenure_from_conn(
                own, symbols, lookback_days, as_of_scan_date=as_of_scan_date
            )

    def _radar_tenure_from_conn(self, conn, symbols, lookback_days, as_of_scan_date=None):
        latest = str(as_of_scan_date or "")[:10] or conn.execute(
            "SELECT MAX(scan_date) FROM monster_scores"
        ).fetchone()[0]
        if not latest:
            return {}
        try:
            cutoff = (dt.date.fromisoformat(str(latest)[:10]) - dt.timedelta(days=int(lookback_days))).isoformat()
        except (TypeError, ValueError):
            return {}
        # 「連續」的基準單位=掃描日而非日曆日(週末/沒掃的日子不算中斷)。先取窗內所有掃描日。
        all_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM monster_scores WHERE scan_date >= ? AND scan_date <= ? ORDER BY scan_date",
            (cutoff, latest),
        ).fetchall()]
        if not all_dates:
            return {}
        latest_date = all_dates[-1]
        params = [cutoff]
        sql = "SELECT symbol, scan_date, score FROM monster_scores WHERE scan_date >= ? AND scan_date <= ?"
        params.append(latest)
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            sql += f" AND symbol IN ({placeholders})"
            params.extend([str(s) for s in symbols])
        sql += " ORDER BY symbol, scan_date"
        by_symbol = {}
        for row in conn.execute(sql, params).fetchall():
            sym, sd, score = row[0], row[1], row[2]
            by_symbol.setdefault(sym, {})[sd] = score
        result = {}
        for sym, date_scores in by_symbol.items():
            present = set(date_scores.keys())
            # daysOnRadar：從最新掃描日往回連續出現的掃描日數
            days_on = 0
            for d in reversed(all_dates):
                if d in present:
                    days_on += 1
                else:
                    break
            # rounds：窗內出現的不連續段落數(每次由「缺席→出現」就是新的一輪)
            rounds = 0
            prev_present = False
            for d in all_dates:
                cur = d in present
                if cur and not prev_present:
                    rounds += 1
                prev_present = cur
            peak_date = max(date_scores, key=lambda d: (date_scores[d] if date_scores[d] is not None else float("-inf")))
            peak_score = date_scores[peak_date]
            result[sym] = {
                "daysOnRadar": days_on,
                "rounds": rounds,
                "firstSeen": min(present),
                "peakScore": round(peak_score, 2) if peak_score is not None else None,
                "peakScoreDate": peak_date,
                "isPeakToday": peak_date == latest_date,
                "windowDays": int(lookback_days),
            }
        return result

    def measure_tenure_hit_rate(self, lookback_days=365):
        """量測用(非上線功能)：按『進榜第 N 天』分桶，算已結算 BUY_CANDIDATE 的 +10% 命中率。
        目的=判斷前端要不要掛「買早勝率較高」之類敘事；若命中率隨天數無單調關係，或樣本
        太少，前端就只顯示中性天數數字，絕不在未驗證類別重蹈 volume_ratio/sector 覆轍。
        用 predictions.hit(已結算標籤) JOIN 該檔在 price_date 當下的進榜連續天數。"""
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            scan_rows = conn.execute("SELECT symbol, scan_date FROM monster_scores ORDER BY symbol, scan_date").fetchall()
            all_scan_dates = sorted({r["scan_date"] for r in scan_rows})
            preds = conn.execute(
                "SELECT symbol, price_date, hit FROM predictions "
                "WHERE action = 'BUY_CANDIDATE' AND hit IS NOT NULL"
            ).fetchall()
        if not all_scan_dates or not preds:
            return {"ok": True, "sampleSize": 0, "byTenureBucket": [], "note": "資料不足(掃描歷史或已結算樣本為空)"}
        by_symbol_dates = {}
        for r in scan_rows:
            by_symbol_dates.setdefault(r["symbol"], set()).add(r["scan_date"])
        buckets = {}
        sample = 0
        for p in preds:
            sym = p["symbol"]
            pdate = str(p["price_date"])[:10]
            dates = by_symbol_dates.get(sym)
            if not dates:
                continue
            eligible = [d for d in all_scan_dates if d <= pdate]
            if not eligible:
                continue
            days_on = 0
            for d in reversed(eligible):
                if d in dates:
                    days_on += 1
                else:
                    break
            if days_on <= 0:
                continue
            bucket = buckets.setdefault(days_on, {"n": 0, "hits": 0})
            bucket["n"] += 1
            bucket["hits"] += 1 if p["hit"] else 0
            sample += 1
        out = []
        for day in sorted(buckets):
            b = buckets[day]
            out.append({
                "tenureDay": day,
                "candidateCount": b["n"],
                "hit10Rate": round(b["hits"] / b["n"], 4) if b["n"] else None,
            })
        return {"ok": True, "sampleSize": sample, "byTenureBucket": out}

    def record_radar_entry_snapshots(
        self, candidates, states, signal_date=None, return_details=False,
    ):
        """Persist the first executable quote for formal or shadow intraday confirmation."""
        candidate_by_symbol = {
            str(item.get("symbol") or ""): item for item in (candidates or []) if item.get("symbol")
        }
        records = []
        eligible_states = 0
        skipped_no_price = 0
        skipped_no_price_symbols = []
        for symbol, state in (states or {}).items():
            if not state or not (state.get("canBuy") or state.get("shadowCanBuy")):
                continue
            eligible_states += 1
            item = candidate_by_symbol.get(str(symbol)) or {}
            shadow_only = bool(state.get("shadowCanBuy") and not state.get("canBuy"))
            mode_prefix = "intraday_confirmed_shadow" if shadow_only else "intraday_confirmed"
            ask_price = self.safe_float(state.get("askPrice"))
            current_price = self.safe_float(state.get("currentPrice"))
            estimated_slippage_pct = self.safe_float(state.get("estimatedSlippagePct")) or 0.1
            execution_entry_price = self.safe_float(state.get("executionEntryPrice"))
            if execution_entry_price is not None and execution_entry_price > 0:
                entry_price = execution_entry_price
                entry_mode = f"{mode_prefix}_execution"
            elif ask_price is not None and ask_price > 0:
                entry_price = ask_price
                entry_mode = f"{mode_prefix}_ask"
            elif current_price is not None and current_price > 0:
                entry_price = current_price * (1 + estimated_slippage_pct / 100)
                entry_mode = f"{mode_prefix}_quote_plus_slippage"
            else:
                skipped_no_price += 1
                skipped_no_price_symbols.append(str(symbol))
                continue
            entry_at = str(state.get("snapshotAt") or now_text())
            row_signal_date = str(signal_date or entry_at[:10] or today_key())[:10]
            scan_date = str(item.get("scan_date") or item.get("scanDate") or row_signal_date)[:10]
            records.append((
                row_signal_date,
                str(symbol),
                scan_date,
                str(item.get("price_date") or item.get("priceDate") or "")[:10],
                self.safe_float(item.get("score")),
                str(state.get("setupType") or ""),
                entry_at,
                entry_price,
                entry_mode,
                str(state.get("source") or ""),
                self.safe_float(state.get("quoteAgeSeconds")),
                self.safe_float(state.get("bidPrice")),
                ask_price,
                self.safe_float(state.get("bidAskSpreadPct")),
                estimated_slippage_pct,
                now_text(),
            ))
        if not records:
            details = {
                "ok": eligible_states == 0,
                "eligibleStates": eligible_states,
                "prepared": 0,
                "inserted": 0,
                "duplicates": 0,
                "skippedNoPrice": skipped_no_price,
                "persisted": 0,
                "missingSymbols": sorted(set(skipped_no_price_symbols)),
            }
            return details if return_details else 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany("""
                INSERT OR IGNORE INTO radar_entry_snapshots (
                    signal_date, symbol, scan_date, price_date, score, setup_type,
                    entry_at, entry_price, entry_mode, quote_source, quote_age_seconds,
                    bid_price, ask_price, spread_pct, estimated_slippage_pct, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)
            inserted = conn.total_changes - before
            expected_keys = {(str(row[0]), str(row[1])) for row in records}
            signal_dates = sorted({key[0] for key in expected_keys})
            symbols = sorted({key[1] for key in expected_keys})
            date_placeholders = ",".join("?" for _ in signal_dates)
            symbol_placeholders = ",".join("?" for _ in symbols)
            persisted_rows = conn.execute(
                "SELECT signal_date, symbol FROM radar_entry_snapshots "
                f"WHERE signal_date IN ({date_placeholders}) "
                f"AND symbol IN ({symbol_placeholders})",
                [*signal_dates, *symbols],
            ).fetchall()
            persisted_keys = {
                (str(row[0]), str(row[1])) for row in persisted_rows
            } & expected_keys
            missing_keys = expected_keys - persisted_keys
            details = {
                "ok": not missing_keys and skipped_no_price == 0,
                "eligibleStates": eligible_states,
                "prepared": len(records),
                "inserted": inserted,
                "duplicates": max(0, len(records) - inserted),
                "skippedNoPrice": skipped_no_price,
                "persisted": len(persisted_keys),
                "missingSymbols": sorted(
                    {key[1] for key in missing_keys} | set(skipped_no_price_symbols)
                ),
            }
            return details if return_details else inserted

    @staticmethod
    def _radar_wilson_interval(hits, sample, z=1.96):
        if sample <= 0:
            return [None, None]
        rate = hits / sample
        denominator = 1 + z * z / sample
        centre = (rate + z * z / (2 * sample)) / denominator
        margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * sample)) / sample) / denominator
        return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]

    @classmethod
    def _radar_outcome_summary(cls, observations):
        settled = [item for item in observations if (item.get("outcome") or {}).get("settled")]
        pending = len(observations) - len(settled)
        if not settled:
            return {
                "signals": len(observations), "settled": 0, "pending": pending,
                "hits": 0, "targetHitRate": None, "targetHitConfidence95": [None, None],
                "stopRate": None, "winRate": None, "avgNetReturn": None,
                "medianNetReturn": None, "profitFactor": None, "avgHoldDays": None,
                "avgMaxFavorable": None, "avgMaxAdverse": None,
            }
        hits = sum(1 for item in settled if item["outcome"].get("targetHit"))
        stops = sum(1 for item in settled if item["outcome"].get("stopHit"))
        returns = [item["outcome"].get("netReturn") for item in settled]
        returns = [float(value) for value in returns if value is not None]
        wins = [value for value in returns if value > 0]
        losses = [-value for value in returns if value <= 0]
        hold_days = [item["outcome"].get("holdDays") for item in settled]
        hold_days = [float(value) for value in hold_days if value is not None]
        favorable = [item["outcome"].get("maxFavorable") for item in settled]
        favorable = [float(value) for value in favorable if value is not None]
        adverse = [item["outcome"].get("maxAdverse") for item in settled]
        adverse = [float(value) for value in adverse if value is not None]
        return {
            "signals": len(observations),
            "settled": len(settled),
            "pending": pending,
            "hits": hits,
            "targetHitRate": round(hits / len(settled), 4),
            "targetHitConfidence95": cls._radar_wilson_interval(hits, len(settled)),
            "stopRate": round(stops / len(settled), 4),
            "winRate": round(len(wins) / len(returns), 4) if returns else None,
            "avgNetReturn": round(sum(returns) / len(returns), 4) if returns else None,
            "medianNetReturn": round(statistics.median(returns), 4) if returns else None,
            "profitFactor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else None,
            "avgHoldDays": round(sum(hold_days) / len(hold_days), 2) if hold_days else None,
            "avgMaxFavorable": round(sum(favorable) / len(favorable), 4) if favorable else None,
            "avgMaxAdverse": round(sum(adverse) / len(adverse), 4) if adverse else None,
        }

    @classmethod
    def _radar_observation_experiment_payload(cls, eligible_observations):
        """Compare stricter rule filters without changing production scoring or gates."""
        criteria = {
            "minimumSettled": 50,
            "minimumAverageNetReturn": 0.0,
            "minimumProfitFactor": 1.0,
            "minimumTargetHitLift": 0.02,
            "productionRulesChanged": False,
        }
        specs = [
            ("baseline", "目前正式候選口徑", lambda item: True),
            ("avoid_risk_off", "避開弱勢避險盤", lambda item: item.get("marketRegime") != "risk_off"),
            ("theme_heat_40", "題材熱度至少 40", lambda item: float(item.get("themeHeat") or 0) >= 40),
            ("score_70", "規則分數至少 70", lambda item: float(item.get("score") or 0) >= 70),
            (
                "avoid_extended_5d",
                "排除 5 日已漲 15% 以上",
                lambda item: item.get("change5") is not None and float(item.get("change5")) < 15,
            ),
            ("surge_setup", "只保留完整妖股型態", lambda item: bool(item.get("surgeSetup"))),
            (
                "balanced_combo",
                "70 分、題材發酵且不追高",
                lambda item: (
                    item.get("marketRegime") != "risk_off"
                    and float(item.get("themeHeat") or 0) >= 40
                    and float(item.get("score") or 0) >= 70
                    and item.get("change5") is not None
                    and float(item.get("change5")) < 15
                ),
            ),
        ]

        def cohort_payload(rows, basis, label, adoption_basis):
            baseline = cls._radar_outcome_summary(rows)
            baseline_rate = baseline.get("targetHitRate")
            experiments = []
            for key, experiment_label, predicate in specs:
                selected = [item for item in rows if predicate(item)]
                stats = cls._radar_outcome_summary(selected)
                target_rate = stats.get("targetHitRate")
                lift = (
                    round(float(target_rate) - float(baseline_rate), 4)
                    if target_rate is not None and baseline_rate is not None else None
                )
                enough = int(stats.get("settled") or 0) >= criteria["minimumSettled"]
                economics_pass = bool(
                    stats.get("avgNetReturn") is not None
                    and float(stats["avgNetReturn"]) > criteria["minimumAverageNetReturn"]
                    and stats.get("profitFactor") is not None
                    and float(stats["profitFactor"]) > criteria["minimumProfitFactor"]
                )
                lift_pass = bool(
                    key != "baseline"
                    and lift is not None
                    and lift >= criteria["minimumTargetHitLift"]
                )
                research_qualified = bool(enough and economics_pass and lift_pass)
                experiments.append({
                    "key": key,
                    "label": experiment_label,
                    **stats,
                    "targetHitLift": lift,
                    "samplePass": enough,
                    "economicsPass": economics_pass,
                    "liftPass": lift_pass,
                    "researchQualified": research_qualified,
                    "adoptionCandidate": bool(adoption_basis and research_qualified),
                    "applied": False,
                })
            qualified = [item for item in experiments if item["researchQualified"]]
            qualified.sort(
                key=lambda item: (
                    float(item.get("avgNetReturn") or -99),
                    float(item.get("targetHitLift") or -99),
                    int(item.get("settled") or 0),
                ),
                reverse=True,
            )
            return {
                "basis": basis,
                "label": label,
                "adoptionEvidence": bool(adoption_basis),
                "baseline": baseline,
                "experiments": experiments,
                "bestQualified": qualified[0] if qualified else None,
            }

        live = [
            item for item in eligible_observations
            if is_intraday_confirmed_entry_mode(item.get("entryMode"))
        ]
        proxy = [
            item for item in eligible_observations
            if item.get("entryMode") == "next_open_backtest"
        ]
        live_payload = cohort_payload(
            live, "intraday_confirmed", "盤中可成交報價確認", True
        )
        proxy_payload = cohort_payload(
            proxy, "next_open_backtest", "隔日開盤代理研究", False
        )
        return {
            "mode": "observation",
            "generatedAt": now_text(),
            "criteria": criteria,
            "live": live_payload,
            "proxy": proxy_payload,
            "recommendedExperiment": live_payload.get("bestQualified"),
            "productionRulesChanged": False,
        }

    def compute_radar_score_track_record(self, lookback_days=365, conn=None):
        if conn is not None:
            return self._radar_score_track_record_from_conn(conn, lookback_days)
        with self.connect() as own:
            own.row_factory = sqlite3.Row
            return self._radar_score_track_record_from_conn(own, lookback_days)

    def _radar_score_track_record_from_conn(self, conn, lookback_days):
        latest_row = conn.execute("""
            SELECT MAX(scan_date)
            FROM monster_scores
            WHERE COALESCE(invalid_for_trading, 0) = 0
        """).fetchone()
        latest = str(latest_row[0] or "")[:10] if latest_row else ""
        empty = {
            "ok": True,
            "latestScanDate": latest or None,
            "lookbackDays": int(lookback_days),
            "policy": radar_trade_policy_payload(),
            "ruleConfig": copy.deepcopy(RADAR_RULE_CONFIG),
            "overall": self._radar_outcome_summary([]),
            "eligible": self._radar_outcome_summary([]),
            "scoreBuckets": [],
            "diagnostics": {
                "scoreMonotonic": None,
                "bestEligibleBucket": None,
                "setupGroups": [],
                "momentumGroups": [],
                "regimeGroups": [],
                "themeHeatGroups": [],
            },
            "entryModes": {},
            "entryModePerformance": {
                "intradayConfirmed": {
                    "label": "盤中可成交報價確認",
                    "all": self._radar_outcome_summary([]),
                    "eligible": self._radar_outcome_summary([]),
                },
                "nextOpenProxy": {
                    "label": "隔日開盤代理回測",
                    "all": self._radar_outcome_summary([]),
                    "eligible": self._radar_outcome_summary([]),
                },
            },
            "observationExperiments": self._radar_observation_experiment_payload([]),
            "topHits": [],
            "topMisses": [],
        }
        if not latest:
            return empty
        try:
            latest_date = dt.date.fromisoformat(latest)
            cutoff = (latest_date - dt.timedelta(days=max(1, int(lookback_days)))).isoformat()
            price_cutoff = (dt.date.fromisoformat(cutoff) - dt.timedelta(days=20)).isoformat()
        except (TypeError, ValueError):
            return empty
        candidates = conn.execute("""
            SELECT ms.*, si.name AS stock_name, si.sector AS stock_sector
            FROM monster_scores ms
            LEFT JOIN stock_info si ON si.symbol = ms.symbol
            WHERE ms.scan_date >= ?
              AND COALESCE(ms.invalid_for_trading, 0) = 0
            ORDER BY ms.scan_date, ms.score DESC
        """, (cutoff,)).fetchall()
        if not candidates:
            return empty
        candidates_by_scan_date = {}
        for candidate in candidates:
            candidate_item = dict(candidate)
            candidates_by_scan_date.setdefault(
                str(candidate_item.get("scan_date") or "")[:10], []
            ).append(candidate_item)
        try:
            market_rows = conn.execute("""
                SELECT * FROM market_prices
                WHERE NOT (market_key = 'TXF' AND source LIKE '%proxy%')
                ORDER BY market_key, date
            """).fetchall()
            market_payload = {}
            for market_row in market_rows:
                market_item = dict(market_row)
                market_payload.setdefault(str(market_item.get("market_key") or ""), []).append(market_item)
            radar_market_context = MarketContext(market_payload)
        except sqlite3.Error:
            radar_market_context = None
        scan_context = {}
        sector_history = []
        for scan_day in sorted(candidates_by_scan_date):
            day_rows = candidates_by_scan_date[scan_day]
            theme_rows = [{
                "symbol": row.get("symbol"),
                "sector": row.get("stock_sector") or "台股",
                "change5": row.get("change5"),
                "change20": row.get("change20"),
                "volumeRatio": row.get("volume_ratio"),
                "turnoverMillion": row.get("turnover_million"),
            } for row in day_rows]
            theme_snapshot = compute_sector_theme_snapshot(
                theme_rows, history=sector_history[-10:]
            )
            price_dates = [str(row.get("price_date") or "")[:10] for row in day_rows]
            reference_date = max((date for date in price_dates if date), default=scan_day)
            if radar_market_context is not None:
                regime_snapshot = self.radar_market_regime_snapshot(
                    reference_date, theme_snapshot, market_context=radar_market_context
                )
            else:
                regime_snapshot = classify_radar_market_regime({}, theme_snapshot)
            scan_context[scan_day] = {
                "regime": regime_snapshot,
                "theme": theme_snapshot,
            }
            sector_history.append({
                "date": scan_day,
                "sectors": theme_snapshot.get("sectors") or {},
            })
        symbols = sorted({str(row["symbol"]) for row in candidates})
        prices_by_symbol = {}
        for offset in range(0, len(symbols), 400):
            chunk = symbols[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT symbol, date, open, high, low, close FROM prices "
                f"WHERE symbol IN ({placeholders}) AND date >= ? ORDER BY symbol, date",
                (*chunk, price_cutoff),
            ).fetchall()
            for row in rows:
                prices_by_symbol.setdefault(str(row["symbol"]), []).append(dict(row))
        snapshot_rows = conn.execute(
            "SELECT * FROM radar_entry_snapshots WHERE scan_date >= ? ORDER BY entry_at", (cutoff,)
        ).fetchall()
        snapshots = {(str(row["scan_date"]), str(row["symbol"])): dict(row) for row in snapshot_rows}
        observations = []
        entry_modes = {}
        for row in candidates:
            item = dict(row)
            symbol = str(item.get("symbol") or "")
            series = prices_by_symbol.get(symbol) or []
            if not series:
                continue
            price_date = str(item.get("price_date") or "")[:10]
            start = next((idx for idx, price in enumerate(series) if str(price.get("date")) >= price_date), -1)
            if start < 0:
                continue
            snapshot = snapshots.get((str(item.get("scan_date") or "")[:10], symbol))
            if snapshot and self.safe_float(snapshot.get("entry_price")):
                entry_fill = float(snapshot["entry_price"])
                entry_date = str(snapshot.get("signal_date") or "")[:10]
                entry_index = next(
                    (idx for idx, price in enumerate(series) if str(price.get("date")) >= entry_date), -1
                )
                if entry_index < 0:
                    continue
                pseudo_entry_day = {
                    "date": entry_date,
                    "open": entry_fill,
                    "high": entry_fill,
                    "low": entry_fill,
                    "close": entry_fill,
                }
                future_rows = [pseudo_entry_day, *series[entry_index + 1:entry_index + MONSTER_TARGET_HORIZON_DAYS]]
                entry_mode = str(snapshot.get("entry_mode") or "intraday_confirmed_quote")
            else:
                if start + 1 >= len(series):
                    continue
                entry_day = series[start + 1]
                raw_open = self.safe_float(entry_day.get("open")) or self.safe_float(entry_day.get("close"))
                if raw_open is None or raw_open <= 0:
                    continue
                entry_fill = raw_open * (1 + PAPER_BASE_SLIPPAGE_RATE)
                entry_date = str(entry_day.get("date") or "")[:10]
                future_rows = series[start + 1:start + 1 + MONSTER_TARGET_HORIZON_DAYS]
                entry_mode = "next_open_backtest"
            outcome = simulate_radar_trade_path(entry_fill, future_rows)
            if not outcome:
                continue
            scan_day = str(item.get("scan_date") or "")[:10]
            historical_context = scan_context.get(scan_day) or {}
            historical_regime = historical_context.get("regime") or {}
            historical_theme = historical_context.get("theme") or {}
            sector = str(item.get("stock_sector") or "台股")
            sector_stat = (historical_theme.get("sectors") or {}).get(sector) or {}
            market_regime_key = str(
                item.get("market_regime") or historical_regime.get("key") or "theme_rotation"
            )
            entry_modes[entry_mode] = entry_modes.get(entry_mode, 0) + 1
            observations.append({
                "symbol": symbol,
                "name": item.get("stock_name") or "",
                "scanDate": str(item.get("scan_date") or "")[:10],
                "priceDate": price_date,
                "score": self.safe_float(item.get("score")) or 0.0,
                "action": str(item.get("action") or ""),
                "buyAllowed": bool(item.get("buy_allowed")),
                "surgeSetup": bool(item.get("surge_setup")),
                "change1": self.safe_float(item.get("change1")),
                "change5": self.safe_float(item.get("change5")),
                "change20": self.safe_float(item.get("change20")),
                "marketRegime": market_regime_key,
                "marketRegimeLabel": RADAR_REGIME_LABELS.get(market_regime_key, "題材輪動"),
                "themeHeat": self.safe_float(item.get("theme_heat")) or float(sector_stat.get("themeHeat") or 0),
                "sectorThemeStreak": int(item.get("sector_theme_streak") or sector_stat.get("streakDays") or 0),
                "entryDate": entry_date,
                "entryMode": entry_mode,
                "outcome": outcome,
            })
        if not observations:
            return empty

        def formal_observation_eligible(item):
            return bool(
                item["action"] == "NEXT_DAY_WATCH"
                or item["buyAllowed"]
                or is_intraday_confirmed_entry_mode(item.get("entryMode"))
            )

        bucket_specs = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 100.0001)]
        buckets = []
        for lower, upper in bucket_specs:
            bucket_rows = [item for item in observations if lower <= item["score"] < upper]
            eligible_rows = [
                item for item in bucket_rows if formal_observation_eligible(item)
            ]
            label = f"{int(lower)}-{int(upper - 1) if upper <= 100 else 100}"
            buckets.append({
                "key": f"{int(lower)}_{int(min(100, upper))}",
                "label": label,
                "minScore": lower,
                "maxScore": 100 if upper > 100 else upper,
                **self._radar_outcome_summary(bucket_rows),
                "eligible": self._radar_outcome_summary(eligible_rows),
            })
        settled = [item for item in observations if item["outcome"].get("settled")]
        hits = sorted(
            (item for item in settled if item["outcome"].get("targetHit")),
            key=lambda item: (item["score"], item["outcome"].get("netReturn") or -99),
            reverse=True,
        )[:8]
        misses = sorted(
            (item for item in settled if not item["outcome"].get("targetHit")),
            key=lambda item: item["score"],
            reverse=True,
        )[:8]

        def compact(item):
            outcome = item["outcome"]
            return {
                "symbol": item["symbol"], "name": item["name"], "scanDate": item["scanDate"],
                "score": item["score"], "entryDate": item["entryDate"], "entryMode": item["entryMode"],
                "entryPrice": round(outcome["entryFillPrice"], 2),
                "exitDate": outcome.get("exitDate"), "exitReason": outcome.get("exitReason"),
                "holdDays": outcome.get("holdDays"), "targetHit": outcome.get("targetHit"),
                "netReturn": round(outcome["netReturn"], 4) if outcome.get("netReturn") is not None else None,
                "maxFavorable": round(outcome["maxFavorable"], 4) if outcome.get("maxFavorable") is not None else None,
                "maxAdverse": round(outcome["maxAdverse"], 4) if outcome.get("maxAdverse") is not None else None,
            }

        eligible = [
            item for item in observations if formal_observation_eligible(item)
        ]
        intraday_confirmed = [
            item for item in observations
            if is_intraday_confirmed_entry_mode(item.get("entryMode"))
        ]
        next_open_proxy = [
            item for item in observations
            if item.get("entryMode") == "next_open_backtest"
        ]
        intraday_confirmed_eligible = [
            item for item in intraday_confirmed if formal_observation_eligible(item)
        ]
        next_open_proxy_eligible = [
            item for item in next_open_proxy if formal_observation_eligible(item)
        ]
        entry_mode_performance = {
            "intradayConfirmed": {
                "label": "盤中可成交報價確認",
                "all": self._radar_outcome_summary(intraday_confirmed),
                "eligible": self._radar_outcome_summary(intraday_confirmed_eligible),
            },
            "nextOpenProxy": {
                "label": "隔日開盤代理回測",
                "all": self._radar_outcome_summary(next_open_proxy),
                "eligible": self._radar_outcome_summary(next_open_proxy_eligible),
            },
        }
        eligible_bucket_stats = [
            (bucket["label"], bucket["eligible"])
            for bucket in buckets
            if int((bucket.get("eligible") or {}).get("settled") or 0) >= 10
            and (bucket.get("eligible") or {}).get("targetHitRate") is not None
        ]
        monotonic = None
        best_bucket = None
        if eligible_bucket_stats:
            rates = [float(stats["targetHitRate"]) for _, stats in eligible_bucket_stats]
            monotonic = all(current >= previous for previous, current in zip(rates, rates[1:]))
            best_label, best_stats = max(
                eligible_bucket_stats,
                key=lambda pair: (
                    float(pair[1].get("avgNetReturn") or -99),
                    float(pair[1].get("targetHitRate") or -1),
                ),
            )
            best_bucket = {
                "label": best_label,
                "settled": int(best_stats.get("settled") or 0),
                "targetHitRate": best_stats.get("targetHitRate"),
                "avgNetReturn": best_stats.get("avgNetReturn"),
                "profitFactor": best_stats.get("profitFactor"),
            }

        def diagnostic_group(key, label, rows):
            return {"key": key, "label": label, **self._radar_outcome_summary(rows)}

        diagnostics = {
            "scoreMonotonic": monotonic,
            "bestEligibleBucket": best_bucket,
            "setupGroups": [
                diagnostic_group("surge", "完整妖股型態", [item for item in eligible if item["surgeSetup"]]),
                diagnostic_group("non_surge", "一般型態量能", [item for item in eligible if not item["surgeSetup"]]),
            ],
            "momentumGroups": [
                diagnostic_group("ret5_lt_15", "5日漲幅低於15%", [
                    item for item in eligible if item.get("change5") is not None and item["change5"] < 15
                ]),
                diagnostic_group("ret5_gte_15", "5日漲幅15%以上", [
                    item for item in eligible if item.get("change5") is not None and item["change5"] >= 15
                ]),
            ],
            "regimeGroups": [
                diagnostic_group(key, label, [
                    item for item in eligible if item.get("marketRegime") == key
                ])
                for key, label in RADAR_REGIME_LABELS.items()
            ],
            "themeHeatGroups": [
                diagnostic_group("low", "題材熱度 0-39", [
                    item for item in eligible if float(item.get("themeHeat") or 0) < 40
                ]),
                diagnostic_group("warming", "題材熱度 40-69", [
                    item for item in eligible if 40 <= float(item.get("themeHeat") or 0) < 70
                ]),
                diagnostic_group("hot", "題材熱度 70-100", [
                    item for item in eligible if float(item.get("themeHeat") or 0) >= 70
                ]),
            ],
        }
        underperforming_groups = []
        for group_type, groups in (
            ("setup", diagnostics["setupGroups"]),
            ("momentum", diagnostics["momentumGroups"]),
            ("market_regime", diagnostics["regimeGroups"]),
            ("theme_heat", diagnostics["themeHeatGroups"]),
        ):
            for group in groups:
                if (
                    int(group.get("settled") or 0) >= 20
                    and group.get("avgNetReturn") is not None
                    and float(group["avgNetReturn"]) < 0
                ):
                    underperforming_groups.append({"groupType": group_type, **group})
        underperforming_groups.sort(
            key=lambda item: (float(item.get("avgNetReturn") or 0), -int(item.get("settled") or 0))
        )
        diagnostics["underperformingGroups"] = underperforming_groups
        observation_experiments = self._radar_observation_experiment_payload(eligible)
        return {
            **empty,
            "latestScanDate": latest,
            "overall": self._radar_outcome_summary(observations),
            "eligible": self._radar_outcome_summary(eligible),
            "scoreBuckets": buckets,
            "diagnostics": diagnostics,
            "entryModes": entry_modes,
            "entryModePerformance": entry_mode_performance,
            "observationExperiments": observation_experiments,
            "topHits": [compact(item) for item in hits],
            "topMisses": [compact(item) for item in misses],
        }

    def save_radar_strategy_experiment_snapshot(
        self, analysis_date=None, lookback_days=1095
    ):
        analysis_date = str(analysis_date or today_key())[:10]
        try:
            dt.date.fromisoformat(analysis_date)
        except ValueError as exc:
            raise ValueError("雷達規則觀察日期格式錯誤") from exc
        lookback_days = max(30, min(int(lookback_days or 1095), 1095))
        track_record = self.compute_radar_score_track_record(lookback_days=lookback_days)
        experiments = copy.deepcopy(track_record.get("observationExperiments") or {})
        experiments.update({
            "analysisDate": analysis_date,
            "lookbackDays": lookback_days,
            "latestScanDate": track_record.get("latestScanDate"),
            "underperformingGroups": (
                (track_record.get("diagnostics") or {}).get("underperformingGroups") or []
            ),
        })
        live_settled = int(
            (((experiments.get("live") or {}).get("baseline") or {}).get("settled")) or 0
        )
        proxy_settled = int(
            (((experiments.get("proxy") or {}).get("baseline") or {}).get("settled")) or 0
        )
        qualified_count = sum(
            1 for item in ((experiments.get("live") or {}).get("experiments") or [])
            if item.get("adoptionCandidate") is True
        )
        generated_at = str(experiments.get("generatedAt") or now_text())
        with self.connect() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO radar_strategy_experiment_runs (
                    analysis_date, generated_at, lookback_days, live_settled,
                    proxy_settled, qualified_count, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                analysis_date, generated_at, lookback_days, live_settled,
                proxy_settled, qualified_count,
                json.dumps(experiments, ensure_ascii=False, separators=(",", ":"), default=str),
            ))
            saved = int(cursor.rowcount or 0) > 0
            if not saved:
                row = conn.execute(
                    "SELECT payload_json FROM radar_strategy_experiment_runs WHERE analysis_date = ?",
                    (analysis_date,),
                ).fetchone()
                try:
                    experiments = json.loads(row[0] or "{}") if row else experiments
                except (json.JSONDecodeError, TypeError):
                    pass
            self.set_meta(conn, "last_radar_strategy_experiment_date", analysis_date)
            self.set_meta(conn, "last_radar_strategy_experiment_at", generated_at)
            self.set_meta(conn, "last_radar_strategy_experiment_live_settled", str(live_settled))
            self.set_meta(conn, "last_radar_strategy_experiment_qualified", str(qualified_count))
        return {"ok": True, "saved": saved, **experiments}

    def list_radar_strategy_experiment_snapshots(self, limit=30):
        limit = max(1, min(int(limit or 30), 365))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM radar_strategy_experiment_runs
                ORDER BY analysis_date DESC, id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                payload = json.loads(record.pop("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            records.append({**record, "payload": payload})
        return records

    def refresh_radar_deployment_readiness(
        self, as_of_date=None, min_samples=50, required_pass_days=5
    ):
        """Persist the rule radar's real-performance deployment gate.

        This gate never changes scores or weights.  It only decides whether a
        formally buyable candidate may leave observation mode.  Both recent
        real-price outcomes and the independent walk-forward calibration must
        pass for several recorded trading days.
        """
        readiness_date = str(as_of_date or today_key())[:10]
        try:
            dt.date.fromisoformat(readiness_date)
        except ValueError as exc:
            raise ValueError("雷達戰績驗證日期格式錯誤") from exc
        min_samples = max(1, int(min_samples or 50))
        required_pass_days = max(1, int(required_pass_days or 5))
        stats = self.compute_radar_score_track_record(lookback_days=365)
        all_eligible = stats.get("eligible") or {}
        entry_mode_performance = stats.get("entryModePerformance") or {}
        confirmed_group = entry_mode_performance.get("intradayConfirmed") or {}
        proxy_group = entry_mode_performance.get("nextOpenProxy") or {}
        confirmed = confirmed_group.get("eligible") or {}
        proxy = proxy_group.get("eligible") or {}
        confirmed_signals = int(confirmed.get("signals") or 0)
        settled = int(confirmed.get("settled") or 0)
        target_hit_rate = self.safe_float(confirmed.get("targetHitRate"))
        avg_net_return = self.safe_float(confirmed.get("avgNetReturn"))
        profit_factor = self.safe_float(confirmed.get("profitFactor"))
        proxy_settled = int(proxy.get("settled") or 0)
        proxy_target_hit_rate = self.safe_float(proxy.get("targetHitRate"))
        proxy_avg_net_return = self.safe_float(proxy.get("avgNetReturn"))
        proxy_profit_factor = self.safe_float(proxy.get("profitFactor"))
        enforced = settled >= min_samples
        live_pass = bool(
            enforced
            and avg_net_return is not None and avg_net_return > 0
            and profit_factor is not None and profit_factor > 1
        )

        walk_forward = (RADAR_RULE_CONFIG.get("walkForward") or {})
        calibrated = walk_forward.get("calibrated") or {}
        wf_trades = int(calibrated.get("trades") or 0)
        wf_avg_net = self.safe_float(calibrated.get("avgNetReturn"))
        wf_profit_factor = self.safe_float(calibrated.get("profitFactor"))
        precision_lift = self.safe_float(walk_forward.get("precisionLift"))
        walk_forward_pass = bool(
            wf_trades >= 100
            and wf_avg_net is not None and wf_avg_net > 0
            and wf_profit_factor is not None and wf_profit_factor > 1
            and precision_lift is not None and precision_lift >= 0.02
        )
        pass_today = live_pass and walk_forward_pass
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            previous = conn.execute("""
                SELECT readiness_date, live_pass, walk_forward_pass, payload_json
                FROM radar_deployment_readiness
                WHERE readiness_date < ?
                ORDER BY readiness_date DESC
                LIMIT ?
            """, (readiness_date, required_pass_days)).fetchall()
            consecutive = 1 if pass_today else 0
            if pass_today:
                for row in previous:
                    try:
                        previous_payload = json.loads(row["payload_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        previous_payload = {}
                    same_basis = str(
                        previous_payload.get("performanceBasisVersion") or ""
                    ) == "2"
                    if same_basis and bool(row["live_pass"]) and bool(row["walk_forward_pass"]):
                        consecutive += 1
                    else:
                        break
            formal_ready = bool(enforced and pass_today and consecutive >= required_pass_days)
            reasons = []
            if not enforced:
                reasons.append(
                    f"盤中可成交報價確認結算樣本 {settled}/{min_samples}，樣本不足，維持觀察"
                )
            else:
                if avg_net_return is None or avg_net_return <= 0:
                    reasons.append("盤中可成交報價確認候選平均成本後報酬尚未轉正")
                if profit_factor is None or profit_factor <= 1:
                    reasons.append("盤中可成交報價確認候選獲利因子尚未大於 1")
            if wf_avg_net is None or wf_avg_net <= 0:
                reasons.append("日線代理 walk-forward 平均成本後報酬尚未轉正")
            if wf_profit_factor is None or wf_profit_factor <= 1:
                reasons.append("日線代理 walk-forward 獲利因子尚未大於 1")
            if precision_lift is None or precision_lift < 0.02:
                reasons.append("日線代理 walk-forward 命中率提升未達 2 個百分點")
            if pass_today and consecutive < required_pass_days:
                reasons.append(f"通過天數 {consecutive}/{required_pass_days}，仍在觀察期")
            if formal_ready:
                reasons = ["盤中可成交報價確認戰績與日線代理 walk-forward 已連續通過正式門檻"]
            payload = {
                "ok": True,
                "readinessDate": readiness_date,
                "generatedAt": now_text(),
                "performanceBasisVersion": 2,
                "performanceBasis": "intraday_confirmed_only",
                "enforced": enforced,
                "formalReady": formal_ready,
                "observationOnly": not formal_ready,
                "minimumSamples": min_samples,
                "requiredPassDays": required_pass_days,
                "consecutivePassDays": consecutive,
                "live": {
                    "pass": live_pass,
                    "entryMode": "intraday_confirmed",
                    "signals": confirmed_signals,
                    "settled": settled,
                    "targetHitRate": target_hit_rate,
                    "avgNetReturn": avg_net_return,
                    "profitFactor": profit_factor,
                },
                "proxy": {
                    "entryMode": "next_open_backtest",
                    "settled": proxy_settled,
                    "targetHitRate": proxy_target_hit_rate,
                    "avgNetReturn": proxy_avg_net_return,
                    "profitFactor": proxy_profit_factor,
                },
                "allEligible": {
                    "settled": int(all_eligible.get("settled") or 0),
                    "targetHitRate": self.safe_float(all_eligible.get("targetHitRate")),
                    "avgNetReturn": self.safe_float(all_eligible.get("avgNetReturn")),
                    "profitFactor": self.safe_float(all_eligible.get("profitFactor")),
                },
                "walkForward": {
                    "pass": walk_forward_pass,
                    "trades": wf_trades,
                    "precisionLift": precision_lift,
                    "avgNetReturn": wf_avg_net,
                    "profitFactor": wf_profit_factor,
                },
                "reasons": reasons,
                "ruleConfigSource": RADAR_RULE_CONFIG.get("source"),
                "independentModelUsed": False,
            }
            conn.execute("""
                INSERT INTO radar_deployment_readiness (
                    readiness_date, generated_at, eligible_settled,
                    target_hit_rate, avg_net_return, profit_factor,
                    live_pass, walk_forward_pass, consecutive_pass_days,
                    enforced, formal_ready, reasons_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(readiness_date) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    eligible_settled = excluded.eligible_settled,
                    target_hit_rate = excluded.target_hit_rate,
                    avg_net_return = excluded.avg_net_return,
                    profit_factor = excluded.profit_factor,
                    live_pass = excluded.live_pass,
                    walk_forward_pass = excluded.walk_forward_pass,
                    consecutive_pass_days = excluded.consecutive_pass_days,
                    enforced = excluded.enforced,
                    formal_ready = excluded.formal_ready,
                    reasons_json = excluded.reasons_json,
                    payload_json = excluded.payload_json
            """, (
                readiness_date, payload["generatedAt"], settled,
                target_hit_rate, avg_net_return, profit_factor,
                1 if live_pass else 0, 1 if walk_forward_pass else 0, consecutive,
                1 if enforced else 0, 1 if formal_ready else 0,
                json.dumps(reasons, ensure_ascii=False, separators=(",", ":")),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            ))
        return payload

    def current_radar_deployment_readiness(self, refresh_if_stale=False):
        today = today_key()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT readiness_date, payload_json
                FROM radar_deployment_readiness
                ORDER BY readiness_date DESC
                LIMIT 1
            """).fetchone()
            meta = {
                str(item[0]): item[1]
                for item in conn.execute("SELECT key, value FROM model_meta").fetchall()
            }
        payload = {}
        if row:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
        if row and isinstance(payload, dict) and payload:
            try:
                basis_version = int(payload.get("performanceBasisVersion") or 0)
            except (TypeError, ValueError):
                basis_version = 0
            if basis_version < 2:
                try:
                    return self.refresh_radar_deployment_readiness(
                        as_of_date=str(row["readiness_date"] or "")[:10]
                    )
                except Exception:
                    return {
                        **payload,
                        "performanceBasisVersion": 2,
                        "performanceBasis": "intraday_confirmed_only",
                        "enforced": False,
                        "formalReady": False,
                        "observationOnly": True,
                        "consecutivePassDays": 0,
                        "live": {
                            "pass": False,
                            "entryMode": "intraday_confirmed",
                            "signals": 0,
                            "settled": 0,
                        },
                        "reasons": ["舊版戰績口徑尚未完成盤中可成交報價確認重算，維持觀察"],
                    }
        if refresh_if_stale and (not row or str(row["readiness_date"] or "") != today):
            market_day = self._cached_market_day_status(today, meta)
            if market_day.get("known") is True and market_day.get("isTradingDay") is True:
                return self.refresh_radar_deployment_readiness(as_of_date=today)
        if isinstance(payload, dict) and payload:
            return payload
        return {
            "ok": True,
            "readinessDate": None,
            "generatedAt": None,
            "performanceBasisVersion": 2,
            "performanceBasis": "intraday_confirmed_only",
            "enforced": False,
            "formalReady": False,
            "observationOnly": True,
            "consecutivePassDays": 0,
            "live": {
                "pass": False,
                "entryMode": "intraday_confirmed",
                "signals": 0,
                "settled": 0,
            },
            "proxy": {"entryMode": "next_open_backtest", "settled": 0},
            "walkForward": {"pass": False},
            "reasons": ["尚未建立雷達正式上線驗證紀錄"],
            "independentModelUsed": False,
        }

    def compute_candidate_followthrough(self, lookback_days=14, min_group=8, conn=None):
        """妖股候選複盤看板：拿近 lookback_days 個掃描日的 monster_scores 候選快照，用 prices
        表直接算「掃描後續 10 個交易日實際走勢」——是否曾摸到 +10%(maxFavorable/touched10pct)、
        第10日收盤是否達標(hitClose，**與既有結算 hit 同定義**)。回答「雷達每天叫我看的候選，
        實際多常真的走出去、哪一類最會發動」。**現有雷達戰績看板因 predictions.hit 尚未回填
        近乎空白，本卡改用 prices 直算補上這段空窗。**

        誠實收斂(沿用 G 案紀律)：①命中率只算『已滿10交易日』的成熟候選(guardrail：半途報酬
        不進命中分母)；②分群樣本 < min_group 只標『樣本不足』不下比率；③maxFavorable 明確是
        『曾觸及』(對齊停利出場)非收盤結算，不另立打架指標；④範圍=純規則評分前段候選池，不等於
        使用者實際買賣績效。全程唯讀 SELECT、不改評分、不打 FinMind、不觸發委託。"""
        if conn is not None:
            return self._candidate_followthrough_from_conn(conn, lookback_days, min_group)
        with self.connect() as own:
            own.row_factory = sqlite3.Row
            return self._candidate_followthrough_from_conn(own, lookback_days, min_group)

    def _candidate_followthrough_from_conn(self, conn, lookback_days, min_group):
        horizon = MONSTER_TARGET_HORIZON_DAYS
        target = MONSTER_TARGET_RETURN
        empty = {
            "ok": True, "latestScanDate": None, "lookbackDays": int(lookback_days),
            "horizonDays": horizon, "targetReturn": target,
            "overall": {"candidatesInWindow": 0, "settled": 0, "touched10pctRate": None,
                        "hitCloseRate": None, "avgMaxFavorable": None, "avgCloseReturn": None},
            "groups": {}, "topFollowThrough": [], "topMissed": [],
        }
        latest = conn.execute("""
            SELECT MAX(scan_date)
            FROM monster_scores
            WHERE COALESCE(invalid_for_trading, 0) = 0
        """).fetchone()[0]
        if not latest:
            return empty
        try:
            cutoff = (dt.date.fromisoformat(str(latest)[:10]) - dt.timedelta(days=int(lookback_days))).isoformat()
            price_cutoff = (dt.date.fromisoformat(str(latest)[:10]) - dt.timedelta(days=int(lookback_days) + 5)).isoformat()
        except (TypeError, ValueError):
            return empty
        cand_rows = conn.execute("""
            SELECT ms.symbol, ms.scan_date, ms.price_date, ms.close, ms.score, ms.action,
                   ms.surge_setup, ms.volume_ratio, si.name AS name
            FROM monster_scores ms
            LEFT JOIN stock_info si ON si.symbol = ms.symbol
            WHERE ms.scan_date >= ?
              AND COALESCE(ms.invalid_for_trading, 0) = 0
            ORDER BY ms.scan_date, ms.score DESC
        """, (cutoff,)).fetchall()
        if not cand_rows:
            return empty
        symbols = sorted({r["symbol"] for r in cand_rows})
        # 批量把涉及 symbols 的 prices(只要 date/close/high)一次撈進記憶體，避免 N+1。
        prices_by_symbol = {}
        for i in range(0, len(symbols), 400):
            chunk = symbols[i:i + 400]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT symbol, date, close, high FROM prices "
                f"WHERE symbol IN ({placeholders}) AND date >= ? ORDER BY symbol, date",
                (*chunk, price_cutoff),
            ).fetchall():
                prices_by_symbol.setdefault(row["symbol"], []).append((row["date"], row["close"], row["high"]))

        observations = []
        for r in cand_rows:
            sym = r["symbol"]
            entry_close = r["close"]
            if not entry_close:  # 髒列 close=0/None：比照 compute_prediction_outcomes 靜默跳過
                continue
            series = prices_by_symbol.get(sym)
            if not series:
                continue
            dates = [s[0] for s in series]
            didx = {d: idx for idx, d in enumerate(dates)}
            pdate = str(r["price_date"] or "")[:10]
            start = didx.get(pdate)
            if start is None:
                start = next((idx for idx, d in enumerate(dates) if d >= pdate), -1)
            if start < 0:
                continue
            target_index = start + horizon
            matured = target_index < len(series)
            end = min(target_index, len(series) - 1)
            window = series[start + 1:end + 1]  # (date, close, high) 逐日後續
            days_observed = max(0, end - start)
            max_fav = None
            days_to_peak = None
            if window:
                highs = [w[2] for w in window if w[2] is not None]
                if highs:
                    peak_high = max(highs)
                    max_fav = (peak_high - entry_close) / entry_close
                    days_to_peak = [w[2] for w in window].index(peak_high) + 1
            touched = 1 if (max_fav is not None and max_fav >= target) else 0
            # 大漲版:另量「窗內盤中最高摸到 +20%」(飆股門檻),純測量、不影響 y/門檻/模型。
            touched20 = 1 if (max_fav is not None and max_fav >= 0.20) else 0
            close_return = None
            hit_close = None
            if matured and series[target_index][1]:
                close_return = (series[target_index][1] - entry_close) / entry_close
                hit_close = 1 if close_return >= target else 0
            observations.append({
                "symbol": sym, "name": r["name"], "scanDate": r["scan_date"],
                "entryClose": round(float(entry_close), 2), "score": r["score"], "action": r["action"],
                "surgeSetup": bool(r["surge_setup"]), "volumeRatio": r["volume_ratio"],
                "matured": matured, "daysObserved": days_observed,
                "maxFavorable": round(max_fav, 4) if max_fav is not None else None,
                "daysToPeak": days_to_peak, "touched10pct": touched, "touched20pct": touched20,
                "closeReturn": round(close_return, 4) if close_return is not None else None,
                "hitClose": hit_close,
            })

        matured_obs = [o for o in observations if o["matured"]]

        def _rate(rows, key):
            vals = [r[key] for r in rows if r[key] is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        mfe_matured = [o["maxFavorable"] for o in matured_obs if o["maxFavorable"] is not None]
        overall = {
            "candidatesInWindow": len(observations),
            "settled": len(matured_obs),
            "touched10pctRate": _rate(matured_obs, "touched10pct"),
            "touched20pctRate": _rate(matured_obs, "touched20pct"),  # 大漲版:飆股 +20% 觸及率
            "hitCloseRate": _rate(matured_obs, "hitClose"),
            "avgMaxFavorable": round(sum(mfe_matured) / len(mfe_matured), 4) if mfe_matured else None,
            "avgCloseReturn": _rate(matured_obs, "closeReturn"),
        }

        def _group_stat(rows):
            n = len(rows)
            if n < min_group:
                return {"sample": n, "insufficientSample": True, "touched10pctRate": None, "hitCloseRate": None}
            return {"sample": n, "insufficientSample": False,
                    "touched10pctRate": _rate(rows, "touched10pct"), "hitCloseRate": _rate(rows, "hitClose")}

        actions = sorted({o["action"] for o in matured_obs if o["action"]})
        groups = {
            "byAction": {a: _group_stat([o for o in matured_obs if o["action"] == a]) for a in actions},
            "bySurgeSetup": {
                "surge": _group_stat([o for o in matured_obs if o["surgeSetup"]]),
                "normal": _group_stat([o for o in matured_obs if not o["surgeSetup"]]),
            },
            "byVolumeRatio": {
                "high": _group_stat([o for o in matured_obs if (o["volumeRatio"] or 0) >= 5]),
                "normal": _group_stat([o for o in matured_obs if (o["volumeRatio"] or 0) < 5]),
            },
        }
        # 展示：曾摸到 +10% 的候選(含未成熟，明確標 daysObserved)當『雷達確實抓到過的實例』，
        # 讓看板即使結算樣本還沒滿也有今天可看的具體證據(非命中率、不進分母)。
        # 同一檔在多個掃描日各有一筆觀測 → 每檔只留最佳(maxFavorable 最高)那筆，避免重複洗版。
        def _best_per_symbol(rows, key):
            best = {}
            for o in rows:
                cur = best.get(o["symbol"])
                if cur is None or (o.get(key) or 0) > (cur.get(key) or 0):
                    best[o["symbol"]] = o
            return list(best.values())

        touched_all = sorted(
            _best_per_symbol([o for o in observations if o["touched10pct"]], "maxFavorable"),
            key=lambda o: (o["maxFavorable"] or 0), reverse=True,
        )[:8]
        top_missed = sorted(
            _best_per_symbol([o for o in matured_obs if not o["touched10pct"]], "score"),
            key=lambda o: (o["score"] or 0), reverse=True,
        )[:5]
        return {
            "ok": True, "latestScanDate": latest, "lookbackDays": int(lookback_days),
            "horizonDays": horizon, "targetReturn": target,
            "overall": overall, "groups": groups,
            "topFollowThrough": touched_all, "topMissed": top_missed,
        }

    def load_market_rows(self):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM market_prices
                WHERE NOT (market_key = 'TXF' AND source LIKE '%proxy%')
                ORDER BY market_key, date
            """).fetchall()
        output = {}
        for row in rows:
            output.setdefault(row["market_key"], []).append(dict(row))
        return output

    def load_stock_info(self):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM stock_info").fetchall()
        return {row["symbol"]: dict(row) for row in rows}

    def sma(self, values, period, index):
        if index < period - 1:
            return None
        return sum(values[index - period + 1:index + 1]) / period

    def ema_series(self, values, period):
        multiplier = 2 / (period + 1)
        output = []
        previous = values[0]
        for index, value in enumerate(values):
            previous = value if index == 0 else value * multiplier + previous * (1 - multiplier)
            output.append(previous)
        return output

    def build_features_for_rows(self, rows, market_rows=None, sector_strength=None):
        if len(rows) < 120:
            return []
        market_rows = market_rows or self.load_market_rows()
        market_context = MarketContext(market_rows)
        sector_strength = sector_strength or {}
        closes = [row["close"] for row in rows]
        highs = [row["high"] for row in rows]
        lows = [row["low"] for row in rows]
        volumes = [row["volume"] for row in rows]
        ema12 = self.ema_series(closes, 12)
        ema26 = self.ema_series(closes, 26)
        dif = [ema12[i] - ema26[i] for i in range(len(rows))]
        dea = self.ema_series(dif, 9)
        output = []

        for index in range(80, len(rows)):
            ma20 = self.sma(closes, 20, index)
            ma60 = self.sma(closes, 60, index)
            if ma20 is None or ma60 is None:
                continue
            rsi = self.rsi(closes, index)
            atr = self.atr(highs, lows, closes, index)
            macd = (dif[index] - dea[index]) * 2
            # 均量分母不含當日本身，跟 short_term_target() 的量縮判斷
            # (range(max(0, j-20), j))用同一套「排除當日」定義，避免爆量
            # 突破日的巨量把自己那根K的20日均量墊高、稀釋 volume_ratio。
            avg_volume = self.sma(volumes, 20, index - 1) or volumes[index]
            boll_values = closes[index - 19:index + 1]
            boll_mid = statistics.mean(boll_values)
            boll_std = statistics.pstdev(boll_values) or 1
            boll_lower = boll_mid - 2 * boll_std
            boll_upper = boll_mid + 2 * boll_std
            row = rows[index]
            base5 = rows[index - 5]
            base20 = rows[index - 20]
            base60 = rows[index - 60]
            if min(row["close"], base5["close"], base20["close"], base60["close"]) <= 0:
                continue
            chip_values = [
                row.get("foreign_buy_sell"),
                row.get("trust_buy_sell"),
                row.get("margin_balance"),
                row.get("short_balance"),
            ]
            chip_data_coverage = sum(1 for value in chip_values if value is not None) / len(chip_values)
            margin_balance = row.get("margin_balance")
            short_balance = row.get("short_balance")
            day_trade_ratio = row.get("day_trade_ratio")
            lending_volume = row.get("securities_lending_volume")
            lending_fee_rate = row.get("securities_lending_fee_rate")
            def official_flow_value(value_key, source_key):
                return row.get(value_key) if is_official_source(row.get(source_key)) else None

            broker_branch_net_buy = official_flow_value("broker_branch_net_buy", "branch_flow_source")
            main_force_buy_sell = official_flow_value("main_force_buy_sell", "branch_flow_source")
            realtime_money_flow = official_flow_value("realtime_money_flow", "realtime_flow_source")
            realtime_large_order_flow = official_flow_value("realtime_large_order_flow", "realtime_flow_source")
            revenue_growth = row.get("revenue_growth")
            gross_margin = row.get("gross_margin")
            operating_margin = row.get("operating_margin")
            roe = row.get("roe")
            debt_ratio = row.get("debt_ratio")
            operating_cashflow_ratio = row.get("operating_cashflow_ratio")
            per = row.get("per")
            pbr = row.get("pbr")
            dividend_yield = row.get("dividend_yield")
            finance_values = [
                revenue_growth,
                gross_margin,
                operating_margin,
                roe,
                debt_ratio,
                operating_cashflow_ratio,
                per,
                pbr,
            ]
            finance_data_coverage = sum(1 for value in finance_values if value is not None) / len(finance_values)
            advanced_flow_values = [
                broker_branch_net_buy,
                main_force_buy_sell,
                realtime_money_flow,
                realtime_large_order_flow,
            ]
            advanced_flow_coverage = sum(1 for value in advanced_flow_values if value is not None) / len(advanced_flow_values)

            margin_lots = (margin_balance or 0) / 1000
            short_lots = (short_balance or 0) / 1000
            chip_score = math.tanh(((row.get("foreign_buy_sell") or 0) + (row.get("trust_buy_sell") or 0) - margin_lots * 0.4 + short_lots * 0.25) / 6500)
            # 法人連續買賣天數（回看最多 10 根，tanh 正規化）
            def _consec_streak(field, idx):
                v0 = rows[idx].get(field)
                if v0 is None:
                    return 0.0
                d = 1 if v0 > 0 else -1
                count = 1
                for k in range(idx - 1, max(idx - 10, -1), -1):
                    vk = rows[k].get(field)
                    if vk is None:
                        break
                    if (d > 0 and vk > 0) or (d < 0 and vk < 0):
                        count += 1
                    else:
                        break
                return math.tanh(d * count / 5.0)
            foreign_consec_buy = _consec_streak("foreign_buy_sell", index)
            trust_consec_buy = _consec_streak("trust_buy_sell", index)
            # 外資持股週環比：本週(近5日)外資買賣 vs 上週(前5-9日)，tanh 正規化
            def _week_sum(field, idx, start, end):
                vals = [rows[k].get(field) for k in range(max(idx - end, 0), max(idx - start + 1, 0))]
                vals = [v for v in vals if v is not None]
                return sum(vals) if vals else None
            fw_cur = _week_sum("foreign_buy_sell", index, 0, 4)
            fw_prev = _week_sum("foreign_buy_sell", index, 5, 9)
            if fw_cur is not None and fw_prev is not None:
                foreign_weekly_chg = math.tanh((fw_cur - fw_prev) / 10000)
            elif fw_cur is not None:
                foreign_weekly_chg = math.tanh(fw_cur / 10000)
            else:
                foreign_weekly_chg = 0.0
            # 週線 RSI（近 15 週收盤計算，每週取最後一個交易日）
            def _weekly_rsi(idx, n=15):
                weekly = []
                seen = set()
                for k in range(idx, max(idx - n * 8, -1), -1):
                    try:
                        d = dt.datetime.strptime(rows[k]["date"], "%Y-%m-%d")
                        wk = (d.year, d.isocalendar()[1])
                    except Exception:
                        continue
                    if wk not in seen:
                        seen.add(wk)
                        weekly.append(float(rows[k]["close"]))
                    if len(weekly) >= n:
                        break
                weekly = list(reversed(weekly))
                if len(weekly) < 3:
                    return 50.0
                period = min(14, len(weekly) - 1)
                gains, losses = [], []
                for i in range(1, len(weekly)):
                    delta = weekly[i] - weekly[i - 1]
                    gains.append(max(delta, 0.0))
                    losses.append(max(-delta, 0.0))
                avg_g = sum(gains[:period]) / period
                avg_l = sum(losses[:period]) / period
                for i in range(period, len(gains)):
                    avg_g = (avg_g * (period - 1) + gains[i]) / period
                    avg_l = (avg_l * (period - 1) + losses[i]) / period
                return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
            weekly_rsi = _weekly_rsi(index) / 100
            # 月線趨勢：MA60 vs MA120（大趨勢多空方向）
            ma120 = self.sma(closes, 120, index)
            monthly_ma_trend = (ma60 - ma120) / row["close"] if ma120 else 0.0
            # 大戶/散戶資料已移除收集，只留主力分點+分點籌碼算這個分數；
            # 保留 advanced_flow_score 這個特徵位置本身，避免改動 FEATURE_NAMES
            # 長度讓既有 model.pkl 的 feature_names 比對失效、強制重訓。
            advanced_flow_score = math.tanh((
                (broker_branch_net_buy or 0)
                + (main_force_buy_sell or 0)
            ) / 6500)
            traded_amount = max((row["close"] or 0) * (row["volume"] or 0), 1)
            realtime_money_flow_score = math.tanh(
                ((realtime_money_flow or 0) / traded_amount) * 20
                + ((realtime_large_order_flow or 0) / max(row["volume"] or 1, 1)) * 2
            )
            valuation_score = (
                math.tanh(((35 - per) / 20) + ((3 - pbr) / 3) + ((dividend_yield or 0) / 8))
                if per and pbr and per > 0 and pbr > 0
                else 0
            )
            daytrade_risk = math.tanh((day_trade_ratio - 0.18) * 5) if day_trade_ratio is not None else 0.0
            # #164新特徵1：融資餘額日增減流量——妖股末段常見融資快速堆高
            # (散戶槓桿追入)，用「日增減張數相對均量」衡量流入強度。往回找
            # 最多5根有融資資料的K(籌碼資料偶有缺日)，缺資料時中性0。
            margin_delta_flow = 0.0
            if margin_balance is not None:
                margin_prev = None
                for k in range(index - 1, max(index - 6, -1), -1):
                    candidate = rows[k].get("margin_balance")
                    if candidate is not None:
                        margin_prev = candidate
                        break
                if margin_prev is not None:
                    # 2026-07-04 回測實驗：margin_balance 單位已是「張」，原本再除1000
                    # 變千張，讓「日增減張數 / 均量(張)」這個比值小1000倍近失效。這裡改成
                    # 用張直接算比值(量綱正確：張/張)，係數*3 維持不變(實測2332日增500張
                    # /均量3萬張≈0.017*3≈0.05，tanh 不飽和)。
                    delta_lots = (margin_balance - margin_prev)
                    margin_delta_flow = math.tanh((delta_lots / max(avg_volume / 1000, 1)) * 3)
            # #164新特徵2：當沖買賣失衡——(當沖買-當沖賣)/(買+賣)，資料層
            # 已算好且天然落在[-1,1]，正值代表當沖客偏多向追price。缺資料中性0。
            daytrade_imbalance_raw = row.get("day_trade_buy_sell_imbalance")
            daytrade_imbalance = clamp(float(daytrade_imbalance_raw), -1.0, 1.0) if daytrade_imbalance_raw is not None else 0.0
            lending_risk = math.tanh((lending_volume / max(avg_volume, 1)) * 8 + ((lending_fee_rate or 1.2) - 1.2) / 2) if lending_volume is not None else 0.0
            # 2026-07-04 回測實驗：short_pressure 是「融資/融券餘額 相對 均量」的壓力比值，
            # 但用了 margin_lots(千張)/short_lots(千張)，讓比值小1000倍近失效。改成用餘額
            # 張數直接算(量綱正確：張/張)，係數 0.12/0.08 維持(實測2332融資2.3萬張/均量3萬張
            # ≈0.78*0.12≈0.094，tanh 不飽和)。chip_score 仍用 margin_lots/short_lots 不動。
            short_pressure = math.tanh(
                ((margin_balance or 0) / max(avg_volume / 1000, 1)) * 0.12
                - ((short_balance or 0) / max(avg_volume / 1000, 1)) * 0.08
            )
            revenue_momentum = math.tanh((revenue_growth - 5) / 28) if revenue_growth is not None else 0.0
            profitability_parts = []
            if gross_margin is not None:
                profitability_parts.append((gross_margin - 20) / 25)
            if operating_margin is not None:
                profitability_parts.append((operating_margin - 8) / 18)
            if roe is not None:
                profitability_parts.append((roe - 3) / 12)
            if debt_ratio is not None:
                profitability_parts.append((55 - debt_ratio) / 35)
            if operating_cashflow_ratio is not None:
                profitability_parts.append((operating_cashflow_ratio - 5) / 25)
            fundamental_score = math.tanh(sum(profitability_parts) / len(profitability_parts)) if profitability_parts else 0.0
            finance_score = math.tanh((revenue_momentum * 0.45) + (fundamental_score * 0.45) + (valuation_score * 0.2))
            stock_ret20 = (row["close"] - base20["close"]) / base20["close"]
            market = self.market_features(row["date"], market_context, stock_ret20)
            sector_relative = sector_strength.get((row["symbol"], row["date"]), market["stock_vs_taiex_20"])
            values = [
                (ma20 - ma60) / row["close"],
                rsi / 100,
                macd / row["close"],
                atr / row["close"],
                (row["close"] - base5["close"]) / base5["close"],
                stock_ret20,
                (row["close"] - base60["close"]) / base60["close"],
                row["volume"] / max(avg_volume, 1),
                (row["close"] - boll_lower) / max(boll_upper - boll_lower, 0.01),
                chip_score,
                finance_score,
                market["taiex_ret_20"],
                market["taiex_ma_gap"],
                market["otc_ret_20"],
                market["nasdaq_ret_1"],
                market["sp500_ret_1"],
                market["usdtwd_ret_20"],
                market["market_regime"],
                market["stock_vs_taiex_20"],
                sector_relative,
                valuation_score,
                daytrade_risk,
                lending_risk,
                short_pressure,
                revenue_momentum,
                fundamental_score,
                chip_data_coverage,
                finance_data_coverage,
                advanced_flow_score,
                realtime_money_flow_score,
                advanced_flow_coverage,
                foreign_consec_buy,
                trust_consec_buy,
                foreign_weekly_chg,
                weekly_rsi,
                monthly_ma_trend,
                margin_delta_flow,
                daytrade_imbalance,
            ]
            if all(math.isfinite(value) for value in values):
                output.append({
                    "date": row["date"],
                    "close": row["close"],
                    "x": values,
                    "index": index,
                    "market": market,
                })
        return output

    def market_features(self, date, market_context, stock_ret20):
        taiex_ret20 = market_context.ret("TAIEX", date, 20)
        taiex_ma_gap = market_context.ma_gap("TAIEX", date, 20)
        otc_ret20 = market_context.ret("OTC", date, 20)
        nasdaq_ret1 = market_context.ret("NASDAQ", date, 1)
        sp500_ret1 = market_context.ret("SP500", date, 1)
        usdtwd_ret20 = market_context.ret("USDTWD", date, 20)
        if taiex_ma_gap > 0.02 and taiex_ret20 > 0.02:
            regime = 1.0
        elif taiex_ma_gap < -0.02 and taiex_ret20 < -0.02:
            regime = -1.0
        else:
            regime = 0.0
        return {
            "taiex_ret_20": taiex_ret20,
            "taiex_ma_gap": taiex_ma_gap,
            "otc_ret_20": otc_ret20,
            "nasdaq_ret_1": nasdaq_ret1,
            "sp500_ret_1": sp500_ret1,
            "usdtwd_ret_20": usdtwd_ret20,
            "market_regime": regime,
            "stock_vs_taiex_20": stock_ret20 - taiex_ret20,
        }

    def rsi(self, closes, index, period=14):
        if index < period:
            return 50
        gains = 0
        losses = 0
        for i in range(index - period + 1, index + 1):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains += diff
            else:
                losses -= diff
        return 100 - 100 / (1 + gains / max(losses, 0.01))

    def atr(self, highs, lows, closes, index, period=14):
        if index < 1:
            return highs[index] - lows[index]
        start = max(1, index - period + 1)
        ranges = []
        for i in range(start, index + 1):
            ranges.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        return sum(ranges) / len(ranges)

    def short_term_target(self, rows, index):
        """
        獨立 AI 的短期淨獲利目標。妖股「10 日內 +10%」仍由雷達候選帳本
        追蹤，但不再是模型正例的定義。

        進場採訊號日次日開盤，買進端加入 0.1% 滑價及未折扣手續費；分別在
        第 3、5、10 個交易日收盤估算可實現淨報酬，賣出端扣手續費與證交稅。
        三個週期依 20%/35%/45% 加權，再扣持有期間最大不利幅度的 20%，避免
        只因終點反彈就把中途風險很大的路徑標成好交易。

        y=1 的語意是「多週期加權淨報酬在風險懲罰後仍大於 0」。分類模型學
        短期淨獲利機率；learning-to-rank 學風險調整後預期淨報酬；額外的
        3/5/10 日回歸器各自保留週期差異。
        """
        if index + SHORT_PROFIT_MAX_HORIZON_DAYS >= len(rows):
            return None

        next_row = rows[index + 1]
        entry_open = float(next_row.get("open") or next_row["close"])
        if entry_open <= 0:
            return None

        entry_fill = entry_open * (1 + PAPER_BASE_SLIPPAGE_RATE)
        entry_cost = entry_fill * (1 + PAPER_BUY_COMMISSION_RATE)
        horizon_returns = {}
        for horizon in SHORT_PROFIT_HORIZONS:
            exit_close = float(rows[index + horizon].get("close") or 0)
            if exit_close <= 0:
                return None
            proceeds = exit_close * (1 - PAPER_SELL_COMMISSION_RATE - PAPER_SELL_TAX_RATE)
            horizon_returns[horizon] = proceeds / entry_cost - 1

        path_lows = []
        for row in rows[index + 1:index + SHORT_PROFIT_MAX_HORIZON_DAYS + 1]:
            low = float(row.get("low") or row.get("close") or 0)
            if low > 0:
                path_lows.append(low)
        max_adverse_return = (
            min(path_lows) / entry_fill - 1 if path_lows else 0.0
        )
        weighted_net_return = sum(
            horizon_returns[horizon] * SHORT_PROFIT_HORIZON_WEIGHTS[horizon]
            for horizon in SHORT_PROFIT_HORIZONS
        )
        downside_magnitude = max(0.0, -max_adverse_return)
        risk_adjusted_return = (
            weighted_net_return
            - SHORT_PROFIT_DOWNSIDE_PENALTY * downside_magnitude
        )
        is_positive = risk_adjusted_return > 0
        target_strength = (
            clamp(risk_adjusted_return / 0.01, 0.0, 5.0)
            if is_positive else 0.0
        )

        return {
            "y":             1 if is_positive else 0,
            "future_return": weighted_net_return,
            "expected_return": risk_adjusted_return,
            "exit_price": float(rows[index + SHORT_PROFIT_MAX_HORIZON_DAYS]["close"]),
            "hold_days": SHORT_PROFIT_MAX_HORIZON_DAYS,
            "exit_reason": (
                "short_profit_net_positive" if is_positive
                else "short_profit_net_negative"
            ),
            "net_return": weighted_net_return,
            "risk_adjusted_return": risk_adjusted_return,
            "max_adverse_return": max_adverse_return,
            "horizon_net_returns": horizon_returns,
            **{
                f"net_return_{horizon}d": horizon_returns[horizon]
                for horizon in SHORT_PROFIT_HORIZONS
            },
            "target_strength": target_strength,
        }

    def build_training_samples(self, symbols=None):
        # 目標由 short_term_target() 的 SHORT_PROFIT_POLICY 唯一決定；妖股
        # +10% 常數只供雷達候選追蹤，不得再混入獨立 AI 標籤。
        symbols = symbols or DEFAULT_SYMBOLS
        total = len(symbols)
        samples = []
        latest = {}
        started_at = now_text()

        def _write_progress(phase, done):
            """最盡力寫進度，失敗不中斷訓練。"""
            try:
                with self.connect() as conn:
                    self.set_meta(conn, "training_progress", json.dumps({
                        "phase": phase, "done": done, "total": total,
                        "status": "running", "startedAt": started_at,
                    }))
            except Exception:
                pass

        _write_progress("sector_strength", 0)
        market_rows = self.load_market_rows()
        # 訓練端的大盤資料品質護欄，對齊預測端 market_data_quality 的「大聲失敗」
        # 哲學：MarketContext 對覆蓋範圍以前的日期會靜默回傳 0.0，這些樣本的
        # 大盤特徵(taiex_ret_20/ma_gap等)全是假中性值，餵進訓練會無聲污染模型。
        # 界線=TAIEX 第21筆資料的日期(ret/ma_gap 需要20天lookback)；目前資料庫
        # 的大盤與個股覆蓋對齊、特徵又從第80根K才開始建，這條護欄今天一筆都
        # 不會攔到——它防的是未來大盤資料表被重建/清空/縮短覆蓋時的靜默污染。
        taiex_dates = [row["date"] for row in (market_rows.get("TAIEX") or [])]
        market_ready_date = taiex_dates[20] if len(taiex_dates) > 20 else None
        samples_skipped_no_market = 0
        # repair=False：主迴圈裡的 ensure_model_ready_rows(repair=True) 本來就會
        # 補抓品質不足的股票，這裡再開 repair 等於品質差的股票每天被網路補抓
        # 兩次(補不好的股票更是天天重複)，訓練池擴大到數百檔後會顯著拖慢訓練
        # 又浪費 FinMind 額度。類股強度少算到「今天才剛補好資料」的個別股票，
        # 對跨多檔平均的類股分數影響極小，隔天自然歸隊。
        sector_strength = self.build_sector_strength(symbols, repair=False)
        sample_errors = []
        for i, symbol in enumerate(symbols):
            if i % 20 == 0:
                _write_progress("building_samples", i)
            # 逐股隔離：單一股票的病態資料讓特徵計算炸出例外時，跳過該檔
            # 繼續，不能讓一檔壞資料毀掉整批訓練樣本(訓練動輒十幾分鐘)。
            try:
                rows, quality = self.ensure_model_ready_rows(symbol, repair=True)
                if not quality["ok"]:
                    latest[symbol] = None
                    continue
                features = self.build_features_for_rows(rows, market_rows, sector_strength)
                latest[symbol] = features[-1] if features else None
                for item in features:
                    if market_ready_date is None or item["date"] < market_ready_date:
                        samples_skipped_no_market += 1
                        continue
                    target = self.short_term_target(rows, item["index"])
                    if not target:
                        continue
                    samples.append({
                        "symbol": symbol,
                        "date": item["date"],
                        "x": item["x"],
                        **target,
                    })
            except Exception as exc:
                latest[symbol] = None
                sample_errors.append(f"{symbol}: {exc}")
        # 乾淨的一天也要寫(空字串/0)，不然前一天的紀錄會殘留在 meta 裡誤導排查。
        try:
            with self.connect() as conn:
                self.set_meta(conn, "last_training_sample_errors", " | ".join(sample_errors[:20]))
                self.set_meta(conn, "last_training_samples_skipped_no_market", str(samples_skipped_no_market))
        except Exception:
            pass
        if samples_skipped_no_market:
            print(f"training samples skipped due to missing market coverage: {samples_skipped_no_market}")
        samples.sort(key=lambda row: row["date"])
        return samples, latest

    def build_sector_strength(self, symbols, repair=False):
        stock_info = self.load_stock_info()
        returns = {}
        sectors = {}
        for symbol in symbols:
            rows, quality = self.ensure_model_ready_rows(symbol, repair=repair)
            if not quality["ok"]:
                continue
            sectors[symbol] = stock_info.get(symbol, {}).get("sector") or "台股"
            for index in range(20, len(rows)):
                base = rows[index - 20]["close"]
                if base:
                    returns[(symbol, rows[index]["date"])] = (rows[index]["close"] - base) / base
        by_sector_date = {}
        for (symbol, date), value in returns.items():
            by_sector_date.setdefault((sectors.get(symbol, "台股"), date), []).append(value)
        sector_average = {
            key: sum(values) / len(values)
            for key, values in by_sector_date.items()
            if values
        }
        output = {}
        for (symbol, date), value in returns.items():
            average = sector_average.get((sectors.get(symbol, "台股"), date))
            output[(symbol, date)] = value - average if average is not None else 0.0
        return output

    def read_model_gate_state(self):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = 'model_gate_state'").fetchone()
        data = {}
        if row:
            try:
                data = json.loads(row[0] or "{}")
            except (json.JSONDecodeError, TypeError):
                data = {}
        return {
            "consecutiveRejects": int(data.get("consecutiveRejects") or 0),
            "lastRejectedAt": str(data.get("lastRejectedAt") or ""),
            "lastRejectReason": str(data.get("lastRejectReason") or ""),
            "lastAcceptedAt": str(data.get("lastAcceptedAt") or ""),
            # 2026-07-04 新增：目前生效的model.pkl是被「連續拒絕N次後強制放行」
            # 上線的，還是真的通過品質檢查——事後查model_gate_state才看得出來，
            # 不然沒辦法分辨現在這個模型的可信度。
            "lastAcceptedWasForced": bool(data.get("lastAcceptedWasForced") or False),
        }

    def train_model(self, symbols=None):
        symbols = symbols or DEFAULT_SYMBOLS
        samples, latest = self.build_training_samples(symbols)
        if len(samples) < 80:
            try:
                with self.connect() as conn:
                    self.set_meta(conn, "training_progress", json.dumps({
                        "phase": "failed", "status": "error",
                        "reason": f"樣本不足：{len(samples)} 筆",
                    }))
            except Exception:
                pass
            raise RuntimeError("Not enough samples to train model")
        # 進入梯度下降階段
        try:
            with self.connect() as conn:
                self.set_meta(conn, "training_progress", json.dumps({
                    "phase": "fitting", "done": len(symbols), "total": len(symbols),
                    "samples": len(samples), "status": "running",
                }))
        except Exception:
            pass
        split = max(40, int(len(samples) * 0.8))
        train = samples[:split]
        validation = samples[split:]
        feature_count = len(FEATURE_NAMES)
        means = [sum(row["x"][i] for row in train) / len(train) for i in range(feature_count)]
        stdevs = []
        for i in range(feature_count):
            variance = sum((row["x"][i] - means[i]) ** 2 for row in train) / len(train)
            stdevs.append(math.sqrt(variance) or 1)

        def normalize(values):
            return [(values[i] - means[i]) / stdevs[i] for i in range(feature_count)]

        weights = [0.0] * (feature_count + 1)
        learning_rate = 0.08
        l2 = 0.001
        train_x = [normalize(row["x"]) for row in train]
        positive_count = sum(row["y"] for row in train)
        negative_count = max(len(train) - positive_count, 1)
        positive_weight = clamp(negative_count / max(positive_count, 1), 1.0, 6.0)
        if np is not None:
            train_matrix = np.asarray(train_x, dtype=float)
            train_labels = np.asarray([row["y"] for row in train], dtype=float)
            logistic_sample_weights = np.asarray([
                (positive_weight if row["y"] else 1.0)
                * (1 + min(float(row.get("target_strength") or 0), 5) * 0.15)
                for row in train
            ], dtype=float)
            np_weights = np.zeros(feature_count + 1, dtype=float)
            for _ in range(260):
                logits = np_weights[0] + train_matrix @ np_weights[1:]
                probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -35, 35)))
                errors = (probabilities - train_labels) * logistic_sample_weights
                bias_gradient = float(np.mean(errors))
                feature_gradient = (
                    train_matrix.T @ errors / len(train)
                    + l2 * np_weights[1:]
                )
                np_weights[0] -= learning_rate * bias_gradient
                np_weights[1:] -= learning_rate * feature_gradient
            weights = np_weights.tolist()
        else:
            for _ in range(260):
                gradient = [0.0] * (feature_count + 1)
                for x, row in zip(train_x, train):
                    pred = sigmoid(weights[0] + sum(
                        x[i] * weights[i + 1] for i in range(feature_count)
                    ))
                    sample_weight = (
                        (positive_weight if row["y"] else 1.0)
                        * (1 + min(float(row.get("target_strength") or 0), 5) * 0.15)
                    )
                    error = (pred - row["y"]) * sample_weight
                    gradient[0] += error
                    for i in range(feature_count):
                        gradient[i + 1] += error * x[i] + l2 * weights[i + 1]
                for i in range(len(weights)):
                    weights[i] -= learning_rate * gradient[i] / len(train)

        predictions = []
        for row in validation:
            x = normalize(row["x"])
            probability = sigmoid(weights[0] + sum(x[i] * weights[i + 1] for i in range(feature_count)))
            predictions.append({**row, "probability": probability})
        # 驗證指標要用「跟上線判斷買訊一致」的門檻，不能用 score_metrics 寫死的
        # 預設 0.5——0.5 太鬆，會讓回測「交易」筆數遠超過上線實際會喊買的頻率，
        # 連帶讓 profitFactor/maxDrawdown 等指標失真(尤其是不分倉位的權益曲線
        # 模擬對交易筆數特別敏感)。所以先算 threshold，再餵給所有 score_metrics。
        threshold = self.buy_signal_threshold()
        metrics = self.score_metrics(predictions, threshold=threshold)
        extra_models = self.train_extra_models(train, validation, normalize, positive_weight, threshold=threshold)
        training_data_dates = [
            str(item.get("date") or "")[:10]
            for item in latest.values()
            if isinstance(item, dict) and item.get("date")
        ]
        training_sample_dates = [
            str(item.get("date") or "")[:10] for item in samples if item.get("date")
        ]
        policy_spec = short_profit_policy_spec(symbols, threshold)
        model = {
            "version": now_text().replace(" ", "T"),
            "model_type": "ensemble-logistic-xgboost-lightgbm-isolation-rank",
            "target_type": SHORT_PROFIT_TARGET_TYPE,
            "policy_hash": short_profit_policy_hash(symbols, threshold),
            "target_policy_hash": SHORT_PROFIT_POLICY_HASH,
            "policy_spec": policy_spec,
            "target_spec": {
                **SHORT_PROFIT_POLICY,
                "entry": "next-day open + slippage 0.1% + commission",
                "fees": "commission 0.1425% x2 + tax 0.3% included in every horizon return",
                "rankingTarget": "weighted 3/5/10-day net return minus max-adverse-excursion penalty",
                "monsterSetupRole": "candidate feature only; not required for a positive label",
            },
            "data_policy": DATA_POLICY_VERSION,
            "symbols": symbols,
            "feature_names": FEATURE_NAMES,
            "weights": weights,
            "means": means,
            "stdevs": stdevs,
            "threshold": threshold,
            "metrics": metrics,
            "extra_models": extra_models,
            "samples": len(samples),
            "validation_size": len(validation),
            "positive_weight": positive_weight,
            "trained_at": now_text(),
            "training_data_max_date": max(training_data_dates) if training_data_dates else None,
            "training_sample_max_date": max(training_sample_dates) if training_sample_dates else None,
        }
        # 品質閘門：覆蓋 model.pkl 前跟舊模型比對，太差就沿用舊版不上線。
        # 舊檔讀不到(不存在/損毀)就沒有比較基準，新模型直接上線。
        old_model = None
        try:
            if self.model_path.exists():
                with open(self.model_path, "rb") as fh:
                    old_model = pickle.load(fh)
        except Exception:
            old_model = None
        gate_state = self.read_model_gate_state()
        comparison_model = old_model
        target_changed = bool(
            old_model
            and old_model.get("target_type") != model.get("target_type")
        )
        if target_changed:
            # 不同標籤的 AUC 與樣本數不可直接比較；保留絕對 AUC 下限，但清除
            # 舊目標的相對基準，避免拿 +10% 分類器與淨獲利分類器硬比。
            comparison_model = {
                "feature_names": list(old_model.get("feature_names") or []),
                "metrics": {},
                "samples": 0,
            }
        gate_accept, gate_reason, gate_forced = evaluate_model_gate(
            comparison_model, metrics, len(samples), FEATURE_NAMES,
            gate_state["consecutiveRejects"],
        )
        if gate_accept and target_changed:
            gate_reason = f"目標改為 {SHORT_PROFIT_TARGET_TYPE}；通過新目標絕對品質閘門"
        if not gate_accept:
            with self.connect() as conn:
                self.set_meta(conn, "model_gate_state", json.dumps({
                    "consecutiveRejects": gate_state["consecutiveRejects"] + 1,
                    "lastRejectedAt": now_text(),
                    "lastRejectReason": gate_reason,
                    "lastAcceptedAt": gate_state["lastAcceptedAt"],
                    "lastAcceptedWasForced": gate_state["lastAcceptedWasForced"],
                    "rejectedMetrics": {
                        "auc": round(metrics.get("auc") or 0, 4),
                        "samples": len(samples),
                    },
                }, ensure_ascii=False))
                # training_progress 標記完成但註明被閘門擋下，UI 不會卡在 fitting
                self.set_meta(conn, "training_progress", json.dumps({
                    "phase": "complete", "done": len(symbols), "total": len(symbols),
                    "samples": len(samples), "status": "complete",
                    "trainedAt": model["trained_at"],
                    "trainingDataMaxDate": model.get("training_data_max_date"),
                    "trainingSampleMaxDate": model.get("training_sample_max_date"),
                    "gateRejected": True, "gateReason": gate_reason,
                    "metrics": {
                        "accuracy": round(metrics.get("accuracy") or 0, 4),
                        "precision": round(metrics.get("precision") or 0, 4),
                        "auc": round(metrics.get("auc") or 0, 4),
                    },
                }))
            print(f"model gate rejected new model: {gate_reason}")
            return {**model, "gateRejected": True, "gateReason": gate_reason}
        # 2026-07-04 稽核修復：write_model_env 只依賴傳入的 model dict(不讀
        # self.model_path)，本來就能安全搬到 os.replace 之前——先做完所有
        # 「失敗了也不影響現況」的準備工作，最後才做決定性的 model.pkl 替換，
        # 這樣如果 write_model_env 意外拋例外，model.pkl 還沒被換掉、下面的
        # model_gate_state 重置也還沒執行，train_model() 整個乾淨地往上拋出
        # 例外，不會出現「model.pkl已經是新模型，但model_gate_state還停在
        # 舊的consecutiveRejects」這種兩邊不同步的狀態。
        self.write_model_env(model)
        model_bytes = pickle.dumps(model)
        temp_path = self.model_path.with_name(f"{self.model_path.name}.tmp")
        temp_path.write_bytes(model_bytes)
        os.replace(temp_path, self.model_path)
        with self.connect() as conn:
            self.set_meta(conn, "model_gate_state", json.dumps({
                "consecutiveRejects": 0,
                "lastRejectedAt": gate_state["lastRejectedAt"],
                "lastRejectReason": gate_state["lastRejectReason"],
                "lastAcceptedAt": now_text(),
                "lastAcceptedWasForced": gate_forced,
            }, ensure_ascii=False))
            self.set_meta(conn, "last_model_train", model["trained_at"])
            self.set_meta(conn, "model_version", model["version"])
            self.set_meta(conn, "last_model_training_data_max_date", model.get("training_data_max_date") or "")
            self.set_meta(conn, "last_model_training_sample_max_date", model.get("training_sample_max_date") or "")
            self.set_meta(conn, "signal_threshold", str(threshold))
            self.set_meta(conn, "last_sponsor_feature_update", model["trained_at"])
            self.set_meta(conn, "last_sponsor_feature_names", ",".join(FEATURE_NAMES[-7:]))
            self.set_meta(conn, "training_progress", json.dumps({
                "phase": "complete", "done": len(symbols), "total": len(symbols),
                "samples": len(samples), "status": "complete",
                "trainedAt": model["trained_at"],
                "trainingDataMaxDate": model.get("training_data_max_date"),
                "trainingSampleMaxDate": model.get("training_sample_max_date"),
                "metrics": {
                    "accuracy": round(metrics.get("accuracy") or 0, 4),
                    "precision": round(metrics.get("precision") or 0, 4),
                    "auc": round(metrics.get("auc") or 0, 4),
                },
            }))
        return model

    def train_extra_models(self, train, validation, normalize, positive_weight=1.0, threshold=0.5):
        if np is None:
            return {"available": False, "reason": "scikit-learn/numpy unavailable", "models": []}
        train_x = np.array([normalize(row["x"]) for row in train], dtype=float)
        train_y = np.array([row["y"] for row in train], dtype=int)
        train_returns = np.array([row.get("expected_return", row["future_return"]) for row in train], dtype=float)
        sample_weights = np.array([
            (positive_weight if row["y"] else 1.0) * (1 + min(float(row.get("target_strength") or 0), 5) * 0.15)
            for row in train
        ], dtype=float)
        validation_x = np.array([normalize(row["x"]) for row in validation], dtype=float)
        output = {"available": True, "models": []}

        if XGBClassifier is not None:
            try:
                xgboost = XGBClassifier(
                    n_estimators=180,
                    max_depth=3,
                    learning_rate=0.045,
                    subsample=0.85,
                    colsample_bytree=0.85,
                    objective="binary:logistic",
                    eval_metric="logloss",
                    random_state=42,
                    n_jobs=1,
                    verbosity=0,
                    # 類別不平衡加權已在 sample_weights 乘過一次，這裡不再設
                    # scale_pos_weight，否則正例有效權重是 positive_weight 平方
                    # （最高 36 倍），輸出機率會系統性偏高、與 logistic 基準不一致。
                )
                xgboost.fit(train_x, train_y, sample_weight=sample_weights)
                probabilities = xgboost.predict_proba(validation_x)[:, 1].tolist()
                predictions = [{**row, "probability": float(prob)} for row, prob in zip(validation, probabilities)]
                output["xgboost"] = {
                    "name": "XGBoost",
                    "estimator": xgboost,
                    # positive_weight 加權(最高6倍)讓分類模型的機率輸出系統性
                    # 偏高、未校準(precision偏低的成因之一)。比照 learning_to_rank
                    # 的 calibration 欄位，存一份驗證集輸出分佈當百分位校準參考
                    # ——用驗證集而非訓練集：訓練集輸出有過擬合的極端化傾向，
                    # 驗證集分佈才是部署時真實分數分佈的誠實估計。校準版與
                    # 原始版並存(extra_model_probabilities 會輸出 *_calibrated
                    # key)，buy_signal_score 目前仍用原始版——換用校準版之前
                    # 必須重跑 backtest_ensemble_weights.py 全部配置驗證。
                    "calibration": sorted(float(p) for p in probabilities),
                    "metrics": self.score_metrics(predictions, threshold=threshold),
                    "note": "正式 XGBoost 分類模型",
                }
                output["models"].append(output["xgboost"]["name"])
            except Exception as exc:
                output["xgboost_error"] = str(exc)

        if LGBMClassifier is not None:
            try:
                lightgbm = LGBMClassifier(
                    n_estimators=220,
                    learning_rate=0.04,
                    num_leaves=15,
                    max_depth=4,
                    subsample=0.85,
                    colsample_bytree=0.85,
                    reg_lambda=0.2,
                    # 同 XGBoost：不設 class_weight，避免與 sample_weights 相乘
                    # 造成正例權重重複計算。
                    random_state=42,
                    verbose=-1,
                )
                lightgbm.fit(train_x, train_y, sample_weight=sample_weights)
                probabilities = lightgbm.predict_proba(validation_x)[:, 1].tolist()
                predictions = [{**row, "probability": float(prob)} for row, prob in zip(validation, probabilities)]
                output["lightgbm"] = {
                    "name": "LightGBM",
                    "estimator": lightgbm,
                    # 同 xgboost：驗證集輸出分佈當百分位校準參考，校準版與原始版並存。
                    "calibration": sorted(float(p) for p in probabilities),
                    "metrics": self.score_metrics(predictions, threshold=threshold),
                    "note": "正式 LightGBM 分類模型",
                }
                output["models"].append(output["lightgbm"]["name"])
            except Exception as exc:
                output["lightgbm_error"] = str(exc)

        if HistGradientBoostingClassifier is not None:
            try:
                gradient = HistGradientBoostingClassifier(
                    max_iter=160,
                    learning_rate=0.055,
                    max_leaf_nodes=15,
                    l2_regularization=0.02,
                    random_state=42,
                )
                gradient.fit(train_x, train_y, sample_weight=sample_weights)
                probabilities = gradient.predict_proba(validation_x)[:, 1].tolist()
                predictions = [{**row, "probability": float(prob)} for row, prob in zip(validation, probabilities)]
                output["gradient_boosting"] = {
                    "name": "Gradient Boosting（LightGBM 類型）",
                    "estimator": gradient,
                    # 同 xgboost：驗證集輸出分佈當百分位校準參考，校準版與原始版並存。
                    "calibration": sorted(float(p) for p in probabilities),
                    "metrics": self.score_metrics(predictions, threshold=threshold),
                    "note": "XGBoost/LightGBM 不可用時使用 sklearn HistGradientBoostingClassifier",
                }
                output["models"].append(output["gradient_boosting"]["name"])
            except Exception as exc:
                output["gradient_boosting_error"] = str(exc)

        if IsolationForest is not None:
            try:
                positive_x = train_x[train_y == 1]
                isolation_train_x = positive_x if len(positive_x) >= 40 else train_x
                isolation = IsolationForest(n_estimators=160, contamination=0.08, random_state=42)
                isolation.fit(isolation_train_x)
                train_scores = isolation.score_samples(train_x)
                val_scores = isolation.score_samples(validation_x)
                # 用 1st/99th 而非 5th/95th：範圍太窄會讓大量正常樣本被地板/天花板
                # 效應壓到同一個 clamp 邊界值，異常分數失去鑑別度。
                lo = float(np.percentile(train_scores, 1))
                hi = float(np.percentile(train_scores, 99))
                probabilities = [smooth_clamp_ratio((float(score) - lo) / max(hi - lo, 1e-9)) for score in val_scores]
                predictions = [{**row, "probability": float(prob)} for row, prob in zip(validation, probabilities)]
                output["isolation_forest"] = {
                    "name": "Isolation Forest 異常偵測",
                    "estimator": isolation,
                    "score_low": lo,
                    "score_high": hi,
                    "metrics": self.score_metrics(predictions, threshold=threshold),
                }
                output["models"].append(output["isolation_forest"]["name"])
            except Exception as exc:
                output["isolation_forest_error"] = str(exc)

        if GradientBoostingRegressor is not None or HistGradientBoostingRegressor is not None:
            try:
                if HistGradientBoostingRegressor is not None:
                    ranker = HistGradientBoostingRegressor(
                        max_iter=180,
                        learning_rate=0.045,
                        max_leaf_nodes=15,
                        l2_regularization=0.05,
                        random_state=42,
                    )
                else:
                    ranker = GradientBoostingRegressor(
                        n_estimators=180,
                        learning_rate=0.045,
                        max_depth=3,
                        random_state=42,
                    )
                ranker.fit(train_x, train_returns, sample_weight=sample_weights)
                train_pred = ranker.predict(train_x)
                val_pred = ranker.predict(validation_x)
                sorted_train = sorted(float(value) for value in train_pred)
                probabilities = [self.rank_probability(float(value), sorted_train) for value in val_pred]
                predictions = [{**row, "probability": float(prob)} for row, prob in zip(validation, probabilities)]
                # 用殘差中位數做穩健截距校正；平均殘差會被少數妖股極端報酬
                # 拉動，實測反而使各週期 MAE 變差。
                # 偏差截距只能從訓練資料估計；若拿 validation 殘差校正後又用
                # 同一批 validation 回報指標，會讓驗證結果帶入答案而過度樂觀。
                bias_correction = float(np.median(train_returns - train_pred))
                output["learning_to_rank"] = {
                    "name": "Learning to Rank 排名模型",
                    "estimator": ranker,
                    "calibration": sorted_train,
                    "bias_correction": bias_correction,
                    "metrics": self.score_metrics(predictions, threshold=threshold),
                }
                output["models"].append(output["learning_to_rank"]["name"])
            except Exception as exc:
                output["learning_to_rank_error"] = str(exc)

        if GradientBoostingRegressor is not None or HistGradientBoostingRegressor is not None:
            output["short_horizon_returns"] = {}
            for horizon in SHORT_PROFIT_HORIZONS:
                key = f"net_return_{horizon}d"
                try:
                    train_target = np.array(
                        [float(row[key]) for row in train], dtype=float,
                    )
                    validation_target = np.array(
                        [float(row[key]) for row in validation], dtype=float,
                    )
                    if HistGradientBoostingRegressor is not None:
                        regressor = HistGradientBoostingRegressor(
                            max_iter=160,
                            learning_rate=0.045,
                            max_leaf_nodes=15,
                            l2_regularization=0.05,
                            random_state=42 + horizon,
                        )
                    else:
                        regressor = GradientBoostingRegressor(
                            n_estimators=160,
                            learning_rate=0.045,
                            max_depth=3,
                            loss="huber",
                            random_state=42 + horizon,
                        )
                    # 週期報酬回歸不套分類正例權重，避免預測值被系統性往上推。
                    regressor.fit(train_x, train_target)
                    train_predicted = regressor.predict(train_x)
                    predicted = regressor.predict(validation_x)
                    bias_correction = float(np.median(train_target - train_predicted))
                    calibrated_predicted = predicted + bias_correction
                    mae = float(np.mean(np.abs(calibrated_predicted - validation_target)))
                    horizon_model = {
                        "name": f"{horizon} 日淨報酬回歸",
                        "estimator": regressor,
                        "metrics": {
                            "mae": mae,
                            "rawMae": float(np.mean(np.abs(predicted - validation_target))),
                            "biasCorrection": bias_correction,
                            "averagePredictedReturn": float(np.mean(calibrated_predicted)),
                            "averageActualReturn": float(np.mean(validation_target)),
                        },
                        "bias_correction": bias_correction,
                    }
                    output["short_horizon_returns"][str(horizon)] = horizon_model
                    output["models"].append(horizon_model["name"])
                except Exception as exc:
                    output[f"short_horizon_{horizon}d_error"] = str(exc)

        # 特徵重要性：平均 XGBoost + LightGBM 的 feature_importances_
        try:
            fi_xgb = list(output["xgboost"]["estimator"].feature_importances_) if "xgboost" in output else None
            fi_lgb = list(output["lightgbm"]["estimator"].feature_importances_) if "lightgbm" in output else None
            if fi_xgb and fi_lgb:
                combined = [(fi_xgb[i] + fi_lgb[i]) / 2 for i in range(len(FEATURE_NAMES))]
            elif fi_xgb:
                combined = fi_xgb
            elif fi_lgb:
                combined = fi_lgb
            else:
                combined = None
            if combined:
                total = sum(combined) or 1
                output["feature_importances"] = [
                    {"feature": FEATURE_NAMES[i], "importance": round(combined[i] / total, 5)}
                    for i in range(len(FEATURE_NAMES))
                ]
                output["feature_importances"].sort(key=lambda x: x["importance"], reverse=True)
        except Exception:
            pass

        return output

    def rank_probability(self, value, sorted_values):
        if not sorted_values:
            return 0.5
        rank = bisect.bisect_left(sorted_values, value)
        percentile = rank / max(len(sorted_values) - 1, 1)
        return clamp(0.05 + percentile * 0.9, 0.01, 0.99)

    def extra_model_probabilities(self, model, x):
        extra = model.get("extra_models") or {}
        if not extra.get("available") or np is None:
            return {}
        arr = np.array([x], dtype=float)
        probabilities = {}
        # *_calibrated：把分類模型的原始機率映射到「它在驗證集輸出分佈中的
        # 百分位」(rank_probability)，消除 positive_weight 加權造成的機率
        # 系統性偏高。與原始版並存輸出——buy_signal_score 目前仍用原始版，
        # 換用校準版之前必須重跑 backtest_ensemble_weights.py 驗證。舊版
        # model.pkl 沒有 calibration 欄位時自然跳過，不影響相容性。
        xgboost = extra.get("xgboost") or {}
        if xgboost.get("estimator") is not None:
            try:
                raw = float(xgboost["estimator"].predict_proba(arr)[0][1])
                probabilities["xgboost"] = raw
                if xgboost.get("calibration"):
                    probabilities["xgboost_calibrated"] = self.rank_probability(raw, xgboost["calibration"])
            except Exception:
                pass
        lightgbm = extra.get("lightgbm") or {}
        if lightgbm.get("estimator") is not None:
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="X does not have valid feature names.*", category=UserWarning)
                    raw = float(lightgbm["estimator"].predict_proba(arr)[0][1])
                probabilities["lightgbm"] = raw
                if lightgbm.get("calibration"):
                    probabilities["lightgbm_calibrated"] = self.rank_probability(raw, lightgbm["calibration"])
            except Exception:
                pass
        gradient = extra.get("gradient_boosting") or {}
        if gradient.get("estimator") is not None:
            try:
                raw = float(gradient["estimator"].predict_proba(arr)[0][1])
                probabilities["gradient_boosting"] = raw
                if gradient.get("calibration"):
                    probabilities["gradient_boosting_calibrated"] = self.rank_probability(raw, gradient["calibration"])
            except Exception:
                pass
        isolation = extra.get("isolation_forest") or {}
        if isolation.get("estimator") is not None:
            try:
                score = float(isolation["estimator"].score_samples(arr)[0])
                lo = float(isolation.get("score_low", score))
                hi = float(isolation.get("score_high", score + 1))
                ratio = (score - lo) / max(hi - lo, 1e-9)
                probabilities["isolation_forest"] = smooth_clamp_ratio(ratio)
            except Exception:
                pass
        ranker = extra.get("learning_to_rank") or {}
        if ranker.get("estimator") is not None:
            try:
                raw_predicted_return = float(ranker["estimator"].predict(arr)[0])
                probabilities["learning_to_rank"] = self.rank_probability(
                    raw_predicted_return, ranker.get("calibration") or [],
                )
                probabilities["rank_predicted_return"] = (
                    raw_predicted_return + float(ranker.get("bias_correction") or 0)
                )
            except Exception:
                pass
        for horizon, horizon_model in (
            extra.get("short_horizon_returns") or {}
        ).items():
            if horizon_model.get("estimator") is None:
                continue
            try:
                probabilities[f"predicted_return_{int(horizon)}d"] = float(
                    horizon_model["estimator"].predict(arr)[0]
                ) + float(horizon_model.get("bias_correction") or 0)
            except Exception:
                pass
        return probabilities

    def compute_auc(self, predictions):
        """ROC AUC，用排序法算 Mann-Whitney U 統計量，不依賴額外套件。
        驗證集只有單一類別(全正例或全負例)時 AUC 沒有意義，回傳 0.5(等同隨機
        猜測的基準線)，不要回傳 0——0 看起來像「模型完全猜反」會誤導人，
        實際上只是那次驗證集剛好沒有另一個類別可以比較。
        """
        scored = [(float(row["probability"]), int(row["y"])) for row in predictions]
        n_pos = sum(1 for _, y in scored if y == 1)
        n_neg = len(scored) - n_pos
        if n_pos == 0 or n_neg == 0:
            return 0.5
        ranked = sorted(scored, key=lambda pair: pair[0])
        rank_sum_positive = 0.0
        i = 0
        n = len(ranked)
        while i < n:
            j = i
            while j < n and ranked[j][0] == ranked[i][0]:
                j += 1
            avg_rank = (i + 1 + j) / 2.0  # 1-based 排名，同分區間取平均排名
            for k in range(i, j):
                if ranked[k][1] == 1:
                    rank_sum_positive += avg_rank
            i = j
        return (rank_sum_positive - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)

    def score_metrics(self, predictions, threshold=0.5):
        if not predictions:
            return {}
        tp = sum(1 for row in predictions if row["probability"] >= threshold and row["y"] == 1)
        fp = sum(1 for row in predictions if row["probability"] >= threshold and row["y"] == 0)
        tn = sum(1 for row in predictions if row["probability"] < threshold and row["y"] == 0)
        fn = sum(1 for row in predictions if row["probability"] < threshold and row["y"] == 1)
        accuracy = (tp + tn) / len(predictions)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-9)
        trades = [row for row in predictions if row["probability"] >= threshold]
        # future_return 已由 short_term_target 扣除完整成本（手續費+稅+滑點）；
        # 此處不再重複扣 0.003，避免指標偏低。
        trade_returns = [float(row.get("future_return") or 0) for row in trades]
        gross_profit = sum(value for value in trade_returns if value > 0)
        gross_loss = -sum(value for value in trade_returns if value < 0)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)
        average_return = sum(trade_returns) / len(trade_returns) if trade_returns else 0
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        returns_by_date = {}
        for row, value in zip(trades, trade_returns):
            returns_by_date.setdefault(row.get("date") or "", []).append(value)
        for date in sorted(returns_by_date):
            day_values = returns_by_date[date]
            day_return = sum(day_values) / max(len(day_values), 1)
            equity *= max(0.01, 1 + day_return)
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, (equity - peak) / peak)
        return {
            "accuracy": accuracy,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "auc": self.compute_auc(predictions),
            "profitFactor": profit_factor,
            "averageTradeReturn": average_return,
            "strategyReturn": average_return,
            "maxDrawdown": max_drawdown,
            "trades": len(trades),
            "positiveRate": sum(row["y"] for row in predictions) / len(predictions),
            "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        }

    def _recent_target_hit_rate(self):
        """最近 60 筆同一短期淨獲利目標的已結算命中率。

        target_type 必須分組；否則舊「10 日內 +10%」與新「3/5/10 日淨獲利」
        的 hit 混在一起，門檻會失去統計意義。
        """
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT hit FROM predictions
                WHERE hit IS NOT NULL AND target_type = ?
                ORDER BY id DESC
                LIMIT 60
            """, (SHORT_PROFIT_TARGET_TYPE,)).fetchall()
        if len(rows) < 20:
            return None
        return sum(row[0] for row in rows) / len(rows)

    # 新目標的 base rate 要由重新訓練與前瞻帳本建立；樣本未成熟前使用保守
    # 預設值，不沿用舊 +10% 目標約 12.6% 的桶界。
    def win_probability_display_threshold(self):
        hit_rate = self._recent_target_hit_rate()
        if hit_rate is None:
            return 0.62
        if hit_rate < 0.40:
            return 0.70
        if hit_rate < 0.48:
            return 0.66
        if hit_rate > 0.58:
            return 0.58
        return 0.62

    def buy_signal_threshold(self):
        hit_rate = self._recent_target_hit_rate()
        if hit_rate is None:
            return 0.50
        if hit_rate < 0.40:
            return 0.58
        if hit_rate < 0.48:
            return 0.54
        if hit_rate > 0.58:
            return 0.46
        return 0.50

    def setup_score_from_features(self, values, market):
        ret5 = values[4]
        ret20 = values[5]
        volume_ratio = values[7]
        trend = clamp((values[0] + 0.035) / 0.085, 0, 1)
        macd = clamp((values[2] + 0.015) / 0.04, 0, 1)
        momentum = 0.55 * clamp(ret5 / 0.08, 0, 1) + 0.45 * clamp(ret20 / 0.16, 0, 1)
        volume = clamp((volume_ratio - 0.85) / 2.4, 0, 1)
        relative = clamp((market.get("stock_vs_taiex_20", values[18]) + 0.04) / 0.12, 0, 1)
        chip = clamp((values[9] + 1) / 2, 0, 1)
        finance = clamp((values[10] + 1) / 2, 0, 1)
        chip_coverage = clamp(values[26], 0, 1)
        finance_coverage = clamp(values[27], 0, 1)
        advanced_flow = clamp((values[28] + 1) / 2, 0, 1)
        realtime_flow = clamp((values[29] + 1) / 2, 0, 1)
        advanced_flow_coverage = clamp(values[30], 0, 1)
        setup = (
            trend * 0.18 +
            macd * 0.11 +
            momentum * 0.25 +
            volume * 0.18 +
            relative * 0.17 +
            chip * chip_coverage * 0.06 +
            finance * finance_coverage * 0.05
        )
        advanced_signal = clamp((advanced_flow * 0.65) + (realtime_flow * 0.35), 0, 1)
        return clamp(setup, 0.01, 0.99), {
            "trend": trend,
            "macd": macd,
            "momentum": momentum,
            "volume": volume,
            "relativeStrength": relative,
            "chip": chip,
            "finance": finance,
            "chipCoverage": chip_coverage,
            "financeCoverage": finance_coverage,
            "advancedFlow": advanced_flow,
            "realtimeFlow": realtime_flow,
            "advancedSignal": advanced_signal,
            "advancedFlowCoverage": advanced_flow_coverage,
        }

    def risk_penalty_from_features(self, values):
        rsi = values[1] * 100
        atr_pct = values[3] * 100
        ret5 = values[4] * 100
        volume_ratio = values[7]
        daytrade_risk = max(values[21], 0)
        lending_risk = max(values[22], 0)
        short_pressure = max(values[23], 0)
        penalty = (
            clamp((rsi - 78) / 24, 0, 1) * 0.08 +
            clamp((atr_pct - 8.5) / 9, 0, 1) * 0.07 +
            clamp((ret5 - 20) / 18, 0, 1) * 0.06 +
            clamp((volume_ratio - 5.5) / 4, 0, 1) * 0.05 +
            daytrade_risk * 0.04 +
            lending_risk * 0.05 +
            short_pressure * 0.04
        )
        return clamp(penalty, 0, 0.30), {
            "rsi": rsi,
            "atrPct": atr_pct,
            "daytradeRisk": daytrade_risk,
            "lendingRisk": lending_risk,
            "shortPressure": short_pressure,
        }

    def brain_decision_inputs(self, rows):
        """Brain 決策要用的『特徵衍生』輸入,完全不跑模型推論(不載 model.pkl、不做
        ensemble、不算機率)。2026-07-09 拆模型:build_brain_decision 的決策分量
        risk_component 來自 risk_penalty、market_component 來自 market_gate——這兩個
        本來就是確定性特徵函式(risk_penalty_from_features / market_gate),Brain 決策
        從不用模型機率(formalModel 權重 0)。這裡回傳 predict_symbol 中『Brain 會讀到』
        的欄位子集,讓 build_brain_decision 走同一套下游程式碼、算出 byte-identical 的
        決策,只是不再經過模型推論。

        - trade_gate 只放 Brain 真的會讀的特徵欄位(marketOk / strongerThanMarket /
          riskOk);模型欄位(scoreOk / rankTop / anomalyOk)Brain 根本不引用,不放。
        - probability=None:權重 0、純顯示,拆模型後改由 15:10 批量預測回填顯示。
        - 資料不足回 None,對齊舊版『predict_symbol 失敗 → prediction=None』的路徑
          (那條路徑下 build_brain_decision 靠 data_complete/quality 走 observe_only)。
        """
        features = self.build_features_for_rows(rows)
        if not features or not rows:
            return None
        latest = features[-1]
        values = latest["x"]
        market = latest.get("market", {})
        risk_penalty, risk_parts = self.risk_penalty_from_features(values)
        setup_score, _ = self.setup_score_from_features(values, market)
        market_gate = self.market_gate(market)
        trade_gate = {
            "strongerThanMarket": bool(market_gate.get("stockStrongerThanTaiex")),
            "riskOk": (
                risk_penalty <= 0.16 and
                float(risk_parts.get("daytradeRisk") or 0) <= 0.90 and
                float(risk_parts.get("lendingRisk") or 0) <= 0.90 and
                float(risk_parts.get("shortPressure") or 0) <= 0.80
            ),
            "marketOk": bool(market_gate["allowBuy"]),
        }
        return {
            "probability": None,
            "threshold": self.buy_signal_threshold(),
            "riskPenalty": risk_penalty,
            "setupScore": setup_score,
            "tradeGate": trade_gate,
            "marketGate": market_gate,
            "close": self.safe_float(rows[-1].get("close")),
        }

    def buy_signal_score(self, logistic_probability, extra_probabilities, values, market):
        # 整合權重已用 backtest_ensemble_weights.py 回測驗證(每日前10名模擬
        # 買進，OOS 2025-11~2026-06，含 adjust_probability_for_market 的
        # 個股層級調整)：9 組候選配置裡，現行權重(win 0.22/0.22/0.34/0.22、
        # core 0.44/0.34/0.14/0.08)命中率並列最高(38.3%，跟win層等權配置
        # 在雜訊範圍內打平)。拿掉 anomaly(isolation forest)或改用 rank 為主
        # 的 core 權重，命中率明顯降到 34.8%~35.5%——isolation forest 的
        # 訊號有確認過的增量價值，不是裝飾用的分量。改權重前先重跑該腳本。
        win_parts = [("logistic", logistic_probability, 0.22)]
        if "xgboost" in extra_probabilities:
            win_parts.append(("xgboost", extra_probabilities["xgboost"], 0.22))
        if "lightgbm" in extra_probabilities:
            win_parts.append(("lightgbm", extra_probabilities["lightgbm"], 0.34))
        if "gradient_boosting" in extra_probabilities:
            win_parts.append(("gradient_boosting", extra_probabilities["gradient_boosting"], 0.22))
        weight_sum = sum(weight for _, _, weight in win_parts) or 1
        win_probability = sum(value * weight for _, value, weight in win_parts) / weight_sum
        rank_probability = float(extra_probabilities.get("learning_to_rank", win_probability))
        anomaly_probability = float(extra_probabilities.get("isolation_forest", 0.5))
        setup_score, setup_parts = self.setup_score_from_features(values, market)
        risk_penalty, risk_parts = self.risk_penalty_from_features(values)
        core_score = clamp(
            win_probability * 0.44 +
            rank_probability * 0.34 +
            anomaly_probability * 0.14 +
            (setup_score - 0.50) * 0.08 -
            risk_penalty,
            0.01,
            0.99,
        )
        advanced_coverage = clamp(float(setup_parts.get("advancedFlowCoverage") or 0), 0, 1)
        advanced_weight = 0.20 * advanced_coverage
        core_weight = 1.0 - advanced_weight
        advanced_signal = clamp(float(setup_parts.get("advancedSignal") or 0.5), 0, 1)
        score = clamp((core_score * core_weight) + (advanced_signal * advanced_weight), 0.01, 0.99)
        return score, {
            "coreScore": core_score,
            "advancedSignal": advanced_signal,
            "winProbability": win_probability,
            "rankProbability": rank_probability,
            "anomalyProbability": anomaly_probability,
            "setupScore": setup_score,
            "riskPenalty": risk_penalty,
            "weights": {
                "coreModel": core_weight,
                "advancedFlow": advanced_weight,
                "coreWhenAdvancedMissing": advanced_weight <= 0,
                "core": {
                    "winModels": 0.44,
                    "learningToRank": 0.34,
                    "isolationForest": 0.14,
                    "setupAdjustment": 0.08,
                },
            },
            "winWeights": {name: weight for name, _, weight in win_parts},
            "setup": setup_parts,
            "risk": risk_parts,
        }

    def load_model(self):
        with self._model_load_lock:
            return self._load_model_locked()

    def load_model_with_error(self):
        # _model_load_error 是實例變數，但 backend 是跨執行緒共享的模組級單例
        # (ThreadingHTTPServer)。呼叫端原本的寫法是「呼叫load_model()->再讀
        # self._model_load_error」，這兩步之間如果有另一個執行緒也呼叫了
        # load_model()(不管成功或失敗)，會把這個共享屬性洗成別的執行緒的
        # 結果，讓呼叫端顯示錯誤的失敗原因(health_check.py/predict_symbol
        # 都有這個模式)。這裡把「執行+讀取錯誤訊息」包在同一個鎖裡當成單一
        # 原子操作回傳，呼叫端要拿失敗原因時改用這個方法，不要再各自另外
        # 讀 self._model_load_error。
        with self._model_load_lock:
            model = self._load_model_locked()
            return model, self._model_load_error

    def _load_model_locked(self):
        # 呼叫前必須已經持有 self._model_load_lock，本身不再上鎖。
        self._model_load_error = ""
        if not self.model_path.exists():
            self._model_cache = None
            return None
        model_env = self.read_model_env()
        environment_check = compare_model_environment(model_env)
        if not environment_check["ok"]:
            self._model_load_error = "model environment mismatch: " + "; ".join(environment_check["issues"])
            return None
        try:
            mtime = self.model_path.stat().st_mtime_ns
        except OSError:
            mtime = None
        cached = self._model_cache
        if cached is not None and mtime is not None and cached.get("mtime") == mtime:
            return cached["model"]
        try:
            model = pickle.loads(self.model_path.read_bytes())
        except (ModuleNotFoundError, AttributeError, pickle.UnpicklingError, EOFError) as exc:
            self._model_load_error = str(exc)
            return None
        if model.get("feature_names") != FEATURE_NAMES:
            self._model_load_error = "model feature schema mismatch"
            return None
        if model.get("data_policy") != DATA_POLICY_VERSION:
            self._model_load_error = "model data policy mismatch"
            return None
        if model.get("target_type") != SHORT_PROFIT_TARGET_TYPE:
            self._model_load_error = "model target policy mismatch"
            return None
        if model.get("target_policy_hash") != SHORT_PROFIT_POLICY_HASH:
            self._model_load_error = "model target policy hash mismatch"
            return None
        try:
            expected_policy_spec = short_profit_policy_spec(
                model.get("symbols"), model.get("threshold"),
            )
            expected_policy_hash = short_profit_policy_hash(
                model.get("symbols"), model.get("threshold"),
            )
        except (TypeError, ValueError):
            self._model_load_error = "model policy metadata invalid"
            return None
        if (
            model.get("policy_hash") != expected_policy_hash
            or model.get("policy_spec") != expected_policy_spec
        ):
            self._model_load_error = "model policy contract mismatch"
            return None
        self._model_cache = {"mtime": mtime, "model": model}
        return model

    def backfill_active_model_training_dates(self):
        """Add auditable data dates to a legacy active model without retraining it."""
        with self._model_load_lock:
            model = self._load_model_locked()
            if not model:
                return {"ok": False, "updated": False, "error": self._model_load_error or "model unavailable"}
            if model.get("training_data_max_date"):
                return {
                    "ok": True,
                    "updated": False,
                    "trainingDataMaxDate": model.get("training_data_max_date"),
                    "trainingSampleMaxDate": model.get("training_sample_max_date"),
                }
            trained_at = str(model.get("trained_at") or "")
            symbols = list(dict.fromkeys(
                str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
                for symbol in (model.get("symbols") or [])
                if str(symbol or "").strip()
            ))
            if not trained_at or not symbols:
                return {"ok": False, "updated": False, "error": "model training timestamp or symbols missing"}
            placeholders = ",".join("?" for _ in symbols)
            with self.connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT MAX(date)
                    FROM prices
                    WHERE symbol IN ({placeholders})
                      AND updated_at <= ?
                      AND (
                            LOWER(COALESCE(price_source, '')) LIKE '%twse%'
                         OR LOWER(COALESCE(price_source, '')) LIKE '%tpex%'
                         OR LOWER(COALESCE(price_source, '')) LIKE '%finmind%'
                         OR LOWER(COALESCE(price_source, '')) LIKE '%shioaji%'
                         OR LOWER(COALESCE(price_source, '')) LIKE '%sinopac%'
                      )
                      AND LOWER(COALESCE(price_source, '')) NOT LIKE '%yahoo%'
                      AND LOWER(COALESCE(price_source, '')) NOT LIKE '%fallback%'
                      AND LOWER(COALESCE(price_source, '')) NOT LIKE '%simulate%'
                    """,
                    [*symbols, trained_at],
                ).fetchone()
            training_data_max_date = str(row[0] or "")[:10] if row else ""
            if not training_data_max_date:
                return {
                    "ok": False,
                    "updated": False,
                    "error": "no verified price row existed before the model training timestamp",
                }
            model["training_data_max_date"] = training_data_max_date
            model["training_sample_max_date"] = model.get("training_sample_max_date")
            model["training_date_metadata_source"] = "verified_prices_updated_at_before_training"
            model["training_date_metadata_backfilled_at"] = now_text()
            self.write_model_env(model)
            temp_path = self.model_path.with_name(f"{self.model_path.name}.tmp")
            temp_path.write_bytes(pickle.dumps(model))
            os.replace(temp_path, self.model_path)
            self._model_cache = None
        with self.connect() as conn:
            self.set_meta(conn, "last_model_training_data_max_date", training_data_max_date)
            self.set_meta(conn, "last_model_training_date_metadata_backfill", now_text())
        return {
            "ok": True,
            "updated": True,
            "trainingDataMaxDate": training_data_max_date,
            "trainingSampleMaxDate": model.get("training_sample_max_date"),
        }

    def predict_symbol(self, symbol, save=True, repair=True):
        model, load_error = self.load_model_with_error()
        if not model:
            reason = load_error or "model.pkl not found"
            raise RuntimeError(f"正式模型不可用：{reason}，請先完成每日更新或重新訓練模型")
        expected_latest_date = self.latest_complete_price_date()
        # 讀快取(鎖內)：命中判斷+deepcopy+就地標記 saved+快照 gen 都在鎖內完成，
        # 但 _save_prediction_row(走 DB_WRITE_LOCK)一律移到鎖外，避免
        # _predict_cache_lock→DB_WRITE_LOCK 與 upsert 的 DB_WRITE_LOCK→
        # _predict_cache_lock 形成環路死鎖。
        hit_payload = None
        need_save_hit = False
        with self._predict_cache_lock:
            cached = self._predict_cache.get(symbol)
            if (
                cached is not None
                and cached["trainedAt"] == model.get("trained_at")
                and time.time() - cached["at"] < PREDICT_RESULT_CACHE_TTL_SECONDS
                and (
                    not expected_latest_date
                    or str((cached.get("payload") or {}).get("priceDate") or "")[:10]
                    == expected_latest_date
                )
            ):
                # 快取只會存「品質檢查通過、成功算完」的結果(失敗路徑直接 raise
                # 不進快取)，所以不論呼叫端 repair 旗標為何都能安全共用。
                # deepcopy：payload 是巢狀 dict，呼叫端(Brain Engine 等)可能就地
                # 修改，不能讓不同呼叫端透過快取互相污染。
                if save and not cached["saved"]:
                    # 鎖內先「預約」saved=True(避免兩執行緒同時命中都去存)，
                    # 實際 DB 存出在鎖外；就算存失敗，之後 compute 路徑會再存一次，
                    # 且 _save_prediction_row 是 ON CONFLICT DO NOTHING 冪等。
                    cached["saved"] = True
                    need_save_hit = True
                hit_payload = copy.deepcopy(cached["payload"])
            gen_at_start = self._predict_cache_gen
        if hit_payload is not None:
            if need_save_hit:
                self._save_prediction_row(hit_payload)
            return hit_payload
        rows, quality = self.ensure_model_ready_rows(symbol, repair=repair)
        if not quality["ok"]:
            raise RuntimeError(
                f"{symbol} model data incomplete: {', '.join(quality['missing'])}; "
                f"rows={quality['rows']}, chipCoverage={quality['chipCoverage']:.2f}, "
                f"chipSourceCoverage={quality['chipSourceCoverage']:.2f}, "
                f"marginSourceCoverage={quality['marginSourceCoverage']:.2f}, "
                f"financeCoverage={quality['financeCoverage']:.2f}, "
                f"financeSourceCoverage={quality['financeSourceCoverage']:.2f}, "
                f"priceSource={quality.get('priceSource')}, "
                f"chipSource={quality.get('chipSource')}, "
                f"marginSource={quality.get('marginSource')}, "
                f"financeSource={quality.get('financeSource')}"
            )
        latest_date = str(quality.get("latestDate") or "")[:10]
        if expected_latest_date and latest_date != expected_latest_date and repair:
            try:
                self.update_prices(
                    [symbol], refresh_info=False, force_refresh=True
                )
            except Exception as exc:
                print(f"stale prediction repair for {symbol} failed: {exc}")
            rows, quality = self.ensure_model_ready_rows(symbol, repair=False)
            latest_date = str(quality.get("latestDate") or "")[:10]
            expected_latest_date = self.latest_complete_price_date()
        if expected_latest_date and latest_date != expected_latest_date:
            raise RuntimeError(
                f"{symbol} stock data stale: latestDate={latest_date or '-'}, "
                f"latestCompleteMarketDate={expected_latest_date}"
            )
        if not quality["ok"]:
            raise RuntimeError(
                f"{symbol} model data incomplete after stale-data repair: "
                f"{', '.join(quality.get('missing') or [])}"
            )
        market_quality = self.market_data_quality(quality.get("latestDate"))
        if not market_quality["ok"] and repair:
            # repair=False 時不觸發網路補抓，避免每次 predict 都等 Yahoo Finance
            self.update_market_data()
            market_quality = self.market_data_quality(quality.get("latestDate"))
        if not market_quality["ok"]:
            raise RuntimeError(
                f"{symbol} market data incomplete: {', '.join(market_quality['missing'])}; "
                f"latestDate={market_quality.get('latestDate')}"
            )
        features = self.build_features_for_rows(rows)
        if not features:
            raise RuntimeError(f"{symbol} has no features")
        latest = features[-1]
        x = [(latest["x"][i] - model["means"][i]) / model["stdevs"][i] for i in range(len(FEATURE_NAMES))]
        logistic_probability = sigmoid(model["weights"][0] + sum(x[i] * model["weights"][i + 1] for i in range(len(FEATURE_NAMES))))
        extra_probabilities = self.extra_model_probabilities(model, x)
        # 2026-07-04 稽核修復(可觀測性)：extra_models 的 available=True 只代表
        # 「有跑 ensemble」，不保證每個 estimator 都訓練成功——IsolationForest 或
        # learning_to_rank 個別 fit/predict 丟例外會被吞成 *_error，available 仍
        # True，這種 model.pkl 上線後 anomaly 硬閘門(anomalyOk 靠 isolation_forest)
        # 會對「每一檔股票」靜默恆通過、rankTop 也退化成純 win 衍生值，直到下次
        # 重訓。把退化狀態顯性化到 payload，讓決策層/UI/健康檢查看得出「這道閘門
        # 其實沒在把關」，而不是靜默放行。(改閘門「恆過→中性略過」的行為屬
        # accuracy_hypothesis，需回測，這裡只做可觀測性、不動放行判定。)
        degraded_gates = []
        if extra_probabilities:  # 非空=ensemble 有在跑；空=整包不可用(已知全退化)
            if "isolation_forest" not in extra_probabilities:
                degraded_gates.append("anomaly")
            if "learning_to_rank" not in extra_probabilities:
                degraded_gates.append("rank")
        # M1 修復：rawProbability 應代表「市場調整前的純模型輸出」，
        # 不要在這裡多呼叫一次 adjust_probability_for_market。
        # 唯一的市場調整發生在下方 line：probability = adjust(probability, market)
        raw_probability = logistic_probability
        probability, buy_signal = self.buy_signal_score(
            logistic_probability,
            extra_probabilities,
            latest["x"],
            latest.get("market", {}),
        )
        probability = self.adjust_probability_for_market(probability, latest.get("market", {}))
        predicted_net_returns = {
            horizon: extra_probabilities.get(f"predicted_return_{horizon}d")
            for horizon in SHORT_PROFIT_HORIZONS
            if extra_probabilities.get(f"predicted_return_{horizon}d") is not None
        }
        predicted_weight = sum(
            SHORT_PROFIT_HORIZON_WEIGHTS[horizon]
            for horizon in predicted_net_returns
        )
        expected_net_return = (
            sum(
                float(predicted_net_returns[horizon])
                * SHORT_PROFIT_HORIZON_WEIGHTS[horizon]
                for horizon in predicted_net_returns
            ) / predicted_weight
            if predicted_weight else None
        )
        short_profit_model = model.get("target_type") == SHORT_PROFIT_TARGET_TYPE
        risk_adjusted_expected_return = extra_probabilities.get("rank_predicted_return")
        if short_profit_model and len(predicted_net_returns) < len(SHORT_PROFIT_HORIZONS):
            degraded_gates.append("short_horizon_returns")
        threshold = self.buy_signal_threshold()
        market_gate = self.market_gate(latest.get("market", {}))
        values = latest["x"]
        risk_parts = buy_signal.get("risk") or {}
        trade_gate = {
            "scoreOk": probability >= threshold,
            "rankTop": float(buy_signal.get("rankProbability") or 0) >= 0.55,
            "anomalyOk": (
                float(buy_signal.get("anomalyProbability") or 0.5) >= 0.35 or
                (
                    float(buy_signal.get("rankProbability") or 0) >= 0.75 and
                    float(buy_signal.get("setupScore") or 0) >= 0.55
                )
            ),
            # volumeExpanded 移出硬閘門：volume_ratio 已是 ML 特徵之一，
            # 模型已從訓練資料學習量能影響，不應再硬封鎖。
            "strongerThanMarket": bool(market_gate.get("stockStrongerThanTaiex")),
            "riskOk": (
                buy_signal["riskPenalty"] <= 0.16 and
                float(risk_parts.get("daytradeRisk") or 0) <= 0.90 and
                float(risk_parts.get("lendingRisk") or 0) <= 0.90 and
                float(risk_parts.get("shortPressure") or 0) <= 0.80
            ),
            "marketOk": bool(market_gate["allowBuy"]),
        }
        if short_profit_model:
            trade_gate["expectedReturnOk"] = bool(
                expected_net_return is not None
                and expected_net_return > 0
                and risk_adjusted_expected_return is not None
                and float(risk_adjusted_expected_return) > 0
                and "short_horizon_returns" not in degraded_gates
            )
        # anomalyGateActive 刻意不放進 trade_gate——action_ready=all(trade_gate.values())
        # 會把它當硬閘門，退化模型(False)就會擋掉所有買進，那是需回測的行為變更。
        # 放 payload 頂層當純觀測欄位，不影響 BUY_CANDIDATE 判定。
        trade_gate_observability = {
            "anomalyGateActive": "anomaly" not in degraded_gates,
            "degradedGates": degraded_gates,
        }
        # 量能資訊保留在 payload 供前端顯示，但不列入硬閘門
        volume_expanded_info = values[7] >= 1.15
        action_ready = all(trade_gate.values())
        action = "BUY_CANDIDATE" if action_ready else "WAIT"
        if probability >= threshold and not market_gate["allowBuy"]:
            action = "WAIT_MARKET_RISK"
        payload = {
            "symbol": symbol,
            "priceDate": latest["date"],
            "close": latest["close"],
            "probability": probability,
            "rawProbability": raw_probability,
            "winProbability": self.adjust_probability_for_market(buy_signal["winProbability"], latest.get("market", {})),
            "setupScore": buy_signal["setupScore"],
            "riskPenalty": buy_signal["riskPenalty"],
            "buySignal": buy_signal,
            "tradeGate": trade_gate,
            "degradedGates": degraded_gates,
            "anomalyGateActive": trade_gate_observability["anomalyGateActive"],
            "volumeExpanded": volume_expanded_info,
            "modelProbabilities": {
                "logistic": logistic_probability,
                **{
                    key: value for key, value in extra_probabilities.items()
                    if key != "rank_predicted_return"
                    and not key.startswith("predicted_return_")
                },
            },
            "rankPredictedReturn": extra_probabilities.get("rank_predicted_return"),
            "shortTermProfit": {
                "targetType": model.get("target_type") or SHORT_PROFIT_TARGET_TYPE,
                "policyHash": model.get("policy_hash") or "",
                "horizons": list(SHORT_PROFIT_HORIZONS),
                "predictedNetReturns": {
                    f"{horizon}d": predicted_net_returns.get(horizon)
                    for horizon in SHORT_PROFIT_HORIZONS
                },
                "expectedNetReturn": expected_net_return,
                "riskAdjustedExpectedReturn": risk_adjusted_expected_return,
                "winProbability": buy_signal.get("winProbability"),
                "monsterSetupRequired": False,
                "observationOnly": True,
                "enabled": short_profit_model,
            },
            "threshold": threshold,
            "action": action,
            "marketGate": market_gate,
            "modelVersion": model["version"],
            "modelMetrics": model.get("metrics", {}),
            "trainedAt": model.get("trained_at"),
            "dataQuality": quality,
            "marketDataQuality": market_quality,
        }
        if save:
            self._save_prediction_row(payload)
        # 寫快取(鎖內、gen 守衛)：若計算期間有人失效過這檔股票(或整批 clear)，
        # _predict_cache_gen 會前進，此時放棄寫回——避免用「失效前的舊資料算出的
        # payload」覆寫快取讓過時預測殘留。_save_prediction_row 已在鎖外做完。
        with self._predict_cache_lock:
            if self._predict_cache_gen == gen_at_start:
                self._predict_cache[symbol] = {
                    "at": time.time(),
                    "trainedAt": model.get("trained_at"),
                    "payload": copy.deepcopy(payload),
                    "saved": bool(save),
                }
        return payload

    def _save_prediction_row(self, payload):
        # 原本是 SELECT不存在才INSERT，多執行緒下有TOCTOU競態(見init_db()
        # 建idx_predictions_unique處的說明)，改成資料庫層UNIQUE約束+
        # ON CONFLICT DO NOTHING，一次操作原子完成，不會再插入重複列。
        target_type = str(
            (payload.get("shortTermProfit") or {}).get("targetType")
            or SHORT_PROFIT_TARGET_TYPE
        )
        uses_short_profit_target = target_type == SHORT_PROFIT_TARGET_TYPE
        target_horizon = (
            SHORT_PROFIT_MAX_HORIZON_DAYS
            if uses_short_profit_target else MONSTER_TARGET_HORIZON_DAYS
        )
        target_return = 0.0 if uses_short_profit_target else MONSTER_TARGET_RETURN
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO predictions (
                    created_at, symbol, price_date, model_version, probability, threshold, action,
                    target_horizon, target_return, target_type, close
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, price_date, model_version) DO NOTHING
            """, (
                now_text(), payload["symbol"], payload["priceDate"], payload["modelVersion"],
                payload["probability"], payload["threshold"], payload["action"],
                target_horizon, target_return, target_type,
                payload["close"],
            ))

    # 逾期門檻：10 交易日 horizon ≈ 14 日曆天，加上週末/連假/補結算緩衝取 25。
    PREDICTION_SETTLEMENT_OVERDUE_CALENDAR_DAYS = 25

    def prediction_settlement_health(self):
        """殭屍預測稽核：price_date 早於「早該結算完」的日期但 hit 仍 NULL 的
        預測筆數。compute_prediction_outcomes 對「無價格列/日期對不上/close=0」
        都是靜默 continue，這些列永遠不進戰績看板的分母(只算 hit IS NOT NULL)
        ——斷更股票的預測被靜默抽走，命中率會朝「有持續更新的股票」偏斜，
        使用者拿偏樂觀的戰績校準對雷達的信任度。

        2026-07-04 稽核修復：overdue 原本沒有排除「結構性永久無法結算」的預測
        (下市/全額交割/長期停牌股，該股票的 prices 資料本身已經停更，不是結算
        管線的錯，永遠不會有新資料讓它結算)。這類殭屍列會長期墊高 overdue、
        佔滿 topSymbols，稀釋真正新故障的可見度。做法：查每個逾期股票在
        prices 表的最新一筆日期，如果本身也停更超過同一個 cutoff，歸類到
        structurallyUnsettleable，不算進 overdue/topSymbols。"""
        cutoff = (dt.datetime.now() - dt.timedelta(
            days=self.PREDICTION_SETTLEMENT_OVERDUE_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        with self.connect() as conn:
            overdue_rows = conn.execute(
                "SELECT symbol, COUNT(*) FROM predictions "
                "WHERE hit IS NULL AND price_date <= ? GROUP BY symbol ORDER BY COUNT(*) DESC",
                (cutoff,),
            ).fetchall()
            eligible = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE price_date <= ?", (cutoff,)
            ).fetchone()[0]
            symbols = [str(r[0]) for r in overdue_rows]
            latest_price_dates = {}
            if symbols:
                placeholders = ", ".join("?" for _ in symbols)
                latest_price_dates = {
                    str(row[0]): row[1] for row in conn.execute(
                        f"SELECT symbol, MAX(date) FROM prices WHERE symbol IN ({placeholders}) "
                        "GROUP BY symbol",
                        symbols,
                    ).fetchall()
                }
        real_overdue = []
        structurally_unsettleable = 0
        for symbol_row in overdue_rows:
            symbol, count = str(symbol_row[0]), int(symbol_row[1])
            latest = latest_price_dates.get(symbol)
            if latest is None or latest < cutoff:
                structurally_unsettleable += count
            else:
                real_overdue.append({"symbol": symbol, "count": count})
        overdue = sum(r["count"] for r in real_overdue)
        return {
            "overdue": overdue,
            "structurallyUnsettleable": structurally_unsettleable,
            "eligible": int(eligible or 0),
            "overdueRate": round(overdue / eligible, 4) if eligible else 0.0,
            "topSymbols": real_overdue[:5],
            "cutoffDate": cutoff,
        }

    def model_prediction_eligibility(self, symbol, market_quality_cache=None):
        """Read-only prefilter for the independent model prediction universe."""
        code = str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
        rows = self.rows_with_verified_sources(self.load_price_rows(code))
        quality = self.model_data_quality(code, rows)
        if not quality.get("ok"):
            missing = list(quality.get("missing") or [])
            history_shortfall = "priceRowsEnough" in missing
            return {
                "eligible": False,
                "symbol": code,
                "stage": "stock_data_quality",
                "reason": ", ".join(missing or ["unknown_stock_data_gap"]),
                "repairable": bool(missing) and not history_shortfall,
                "structuralHistoryGap": history_shortfall,
                "dataQuality": quality,
            }
        latest_date = str(quality.get("latestDate") or "")[:10]
        cache = market_quality_cache if isinstance(market_quality_cache, dict) else {}
        complete_date_key = "__latest_complete_price_date__"
        if complete_date_key not in cache:
            cache[complete_date_key] = self.latest_complete_price_date()
        expected_latest_date = str(cache.get(complete_date_key) or "")[:10]
        if expected_latest_date and latest_date != expected_latest_date:
            return {
                "eligible": False,
                "symbol": code,
                "stage": "stock_data_freshness",
                "reason": "latestDateMatchesMarket",
                "repairable": True,
                "structuralHistoryGap": False,
                "latestDate": latest_date or None,
                "expectedLatestDate": expected_latest_date,
                "dataQuality": quality,
            }
        if latest_date not in cache:
            cache[latest_date] = self.market_data_quality(latest_date)
        market_quality = cache[latest_date]
        if not market_quality.get("ok"):
            return {
                "eligible": False,
                "symbol": code,
                "stage": "market_data_quality",
                "reason": ", ".join(market_quality.get("missing") or ["unknown_market_data_gap"]),
                "dataQuality": quality,
                "marketDataQuality": market_quality,
            }
        return {
            "eligible": True,
            "symbol": code,
            "stage": "ready",
            "reason": "",
            "latestDate": latest_date,
        }

    def latest_complete_price_date(self, minimum_symbols=1500):
        minimum_symbols = max(1, int(minimum_symbols or 1500))
        now_value = time.time()
        with self._predict_cache_lock:
            cached = self._latest_complete_price_date_cache
            if (
                int(cached.get("minimumSymbols") or 0) == minimum_symbols
                and now_value - float(cached.get("at") or 0) < 30
            ):
                return str(cached.get("value") or "")
        with self.connect() as conn:
            row = conn.execute("""
                SELECT date
                FROM prices
                GROUP BY date
                HAVING COUNT(DISTINCT symbol) >= ?
                ORDER BY date DESC
                LIMIT 1
            """, (minimum_symbols,)).fetchone()
        value = str(row[0] or "")[:10] if row else ""
        with self._predict_cache_lock:
            self._latest_complete_price_date_cache = {
                "at": now_value,
                "minimumSymbols": minimum_symbols,
                "value": value,
            }
        return value

    def batch_save_predictions(self, symbols=None, limit=600):
        """
        每日收盤後批量為液態宇宙所有股票存一筆 ML 預測。
        _save_prediction_row() 內部靠 idx_predictions_unique(symbol,
        price_date, model_version) + ON CONFLICT DO NOTHING 去重：
          同一 (symbol, price_date, model_version) 只插入一次。
        回傳 {saved, skipped, errors, symbols_total}
        """
        requested_total = max(1, int(limit or 600))
        fill_to_limit = symbols is None
        if fill_to_limit:
            # 資料品質預篩會排除歷史不足或官方來源覆蓋不完整的標的。先多取候選，
            # 再按原流動性排序補滿合格數，避免「要求 600」實際只剩五百多檔。
            pool_limit = max(requested_total + 100, int(math.ceil(requested_total * 1.15)))
            candidate_pool = list(self.liquid_monster_universe(limit=pool_limit))
        else:
            candidate_pool = list(symbols or [])
        candidate_pool_total = len(candidate_pool)
        saved = 0
        ineligible = []
        errors = []
        prediction_dates = set()
        model, model_error = self.load_model_with_error()
        if not model:
            errors.append({
                "symbol": "*",
                "stage": "model_load",
                "error": model_error or "model.pkl not found",
            })
        market_quality_cache = {}
        eligible_symbols = []
        eligibility_dates = {}
        considered_symbols = []
        if model:
            for sym in candidate_pool:
                if fill_to_limit and len(eligible_symbols) >= requested_total:
                    break
                considered_symbols.append(str(sym))
                try:
                    eligibility = self.model_prediction_eligibility(
                        sym,
                        market_quality_cache=market_quality_cache,
                    )
                except Exception as exc:
                    errors.append({
                        "symbol": str(sym),
                        "stage": "eligibility_check",
                        "error": str(exc),
                    })
                    continue
                if eligibility.get("eligible"):
                    code = str(sym)
                    eligible_symbols.append(code)
                    eligibility_dates[code] = str(eligibility.get("latestDate") or "")[:10]
                else:
                    ineligible.append(eligibility)
        else:
            considered_symbols = [str(sym) for sym in candidate_pool]
        total = len(considered_symbols)
        existing_prediction_keys = set()
        if eligible_symbols and model:
            placeholders = ",".join("?" for _ in eligible_symbols)
            with self.connect() as conn:
                existing_prediction_keys = {
                    (str(row[0]), str(row[1])[:10])
                    for row in conn.execute(
                        f"""
                        SELECT symbol, price_date
                        FROM predictions
                        WHERE model_version = ?
                          AND symbol IN ({placeholders})
                        """,
                        [str(model.get("version") or ""), *eligible_symbols],
                    ).fetchall()
                }
        skipped_existing = 0
        for sym in eligible_symbols:
            eligibility_date = eligibility_dates.get(sym) or ""
            if (sym, eligibility_date) in existing_prediction_keys:
                skipped_existing += 1
                if eligibility_date:
                    prediction_dates.add(eligibility_date)
                continue
            try:
                result = self.predict_symbol(sym, save=True, repair=False)
                if result.get("priceDate"):
                    prediction_dates.add(str(result["priceDate"])[:10])
                # predict_symbol 回傳時不區分「新插入」vs「已存在跳過」，
                # 用 predictions 表的 rowcount 變化來判斷也較複雜，
                # 這裡直接計為 saved（重複呼叫時 DB 層自動跳過）
                saved += 1
            except Exception as exc:
                errors.append({"symbol": sym, "stage": "prediction", "error": str(exc)})
        paper_signal_count = 0
        paper_signal_errors = []
        for price_date in sorted(prediction_dates):
            try:
                sync = self.sync_model_prediction_signals(price_date=price_date)
                paper_signal_count += int(sync.get("saved") or 0)
            except Exception as exc:
                paper_signal_errors.append({"priceDate": price_date, "error": str(exc)})
        return {
            "ok": True,
            "symbols_total": total,
            "requested_total": requested_total if fill_to_limit else len(candidate_pool),
            "candidate_pool_total": candidate_pool_total,
            "filled_to_requested": bool(
                not fill_to_limit or len(eligible_symbols) >= requested_total
            ),
            "eligible_shortfall": (
                max(0, requested_total - len(eligible_symbols)) if fill_to_limit else 0
            ),
            "eligible_total": len(eligible_symbols),
            "saved": saved,
            "skipped": len(ineligible) + skipped_existing,
            "skipped_ineligible_count": len(ineligible),
            "skipped_existing_count": skipped_existing,
            "ineligible": ineligible,
            "repair_symbols": [
                str(item.get("symbol") or "")
                for item in ineligible
                if item.get("repairable") is True and item.get("symbol")
            ],
            "error_count": len(errors),
            "errors": errors,
            "paper_signal_count": paper_signal_count,
            "paper_signal_errors": paper_signal_errors,
        }

    def sync_model_prediction_signals(self, price_date=None, max_per_day=PAPER_MAX_OPEN_POSITIONS):
        """把模型自己的 BUY_CANDIDATE 轉成獨立紙上訊號，不讀妖股候選。"""
        where = "WHERE action = 'BUY_CANDIDATE' AND target_type = ?"
        params = [SHORT_PROFIT_TARGET_TYPE]
        if price_date:
            where += " AND price_date = ?"
            params.append(str(price_date)[:10])
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT created_at, symbol, price_date, model_version,
                       probability, threshold, action, target_type, close
                FROM predictions
                {where}
                ORDER BY price_date ASC, symbol ASC, created_at DESC
            """, params).fetchall()

        # 同一檔同一天可能因重訓留下多個 model_version；紙上帳只採最後一版，
        # 否則同一股票會被重複計入候選與績效。
        latest_by_symbol_date = {}
        for row in rows:
            key = (str(row["price_date"] or "")[:10], str(row["symbol"] or ""))
            latest_by_symbol_date.setdefault(key, row)
        by_date = {}
        for row in latest_by_symbol_date.values():
            by_date.setdefault(str(row["price_date"] or "")[:10], []).append(row)
        selected = []
        for day_rows in by_date.values():
            selected.extend(sorted(
                day_rows,
                key=lambda row: float(row["probability"] or 0),
                reverse=True,
            )[:max(1, int(max_per_day or PAPER_MAX_OPEN_POSITIONS))])

        symbols = sorted({str(row["symbol"] or "") for row in selected if row["symbol"]})
        names = {}
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            with self.connect() as conn:
                names = {
                    str(row[0]): str(row[1] or "")
                    for row in conn.execute(
                        f"SELECT symbol, name FROM stock_info WHERE symbol IN ({placeholders})",
                        symbols,
                    ).fetchall()
                }

        saved = 0
        with self.connect() as conn:
            for row in selected:
                close = float(row["close"] or 0)
                if close <= 0:
                    continue
                probability = float(row["probability"] or 0)
                threshold = float(row["threshold"] or 0)
                self.save_strategy_signal(conn, {
                    "signalDate": str(row["price_date"] or "")[:10],
                    "strategy": "model_short_profit_3_5_10d",
                    "side": "BUY_CANDIDATE",
                    "symbol": str(row["symbol"] or ""),
                    "name": names.get(str(row["symbol"] or ""), ""),
                    "decision": "短期淨獲利模型候選",
                    "score": probability * 100,
                    "modelVersion": row["model_version"],
                    "price": close,
                    "buyPoint": close,
                    "stopPrice": close * 0.93,
                    "targetPrice": None,
                    "tradeHorizon": "model_short_profit",
                    "tradeHorizonLabel": "模型 3／5／10 日淨獲利",
                    "tradeHorizonDays": "3,5,10",
                    "tradeHorizonScore": probability * 100,
                    "dataDate": str(row["price_date"] or "")[:10],
                    "dataSource": "正式 predictions 表",
                    "decisionSource": "短期淨獲利 ensemble 模型獨立紙上訊號（不使用妖股候選）",
                    "evidence": {
                        "scope": "independent_model",
                        "probability": probability,
                        "threshold": threshold,
                        "action": row["action"],
                        "targetType": row["target_type"],
                        "selection": f"當日模型 BUY_CANDIDATE 前 {max_per_day} 名",
                    },
                })
                saved += 1
        return {
            "ok": True,
            "priceDate": str(price_date or ""),
            "sourcePredictions": len(rows),
            "selected": len(selected),
            "saved": saved,
        }

    def adjust_probability_for_market(self, probability, market):
        adjusted = probability
        if market.get("taiex_ma_gap", 0) < 0:
            adjusted *= 0.85
        if market.get("market_regime", 0) < 0:
            adjusted *= 0.88
        if market.get("stock_vs_taiex_20", 0) <= 0:
            adjusted *= 0.92
        return clamp(adjusted, 0.01, 0.99)

    def market_gate(self, market):
        taiex_above_month = market.get("taiex_ma_gap", 0) >= 0
        stock_stronger = market.get("stock_vs_taiex_20", 0) > 0
        regime = market.get("market_regime", 0)
        market_trend_ok = bool(taiex_above_month and regime >= 0)
        return {
            "allowBuy": market_trend_ok,
            "taiexAboveMonthLine": bool(taiex_above_month),
            "stockStrongerThanTaiex": bool(stock_stronger),
            "hotMarket": bool(market_trend_ok and regime > 0),
            "regime": "多頭" if regime > 0 else "空頭" if regime < 0 else "震盪",
        }

    def market_status(self, live_price=None):
        """全市場層級的大盤環境紅綠燈：妖股短線最怕大盤 turbulent，這裡從最新大盤
        資料算 regime + market_gate，對應成綠燈(可積極)/黃燈(保守)/紅燈(避開)。
        純讀取現有市場資料、不打網路。stale 時如實標記(用凍結舊資料判斷會誤導)。

        2026-07-09 使用者「大盤要即時」：紅綠燈的「站上/跌破月線」改用『即時加權指數』
        對 20 日均線(月線)重算——盤中就能反映大盤現在站上還是跌破月線,而不是用昨天的
        日線收盤。live_price 為 None(拿不到即時價)時完全退回日線行為(graceful,不退化)。
        regime(多日趨勢)本質是日線概念、盤中不會變,維持用日線;只把「站上月線」即時化。
        月線=最近 20 根『已收盤』日K的均值(今天未收不納入)。**market_gate 本身(餵個股
        買賣判斷)完全不動**,只有這張『顯示用』紅綠燈吃即時價。"""
        try:
            market_rows = self.load_market_rows()
            context = MarketContext(market_rows)
            taiex_dates = context.dates_by_key.get("TAIEX") or []
            if not taiex_dates:
                return {"ok": False, "error": "無大盤資料"}
            latest_date = taiex_dates[-1]
            market = self.market_features(latest_date, context, 0.0)
            gate = self.market_gate(market)
            regime_val = float(market.get("market_regime") or 0)
            taiex_ma_gap = float(market.get("taiex_ma_gap") or 0)
            allow = bool(gate.get("allowBuy"))
            hot = bool(gate.get("hotMarket"))
            taiex_above_month = bool(gate.get("taiexAboveMonthLine"))
            live_gate = False
            if live_price is not None:
                try:
                    lp = float(live_price)
                except (TypeError, ValueError):
                    lp = 0.0
                ma20 = context.ma_value("TAIEX", latest_date, 20)
                if lp > 0 and ma20 > 0:
                    # 即時 gap = (即時加權 - 月線) / 月線;regime 仍用日線(多日趨勢盤中不變)
                    taiex_ma_gap = (lp - ma20) / ma20
                    taiex_above_month = taiex_ma_gap >= 0
                    market_trend_ok = bool(taiex_above_month and regime_val >= 0)
                    allow = market_trend_ok
                    hot = bool(market_trend_ok and regime_val > 0)
                    live_gate = True
            if not allow:
                light = "red"
                advice = "大盤偏弱（跌破月線或走空），妖股短線風險高，建議避開或極輕倉"
            elif hot:
                light = "green"
                advice = "大盤偏多、動能強，妖股短線可較積極"
            else:
                light = "yellow"
                advice = "大盤震盪，妖股短線保守、控制部位大小"
            stale_days = calendar_days_between(latest_date, today_key())
            return {
                "ok": True,
                "light": light,
                "regime": gate.get("regime"),
                "advice": advice,
                "allowBuy": bool(allow),
                "hotMarket": bool(hot),
                "taiexAboveMonthLine": bool(taiex_above_month),
                "taiexMaGapPct": round(taiex_ma_gap * 100, 2),
                "taiexRet20Pct": round(float(market.get("taiex_ret_20") or 0) * 100, 2),
                "liveGate": live_gate,
                "latestDate": latest_date,
                "stale": stale_days is not None and stale_days > MARKET_DATA_MAX_STALE_DAYS,
                "staleDays": stale_days,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def model_training_freshness(self, model=None):
        """Compare the active model with completed market sessions, not calendar days."""
        active_model = model if model is not None else self.load_model()
        trained_at = str((active_model or {}).get("trained_at") or "")
        trained_date = trained_at[:10]
        training_data_max_date = str(
            (active_model or {}).get("training_data_max_date") or ""
        )[:10]
        training_sample_max_date = str(
            (active_model or {}).get("training_sample_max_date") or ""
        )[:10]
        freshness_basis_date = training_data_max_date or trained_date
        latest_complete = ""
        trading_session_lag = None
        with self.connect() as conn:
            complete = conn.execute(
                """
                SELECT date
                FROM prices
                GROUP BY date
                HAVING COUNT(DISTINCT symbol) >= ?
                ORDER BY date DESC
                LIMIT 1
                """,
                (RADAR_COMPLETE_DAILY_MIN_ROWS,),
            ).fetchone()
            complete_coverage = bool(complete and complete[0])
            if complete_coverage:
                latest_complete = str(complete[0])[:10]
            else:
                fallback = conn.execute("SELECT MAX(date) FROM prices").fetchone()
                latest_complete = str(fallback[0] or "")[:10] if fallback else ""
            if freshness_basis_date and latest_complete:
                if complete_coverage:
                    lag_row = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM (
                            SELECT date
                            FROM prices
                            WHERE date > ? AND date <= ?
                            GROUP BY date
                            HAVING COUNT(DISTINCT symbol) >= ?
                        )
                        """,
                        (freshness_basis_date, latest_complete, RADAR_COMPLETE_DAILY_MIN_ROWS),
                    ).fetchone()
                else:
                    lag_row = conn.execute(
                        "SELECT COUNT(DISTINCT date) FROM prices WHERE date > ? AND date <= ?",
                        (freshness_basis_date, latest_complete),
                    ).fetchone()
                trading_session_lag = int(lag_row[0] or 0) if lag_row else 0

        gate_state = self.read_model_gate_state()
        last_rejected_at = str(gate_state.get("lastRejectedAt") or "")
        last_reject_reason = str(gate_state.get("lastRejectReason") or "")
        detail_parts = []
        if not trained_date:
            detail_parts.append("未載入生效模型")
        elif not latest_complete:
            detail_parts.append(f"生效模型 {trained_date}｜缺少完整交易日日 K 基準")
        else:
            detail_parts.append(
                f"訓練資料 {freshness_basis_date}｜落後 {trading_session_lag} 個完整交易日"
                f"（最新 {latest_complete}）"
            )
            if training_sample_max_date:
                detail_parts.append(f"可標記訓練樣本截至 {training_sample_max_date}")
            if not training_data_max_date:
                detail_parts.append("舊模型缺少訓練資料日期，暫以訓練日判斷")
        if last_rejected_at and last_rejected_at > trained_at:
            rejected = f"最近重訓 {last_rejected_at} 被品質閘門拒絕"
            if last_reject_reason:
                rejected += f"：{last_reject_reason}"
            detail_parts.append(rejected)
        return {
            "name": "AI 模型（model.pkl）",
            "latest": trained_at or None,
            "detail": "｜".join(detail_parts),
            "ok": bool(trained_date and latest_complete and trading_session_lag == 0),
            "tradingSessionLag": trading_session_lag,
            "trainingDataMaxDate": training_data_max_date or None,
            "trainingSampleMaxDate": training_sample_max_date or None,
            "freshnessBasisDate": freshness_basis_date or None,
            "latestCompletePriceDate": latest_complete or None,
            "lastRejectedAt": last_rejected_at or None,
            "lastRejectReason": last_reject_reason or None,
        }

    def data_freshness(self):
        """資料新鮮度儀表板：現有健康檢查(run_system_health)偏重「模型能不能用」，
        這裡專門看「每個資料源多久沒更新了」，一眼看出哪個源斷更。純讀取、不打網路。"""
        today = today_key()
        sources = []
        try:
            usage = self.read_finmind_usage()
            sources.append({
                "name": "FinMind 額度",
                "latest": usage.get("updatedAt"),
                "detail": f"{usage.get('calls')}/{usage.get('safeLimit')} 次"
                          + ("｜已阻擋" if usage.get("blocked") else ""),
                "ok": not usage.get("blocked"),
            })
        except Exception:
            pass
        try:
            model = self.load_model()
            sources.append(self.model_training_freshness(model))
        except Exception:
            pass
        try:
            # 大盤指數：TAIEX 是主指數，OTC 櫃買也要監控——只看 TAIEX 曾讓 OTC 斷更
            # 多日沒被抓到(2026-07-04 發現 OTC 落後 8 天)。取兩者中較舊的當判準。
            with self.connect() as conn:
                idx_latest = {
                    mk: (conn.execute("SELECT MAX(date) FROM market_prices WHERE market_key = ?", (mk,)).fetchone() or [None])[0]
                    for mk in ("TAIEX", "OTC")
                }
            parts = []
            worst_ok = True
            for mk in ("TAIEX", "OTC"):
                d = idx_latest.get(mk)
                a = calendar_days_between(d, today) if d else None
                ok = a is not None and a <= MARKET_DATA_MAX_STALE_DAYS
                worst_ok = worst_ok and ok
                parts.append(f"{mk} {d or '無'}" + ("" if ok else f"(落後{a}天)" if a is not None else "(無)"))
            sources.append({
                "name": "大盤指數",
                "latest": idx_latest.get("TAIEX"),
                "detail": "｜".join(parts),
                "ok": worst_ok,
            })
        except Exception:
            pass
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
                latest_px = row[0] if row and row[0] else None
                cnt = prev_cnt = common_cnt = prev_common_cnt = 0
                if latest_px:
                    cnt = conn.execute("SELECT COUNT(DISTINCT symbol) FROM prices WHERE date = ?", (latest_px,)).fetchone()[0]
                    # 一般股(剛好 4 碼純數字、且非 00 開頭 ETF)才是真正可選標的;整包
                    # 檔數含大量權證/ETN/ETF(6 碼佔多數),拿整包當「有幾檔股票」會誤導。
                    common_cnt = conn.execute(
                        "SELECT COUNT(DISTINCT symbol) FROM prices WHERE date = ? "
                        "AND symbol GLOB '[0-9][0-9][0-9][0-9]' AND symbol NOT LIKE '00%'",
                        (latest_px,),
                    ).fetchone()[0]
                    # 前一個交易日的覆蓋——用來判斷最新日是不是「更新沒跑完」
                    prev = (conn.execute("SELECT MAX(date) FROM prices WHERE date < ?", (latest_px,)).fetchone() or [None])[0]
                    if prev:
                        prev_cnt = conn.execute("SELECT COUNT(DISTINCT symbol) FROM prices WHERE date = ?", (prev,)).fetchone()[0]
                        prev_common_cnt = conn.execute(
                            "SELECT COUNT(DISTINCT symbol) FROM prices WHERE date = ? "
                            "AND symbol GLOB '[0-9][0-9][0-9][0-9]' AND symbol NOT LIKE '00%'",
                            (prev,),
                        ).fetchone()[0]
            age = calendar_days_between(latest_px, today) if latest_px else None
            date_ok = age is not None and age <= MARKET_DATA_MAX_STALE_DAYS
            # 覆蓋驟降：最新日檔數 < 前一交易日的一半(且前一天本來就完整)=全市場更新
            # 沒跑完,只補了持股那批。只看日期會誤判 OK(2026-07-04 發現 07-03 只 46 檔卻標 OK)。
            # 兩天必須使用同一個「一般股」口徑。prices 歷史日曾由 FinMind 帶入
            # 數千筆權證/ETN，官方當日快照則只收四碼普通股；拿整包 cnt 比
            # prev_cnt 會把衍生商品筆數下降誤報成普通股同步失敗。
            coverage_gap = (
                prev_common_cnt >= 1000
                and common_cnt < prev_common_cnt * 0.5
            )
            # 誠實揭露:對外呈現「一般股 N 檔」,整包列數放括號註明含權證/ETF,避免用
            # 6696 這種含 4584 檔衍生商品的原始列數冒充「可選股票數」。
            detail = f"最新 {latest_px}（一般股 {common_cnt} 檔｜整包 {cnt} 列含權證/ETF）" if latest_px else "無資料"
            if coverage_gap:
                detail += (
                    f"⚠️前日一般股{prev_common_cnt}檔"
                    f"（整包{prev_cnt}列）,全市場更新恐未完成"
                )
            sources.append({
                "name": "個股日K",
                "latest": latest_px,
                "detail": detail,
                "ok": date_ok and not coverage_gap,
            })
        except Exception:
            pass
        try:
            keys = (
                "last_official_close_sync_attempt_at",
                "last_official_close_sync_status",
                "last_official_close_sync_target_date",
                "last_official_close_sync_latest_date",
                "last_official_close_sync_latest_count",
                "last_official_close_sync_error",
                "last_official_close_sync_calendar_known",
                "last_official_close_sync_calendar_is_trading_day",
                "last_official_close_sync_calendar_reason",
                "last_official_close_sync_calendar_source",
            )
            with self.connect() as conn:
                placeholders = ",".join("?" for _ in keys)
                meta = {
                    str(row[0]): str(row[1] or "")
                    for row in conn.execute(
                        f"SELECT key, value FROM model_meta WHERE key IN ({placeholders})",
                        keys,
                    ).fetchall()
                }
            close_status = meta.get("last_official_close_sync_status") or ""
            if close_status:
                target_date = meta.get("last_official_close_sync_target_date") or ""
                latest_date = meta.get("last_official_close_sync_latest_date") or ""
                latest_count = meta.get("last_official_close_sync_latest_count") or "0"
                status_labels = {
                    "ready": "當日官方收盤資料已完整",
                    "waiting": "等待官方發布當日收盤資料",
                    "scheduled_holiday": "官方行事曆確認休市，保留前一交易日",
                    "previous_trading_day": "舊版未確認交易日，不能視為成功",
                    "failed": "官方收盤資料同步失敗",
                }
                detail = (
                    f"{status_labels.get(close_status, close_status)}｜"
                    f"目標 {target_date or '-'}｜最新 {latest_date or '-'}（{latest_count} 列）"
                )
                calendar_reason = meta.get("last_official_close_sync_calendar_reason") or ""
                calendar_source = meta.get("last_official_close_sync_calendar_source") or ""
                if calendar_reason:
                    detail += f"｜交易日曆 {calendar_reason}"
                if calendar_source:
                    detail += f"（{calendar_source}）"
                close_error = meta.get("last_official_close_sync_error") or ""
                if close_error:
                    detail += f"｜{close_error[:160]}"
                now_value = dt.datetime.now()
                waiting_overdue = (
                    close_status == "waiting"
                    and target_date == today
                    and (now_value.hour * 60 + now_value.minute) >= (18 * 60 + 20)
                )
                close_ok = close_status in {"ready", "scheduled_holiday"} or (
                    close_status == "waiting" and not waiting_overdue
                )
                sources.append({
                    "name": "收盤官方全市場同步",
                    "latest": meta.get("last_official_close_sync_attempt_at") or None,
                    "detail": detail,
                    "ok": close_ok,
                })
        except Exception:
            pass
        try:
            radar_validity = self.current_radar_decision_validity(current_date=today)
            invalid_reasons = radar_validity.get("invalidReasons") or []
            detail = radar_validity.get("summary") or "雷達決策資料無效"
            if invalid_reasons:
                detail += "｜所有買進建議已停用"
            sources.append({
                "name": "雷達決策資料",
                "latest": radar_validity.get("scanDate"),
                "detail": detail,
                "ok": radar_validity.get("validForTrading") is True,
            })
        except Exception as exc:
            sources.append({
                "name": "雷達決策資料",
                "latest": None,
                "detail": f"無法驗證：{str(exc)[:160]}｜所有買進建議已停用",
                "ok": False,
            })
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT MAX(updated_at) FROM realtime_flow_staging WHERE date = ?", (today,)).fetchone()
            latest_tick = row[0] if row and row[0] else None
            # tick 只有盤中才有，盤外沒有不算異常(ok=True)
            sources.append({
                "name": "即時主力大單（tick）",
                "latest": latest_tick,
                "detail": f"最後更新 {str(latest_tick)[11:16]}" if latest_tick else "今日尚無（盤中才有）",
                "ok": True,
            })
        except Exception:
            pass
        overall_ok = all(s.get("ok", True) for s in sources)
        return {"ok": True, "overallOk": overall_ok, "checkedAt": now_text(), "sources": sources}

    def save_brain_v2_snapshot(self, decision):
        """把 build_brain_decision() 的回傳結果存一份快照，讓未來累積夠多天數
        後，可以回頭比對「當初每個分量給的分數」跟「後續實際走勢」，藉此驗證
        目前 Brain v2 的權重配置是不是真的合理，而不是只能憑經驗設數字。"""
        if not decision or not decision.get("ok"):
            return False
        bv2 = decision.get("brainV2") or {}
        components = {row.get("key"): row for row in (bv2.get("components") or [])}

        def component_score(key):
            row = components.get(key) or {}
            score = row.get("score")
            return None if score is None else float(score)

        soft_gate = bv2.get("softGate") or {}
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO brain_v2_snapshots (
                    created_at, symbol, price_date, context, engine_version,
                    v2_score, entry_threshold, data_confidence_threshold, entry_allowed,
                    formal_model_score, kline_score, volume_score, market_score,
                    chip_money_score, fundamental_score, strategy_backtest_score,
                    tradingview_score, risk_score, data_confidence_score,
                    required_component_failures, close,
                    soft_gate_score, soft_gate_penalty, soft_gate_entry_allowed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, price_date, context) DO UPDATE SET
                    created_at = excluded.created_at,
                    engine_version = excluded.engine_version,
                    v2_score = excluded.v2_score,
                    entry_threshold = excluded.entry_threshold,
                    data_confidence_threshold = excluded.data_confidence_threshold,
                    entry_allowed = excluded.entry_allowed,
                    formal_model_score = excluded.formal_model_score,
                    kline_score = excluded.kline_score,
                    volume_score = excluded.volume_score,
                    market_score = excluded.market_score,
                    chip_money_score = excluded.chip_money_score,
                    fundamental_score = excluded.fundamental_score,
                    strategy_backtest_score = excluded.strategy_backtest_score,
                    tradingview_score = excluded.tradingview_score,
                    risk_score = excluded.risk_score,
                    data_confidence_score = excluded.data_confidence_score,
                    required_component_failures = excluded.required_component_failures,
                    close = excluded.close,
                    soft_gate_score = excluded.soft_gate_score,
                    soft_gate_penalty = excluded.soft_gate_penalty,
                    soft_gate_entry_allowed = excluded.soft_gate_entry_allowed
            """, (
                now_text(), decision.get("symbol"), decision.get("date"), decision.get("context"),
                decision.get("engineVersion"),
                bv2.get("score"), bv2.get("entryThreshold"), bv2.get("dataConfidenceThreshold"),
                int(bool(bv2.get("entryAllowed"))),
                component_score("formalModel"), component_score("kline"), component_score("volume"),
                component_score("market"), component_score("chipMoney"), component_score("fundamental"),
                component_score("strategyBacktest"), component_score("tradingviewSignal"),
                component_score("risk"), component_score("dataConfidence"),
                json.dumps(bv2.get("requiredComponentFailures") or [], ensure_ascii=False),
                (decision.get("prediction") or {}).get("close"),
                soft_gate.get("softScore"), soft_gate.get("penalty"),
                int(bool(soft_gate.get("softEntryAllowed"))),
            ))
        return True

    def get_previous_brain_v2_snapshot(self, symbol, price_date, context=None):
        """找出「今天以前」最近一筆有記錄的 Brain v2 快照，給分數趨勢比較用。
        優先比對同一個 context；找不到同 context 紀錄時，會退而使用任何
        context 的最近一筆，避免因為呼叫情境不同就完全看不到趨勢。"""
        if not symbol or not price_date:
            return None
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = None
            if context:
                row = conn.execute(
                    """
                    SELECT price_date, v2_score, formal_model_score, entry_allowed
                    FROM brain_v2_snapshots
                    WHERE symbol = ? AND context = ? AND price_date < ?
                    ORDER BY price_date DESC LIMIT 1
                    """,
                    (symbol, context, price_date),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT price_date, v2_score, formal_model_score, entry_allowed
                    FROM brain_v2_snapshots
                    WHERE symbol = ? AND price_date < ?
                    ORDER BY price_date DESC LIMIT 1
                    """,
                    (symbol, price_date),
                ).fetchone()
        return dict(row) if row else None

    def save_monster_rule_snapshot(self, symbol, price_date, rule_result):
        """存一份妖股短線規則引擎的判斷快照。跟 Brain v2 的 soft_gate 是同一件事
        (先收集資料、不影響既有 buyAllowed 判斷，等累積幾週資料後才回頭比對
        命中率)，合併存進 brain_v2_snapshots 同一列(context 固定 monster，
        規則引擎本來就是「今天要不要追這檔妖股」的進場情境資料，跟
        portfolio_exit 無關)。用 ON CONFLICT 只更新 rule_* 欄位，不動 Brain v2
        那邊已經寫入的分量分數——不管哪個先寫，兩者都能安全疊加進同一列。"""
        if not symbol or not price_date or not rule_result:
            return False
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO brain_v2_snapshots (
                    created_at, symbol, price_date, context,
                    rule_action, rule_vetoed, rule_veto_reason, rule_overheated, rule_rules, rule_bonus_tags
                ) VALUES (?, ?, ?, 'monster', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, price_date, context) DO UPDATE SET
                    rule_action = excluded.rule_action,
                    rule_vetoed = excluded.rule_vetoed,
                    rule_veto_reason = excluded.rule_veto_reason,
                    rule_overheated = excluded.rule_overheated,
                    rule_rules = excluded.rule_rules,
                    rule_bonus_tags = excluded.rule_bonus_tags
            """, (
                now_text(), symbol, price_date, rule_result.get("action"),
                int(bool(rule_result.get("vetoed"))), rule_result.get("vetoReason"),
                int(bool(rule_result.get("overheated"))),
                json.dumps(rule_result.get("rules") or [], ensure_ascii=False),
                json.dumps(rule_result.get("bonusTags") or [], ensure_ascii=False),
            ))
        return True

    def update_outcomes(self):
        # 舊版在同一條連線上「逐筆 UPDATE + 每筆重新載入該股全部歷史」：
        # 第一筆 UPDATE 就取得全域寫入鎖(LockedConnection 的設計)並一路持有到
        # 整個迴圈結束——待結算的 predictions 累積到數千筆時(15:10 批量預測每天
        # 產生數百筆、每筆要等 target_horizon 天後才能結算)，寫入鎖會被扣住好幾
        # 分鐘，其他程序/執行緒的寫入全部撞 database is locked(07/02 實際發生)。
        # 改為三段式：(1)純讀取待結算清單 (2)無鎖狀態下按股票分組載入歷史並計算
        # (3)一次 executemany 短交易寫回。同時把「每筆 prediction 重載一次全歷史」
        # 改成「每檔股票載一次」(實測 5130 筆 pending 只涉及 865 檔)。
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            predictions = [dict(row) for row in conn.execute(
                "SELECT * FROM predictions WHERE hit IS NULL ORDER BY id"
            ).fetchall()]

        by_symbol = {}
        for prediction in predictions:
            by_symbol.setdefault(prediction["symbol"], []).append(prediction)

        rows_by_symbol = {symbol: self.load_price_rows(symbol) for symbol in by_symbol}
        updates = self.compute_prediction_outcomes(predictions, rows_by_symbol)

        if updates:
            with self.connect() as conn:
                # AND hit IS NULL：讀取與寫回之間若有另一個程序也在結算
                # (例如手動跑 daily_update.py)，不要無條件覆寫別人已寫入的結果。
                conn.executemany("""
                    UPDATE predictions
                    SET outcome_date = ?, outcome_close = ?, outcome_return = ?, hit = ?
                    WHERE id = ? AND hit IS NULL
                """, updates)
        return len(updates)

    def compute_prediction_outcomes(self, predictions, rows_by_symbol):
        """純計算：不碰資料庫、不碰網路，方便直接用合成資料做回歸測試。
        回傳 executemany 用的 (outcome_date, outcome_close, outcome_return,
        hit, id) tuple 清單；還沒到結算日或資料不足的 prediction 直接略過。

        新 short-profit-net-v1 直接重算與訓練一致的 3/5/10 日扣成本、風險
        調整目標。舊列仍保留「窗內曾達 target_return」定義，歷史不重寫。
        """
        updates = []
        date_index_cache = {}
        for prediction in predictions:
            rows = rows_by_symbol.get(prediction["symbol"]) or []
            if not rows:
                continue
            if prediction["symbol"] not in date_index_cache:
                dates = [row["date"] for row in rows]
                date_index_cache[prediction["symbol"]] = (dates, {date: idx for idx, date in enumerate(dates)})
            dates, date_index = date_index_cache[prediction["symbol"]]
            start_index = date_index.get(prediction["price_date"])
            if start_index is None:
                start_index = next((idx for idx, date in enumerate(dates) if date >= prediction["price_date"]), -1)
            target_index = start_index + prediction["target_horizon"]
            if start_index < 0 or target_index >= len(rows):
                continue
            if not prediction["close"]:
                # 髒資料防呆：close=0/None 的舊 prediction 不能讓整批結算
                # 除以零炸掉，跳過留待人工清理。
                continue
            try:
                target_type = str(prediction["target_type"] or "")
            except (KeyError, IndexError):
                target_type = ""
            if target_type == SHORT_PROFIT_TARGET_TYPE:
                target_result = self.short_term_target(rows, start_index)
                if not target_result:
                    continue
                target = rows[start_index + SHORT_PROFIT_MAX_HORIZON_DAYS]
                updates.append((
                    target["date"], target["close"],
                    target_result["future_return"], target_result["y"],
                    prediction["id"],
                ))
                continue
            target = rows[target_index]
            entry = prediction["close"]
            actual_return = (target["close"] - entry) / entry  # outcome_return 保持「持有到第 N 天」實現報酬
            # 命中 = 窗內[start+1 .. target_index]任一天收盤達 +target_return(對齊訓練標籤停利)
            hit = 0
            for k in range(start_index + 1, target_index + 1):
                kc = rows[k]["close"]
                if kc and (kc - entry) / entry >= prediction["target_return"]:
                    hit = 1
                    break
            updates.append((target["date"], target["close"], actual_return, hit, prediction["id"]))
        return updates

    def full_daily_update(self, symbols=None, force_refresh=False, training_symbols=None, train=True):
        symbols = symbols or DEFAULT_SYMBOLS
        training_symbols = training_symbols or symbols
        # 每日更新先以 TWSE/TPEx 全市場日K補齊上一交易日收盤，不能只更新
        # 持股而讓雷達看到「日期新、覆蓋卻只有少數股票」的半套資料。籌碼與
        # 財報等慢資料仍由下方逐檔 FinMind 更新；這一步只負責官方 OHLCV/估值。
        official_snapshot = self.sync_official_daily_snapshot()
        freshness = self.data_freshness()
        freshness_sources = {
            str(source.get("name") or ""): source
            for source in (freshness.get("sources") or [])
            if isinstance(source, dict)
        }
        stock_daily_health = freshness_sources.get("個股日K") or {}
        official_snapshot_ok = bool(
            official_snapshot.get("written")
            and stock_daily_health.get("ok")
        )
        market = self.update_market_data()
        # symbols 是使用者實際持股(通常 10~40 檔)，不是全市場掃描，
        # 多抓當沖/借券兩個擴充資料集對 FinMind 額度幾乎沒有負擔，
        # 所以每日更新走完整版；妖股全市場掃描(scan_monster_scores)
        # 維持預設 include_extended=False，避免上千檔 symbol 把額度用爆。
        # force_refresh=True：強制忽略「今天已抓過」的快取短路，立刻重抓+重訓，
        # 給使用者在同一天內想馬上驗證修復或重訓時用，不必等隔天快取自然過期。
        counts = self.update_prices(symbols, include_extended=True, force_refresh=force_refresh)
        price_fetch_errors = dict(self._last_price_fetch_errors)
        if train:
            model = self.train_model(training_symbols)
            predictions = []
            prediction_errors = []
            for symbol in symbols:
                try:
                    predictions.append(self.predict_symbol(symbol, save=True))
                except Exception as exc:
                    prediction_errors.append({"symbol": symbol, "error": str(exc)})
            updated_outcomes = self.update_outcomes()
        else:
            # 早上/盤中的每日工作只同步真實資料；不載入模型、不預測、不結算。
            # 獨立模型循環在收盤後自行重訓、批量預測並更新績效。
            model = {}
            predictions = []
            prediction_errors = []
            updated_outcomes = 0
        return {
            "ok": official_snapshot_ok,
            "error": (
                "官方全市場日K同步未通過覆蓋新鮮度檢查"
                if not official_snapshot_ok else None
            ),
            "officialSnapshot": {
                **official_snapshot,
                "coverage": stock_daily_health,
            },
            "updatedRows": counts,
            "priceFetchErrors": price_fetch_errors,
            "market": market,
            "model": self.public_model(model),
            "trainingSymbols": training_symbols,
            "trainingSymbolCount": len(training_symbols),
            "portfolioSymbols": symbols,
            "portfolioSymbolCount": len(symbols),
            "predictions": predictions,
            "predictionErrors": prediction_errors,
            "updatedOutcomes": updated_outcomes,
        }

    def public_model(self, model):
        extra = model.get("extra_models") or {}
        extra_public = {
            "available": bool(extra.get("available")),
            "models": extra.get("models") or [],
            "xgboostMetrics": (extra.get("xgboost") or {}).get("metrics"),
            "lightgbmMetrics": (extra.get("lightgbm") or {}).get("metrics"),
            "gradientBoostingMetrics": (extra.get("gradient_boosting") or {}).get("metrics"),
            "isolationForestMetrics": (extra.get("isolation_forest") or {}).get("metrics"),
            "learningToRankMetrics": (extra.get("learning_to_rank") or {}).get("metrics"),
            "shortHorizonReturnMetrics": {
                horizon: (item or {}).get("metrics")
                for horizon, item in (extra.get("short_horizon_returns") or {}).items()
            },
            "errors": {
                key: value for key, value in extra.items()
                if key.endswith("_error")
            },
        }
        return {
            "version": model.get("version"),
            "type": model.get("model_type"),
            "targetType": model.get("target_type"),
            "targetSpec": model.get("target_spec"),
            "policyHash": model.get("policy_hash"),
            "targetPolicyHash": model.get("target_policy_hash"),
            "dataPolicy": model.get("data_policy"),
            "trainedAt": model.get("trained_at"),
            "trainingDataMaxDate": model.get("training_data_max_date"),
            "trainingSampleMaxDate": model.get("training_sample_max_date"),
            "trainingDateMetadataSource": model.get("training_date_metadata_source"),
            "symbols": model.get("symbols"),
            "samples": model.get("samples"),
            "validationSize": model.get("validation_size"),
            "positiveWeight": model.get("positive_weight"),
            "threshold": self.buy_signal_threshold(),
            "signalThreshold": self.buy_signal_threshold(),
            "signalMethod": "short-profit probability + risk-adjusted 3/5/10-day net-return rank + anomaly/setup adjustment; monster setup is not required",
            "metrics": model.get("metrics"),
            "progress": self.model_progress(model.get("metrics") or {}),
            "extraModels": extra_public,
            "featureNames": model.get("feature_names"),
            "modelPath": str(self.model_path),
            # 2026-07-04 稽核修復：品質閘門擋下新模型時，train_model 回傳的 dict 帶
            # gateRejected/gateReason(且 model.pkl 上仍是舊模型)，但 public_model 原本
            # 重建 dict 時漏掉這兩個鍵——full_daily_update 寫進 latest.json 排程log 的
            # result.model 區塊只看得到「這個被擋下的新模型」的 metrics/version，卻看不出
            # 它其實沒上線、線上還是舊模型，維運排查時容易誤判成新模型已生效。補透傳
            # (未被擋下時 gateRejected=False、gateReason=None)。純可觀測性欄位，不影響
            # /api/ml/status 與 predict_symbol(那兩處都重讀磁碟的 model.pkl，本來就正確)。
            "gateRejected": bool(model.get("gateRejected")),
            "gateReason": model.get("gateReason"),
        }

    def model_progress(self, metrics):
        def value(key, default=None):
            raw = metrics.get(key, default)
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        precision = value("precision", 0)
        average_return = value("averageTradeReturn", 0)
        profit_factor = value("profitFactor", 0)
        max_drawdown = value("maxDrawdown", 0)
        stages = [
            {
                "stage": 1,
                "title": "Precision 先到 55-60%",
                "metric": "precision",
                "current": precision,
                "target": 0.55,
                "passed": precision >= 0.55,
                "label": "喊買命中率",
            },
            {
                "stage": 2,
                "title": "每筆平均報酬轉正",
                "metric": "averageTradeReturn",
                "current": average_return,
                "target": 0.0,
                "passed": average_return > 0,
                "label": "平均每筆報酬",
            },
            {
                "stage": 3,
                "title": "Profit factor > 1.5",
                "metric": "profitFactor",
                "current": profit_factor,
                "target": 1.5,
                "passed": profit_factor >= 1.5,
                "label": "賺賠比",
            },
            {
                "stage": 4,
                "title": "最大回撤控制在 -20% 內",
                "metric": "maxDrawdown",
                "current": max_drawdown,
                "target": -0.20,
                "passed": max_drawdown >= -0.20,
                "label": "最大回撤",
            },
            {
                "stage": 5,
                "title": "追求 70-80% 高把握訊號",
                "metric": "precision",
                "current": precision,
                "target": 0.70,
                "passed": precision >= 0.70 and average_return > 0 and profit_factor >= 1.5 and max_drawdown >= -0.20,
                "label": "高把握命中率",
            },
        ]
        completed = 0
        for item in stages[:4]:
            if not item["passed"]:
                break
            completed += 1
        passed_count = sum(1 for item in stages[:4] if item["passed"])
        next_stage = next((item for item in stages if not item["passed"]), stages[-1])
        return {
            "completedStages": completed,
            "passedStages": passed_count,
            "currentStage": next_stage["stage"],
            "nextGoal": next_stage,
            "highConfidenceReady": stages[-1]["passed"],
            "stages": stages,
        }

    def status(self):
        model, model_load_error = self.load_model_with_error()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM model_meta").fetchall()}
            price_count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            market_count = conn.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0]
            prediction_count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            prediction_completed_count = conn.execute("SELECT COUNT(*) FROM predictions WHERE hit IS NOT NULL").fetchone()[0]
            prediction_pending_count = conn.execute("SELECT COUNT(*) FROM predictions WHERE hit IS NULL").fetchone()[0]
            trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            hit_rows = conn.execute(
                "SELECT hit FROM predictions "
                "WHERE hit IS NOT NULL AND target_type = ? "
                "ORDER BY id DESC LIMIT 60",
                (SHORT_PROFIT_TARGET_TYPE,),
            ).fetchall()
        hit_rate = sum(row["hit"] for row in hit_rows) / len(hit_rows) if hit_rows else None
        progress = self.model_progress(model.get("metrics") or {}) if model else None
        data_health = self.data_health(meta)
        model_training_freshness = (
            self.model_training_freshness(model) if model else {
                "ok": False,
                "detail": model_load_error or "model.pkl not found",
            }
        )
        model_health = {
            "ok": bool(model and model_training_freshness.get("ok")),
            "mode": "independent",
            "reason": (
                "" if model and model_training_freshness.get("ok")
                else model_training_freshness.get("detail")
                if model else f"獨立模型不可用：{model_load_error or 'model.pkl not found'}"
            ),
            "status": (
                "ready" if model and model_training_freshness.get("ok")
                else "data_stale" if model else "model_unavailable"
            ),
            "updatedAt": now_text(),
        }
        def meta_json(key):
            try:
                value = json.loads(meta.get(key) or "[]")
            except (TypeError, ValueError):
                value = []
            return value if isinstance(value, list) else []

        def meta_integer(key):
            value = self.safe_float(meta.get(key))
            return int(value) if value is not None else 0

        batch_prediction_health = {
            "completedAt": meta.get("last_batch_predictions_completed_at") or None,
            "symbolsTotal": meta_integer("last_batch_predictions_total"),
            "eligible": meta_integer("last_batch_predictions_eligible"),
            "saved": meta_integer("last_batch_predictions_saved"),
            "alreadyPresent": meta_integer("last_batch_predictions_existing"),
            "ineligibleCount": meta_integer("last_batch_predictions_ineligible"),
            "errorCount": meta_integer("last_batch_predictions_errors"),
            "ineligible": meta_json("last_batch_predictions_ineligible_json"),
            "errors": meta_json("last_batch_predictions_errors_json"),
            "paperSignalErrors": meta_json("last_batch_predictions_paper_signal_errors_json"),
        }
        try:
            training_progress = json.loads(meta.get("training_progress") or "null")
        except Exception:
            training_progress = None
        return {
            "ok": True,
            "trainingProgress": training_progress,
            "dbPath": str(self.db_path),
            "modelPath": str(self.model_path),
            "modelEnvPath": str(self.model_env_path),
            "modelExists": self.model_path.exists(),
            "modelLoadError": model_load_error,
            "model": self.public_model(model) if model else None,
            "modelEnv": self.read_model_env(),
            "runtimeEnv": current_model_environment(),
            "modelProgress": progress,
            "modelHealth": model_health,
            "modelTrainingFreshness": model_training_freshness,
            "batchPredictionHealth": batch_prediction_health,
            "dataSourcePriority": DATA_SOURCE_PRIORITY,
            "finmindUsage": self.read_finmind_usage(),
            "meta": meta,
            "dataHealth": data_health,
            "priceRows": price_count,
            "marketRows": market_count,
            "predictionRows": prediction_count,
            "predictionCompletedRows": prediction_completed_count,
            "predictionPendingRows": prediction_pending_count,
            "recentHitSampleCount": len(hit_rows),
            "recentHitRule": "次日開盤進場後，3/5/10 日加權淨報酬扣風險懲罰是否為正",
            "tradeRows": trade_count,
            "recentHitRate": hit_rate,
            "adaptiveThreshold": self.buy_signal_threshold(),
            "winProbabilityThreshold": self.win_probability_display_threshold(),
            "featureImportances": (
                (model.get("extra_models") or {}).get("feature_importances")
                if model else None
            ),
        }

    def data_health(self, meta):
        today = dt.date.today().isoformat()
        # 模型健康與正式股票分析完全分離。此處只判斷價格資料工作與資料日期，
        # last_system_health_* 即使是模型失敗或過期，也不能封鎖雷達、持股或提醒。
        status = str(meta.get("last_daily_job_status") or "")
        updated_at = str(meta.get("last_daily_job_at") or "")
        error = str(meta.get("last_daily_job_error") or "")
        if updated_at.startswith(today) and status == "failed":
            return {
                "ok": False,
                "mode": "observe_only",
                "reason": f"今日每日更新失敗：{error or '未知錯誤'}",
                "status": status,
                "updatedAt": updated_at,
            }
        # 2026-07-08 改：不再要求「完整每日更新(舊版含重訓)今天成功」才放行。
        # 重訓已是純參考、且移到收盤後 14:30 執行,盤中的賣出/觀察決策只需要
        # 「今天的價格資料是新鮮的」。舊版綁 last_daily_job,只要早上那次沉重的
        # 每日更新沒在窗口跑完(實測會因排程時機/重啟而整個沒跑到),就把盤中決策
        # 鎖進 observe_only 一整天——即使 last_data_update 明明是今天、資料完全新鮮。
        # 改看 last_data_update:每日更新/盤中即時報價/資料缺口修復任一刷新價格資料
        # 都算資料就緒。上面「今日每日更新明確失敗」的示警仍保留(真的抓資料失敗要擋)。
        data_updated_at = str(meta.get("last_data_update") or "")
        if not data_updated_at.startswith(today):
            return {
                "ok": False,
                "mode": "observe_only",
                "reason": "今日尚未更新價格資料，暫停停利與軟性賣出通知（跌破絕對停損仍會照常通知）",
                "status": status,
                "updatedAt": updated_at or data_updated_at,
            }
        return {
            "ok": True,
            "mode": "normal",
            "reason": "",
            "status": status,
            "updatedAt": updated_at if updated_at.startswith(today) else data_updated_at,
        }

    def list_predictions(self, limit=80):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM predictions
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def record_trade(self, payload):
        symbol = str(payload.get("symbol", "")).replace(".TWO", "").replace(".TW", "").strip()
        side = str(payload.get("side", "")).upper()
        price = float(payload.get("price", 0) or 0)
        shares = int(payload.get("shares", 0) or 0)
        if not symbol or side not in {"BUY", "SELL"} or price <= 0 or shares <= 0:
            raise ValueError("symbol, side, price, shares are required")
        status = str(payload.get("status", "paper") or "paper").strip()
        explicit_buy_at = payload.get("buyAt") or payload.get("buy_at") or payload.get("buyDate")
        buy_at = (
            explicit_buy_at
            or (now_text() if side == "BUY" and status in {"filled", "partial", "closed"} else None)
        )
        broker_order_id = str(payload.get("brokerOrderId") or payload.get("broker_order_id") or "").strip()
        broker_seqno = str(payload.get("brokerSeqno") or payload.get("broker_seqno") or "").strip()
        broker_ordno = str(payload.get("brokerOrdno") or payload.get("broker_ordno") or "").strip()
        broker_dseq = str(payload.get("brokerDseq") or payload.get("broker_dseq") or "").strip()
        execution_price = self.safe_float(
            payload.get("executionPrice") or payload.get("execution_price")
        )
        if not execution_price and status in {"filled", "partial", "closed"}:
            execution_price = price
        trade_condition = str(
            payload.get("tradeCondition") or payload.get("trade_condition") or ""
        ).strip()
        execution_evidence_source = str(
            payload.get("executionEvidenceSource")
            or payload.get("execution_evidence_source")
            or ""
        ).strip()
        strategy_horizon = normalize_strategy_horizon(
            payload.get("strategyHorizon") or payload.get("strategy_horizon")
        ) if side == "BUY" else "unknown"
        strategy_horizon_source = str(
            payload.get("strategyHorizonSource")
            or payload.get("strategy_horizon_source")
            or ("order_entry" if strategy_horizon != "unknown" else "not_provided")
        ).strip()
        strategy_horizon_locked_at = None
        if side == "BUY" and strategy_horizon != "unknown" and status in {"filled", "partial", "closed"}:
            strategy_horizon_locked_at = str(
                payload.get("strategyHorizonLockedAt")
                or payload.get("strategy_horizon_locked_at")
                or payload.get("filledAt")
                or payload.get("filled_at")
                or buy_at
                or now_text()
            ).strip()
        entry_cost_includes_buy_fee = bool(
            payload.get("entryCostIncludesBuyFee") is True
            or payload.get("entry_cost_includes_buy_fee") is True
            or payload.get("entryCostIncludesBuyFee") == 1
            or payload.get("entry_cost_includes_buy_fee") == 1
        ) if side == "BUY" else False
        broker_cost_amount = self.safe_float(
            payload.get("brokerCostAmount") or payload.get("broker_cost_amount")
        )
        source_lot_key = str(
            payload.get("sourceLotKey") or payload.get("source_lot_key") or ""
        ).strip()
        with self.connect() as conn:
            cursor = conn.execute("""
                INSERT INTO trades (
                    created_at, buy_at, symbol, side, price, shares, signal, stop_price, target_price, status, note,
                    broker_order_id, broker_seqno, broker_ordno, strategy_horizon,
                    strategy_horizon_source, strategy_horizon_locked_at,
                    entry_cost_includes_buy_fee, broker_cost_amount, source_lot_key,
                    execution_price, broker_dseq, trade_condition, execution_evidence_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_text(), buy_at, symbol, side, price, shares, payload.get("signal"),
                payload.get("stopPrice"), payload.get("targetPrice"), status, payload.get("note"),
                broker_order_id or None, broker_seqno or None, broker_ordno or None,
                strategy_horizon, strategy_horizon_source or None, strategy_horizon_locked_at,
                int(entry_cost_includes_buy_fee), broker_cost_amount, source_lot_key or None,
                execution_price, broker_dseq or None, trade_condition or None,
                execution_evidence_source or None,
            ))
        return {
            "ok": True,
            "id": cursor.lastrowid,
            "strategyHorizon": strategy_horizon,
            "strategyHorizonLocked": bool(strategy_horizon_locked_at),
            "entryCostIncludesBuyFee": entry_cost_includes_buy_fee,
            "executionPrice": execution_price,
        }

    def list_trades(self, limit=80, include_paper=True):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            where = "" if include_paper else "WHERE status != 'paper'"
            rows = conn.execute(
                f"""
                SELECT trades.*,
                       (SELECT COUNT(1)
                        FROM trade_execution_evidence evidence
                        WHERE evidence.trade_id = trades.id) AS execution_evidence_count
                FROM trades {where}
                ORDER BY trades.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["buyDate"] = str(record.get("filled_at") or record.get("buy_at") or "")[:10]
            record["sellDate"] = str(record.get("exit_at") or "")[:10]
            record["strategyHorizon"] = normalize_strategy_horizon(record.get("strategy_horizon"))
            record["entryCostIncludesBuyFee"] = bool(record.get("entry_cost_includes_buy_fee"))
            price = float(record.get("execution_price") or record.get("price") or 0)
            exit_price = float(record.get("exit_price") or 0)
            shares = int(record.get("filled_shares") or record.get("shares") or 0)
            broker_cost_amount = self.safe_float(record.get("broker_cost_amount"))
            record["executionPrice"] = price if price > 0 else None
            record["costBasisAmount"] = broker_cost_amount
            record["costBasisPrice"] = (
                round(broker_cost_amount / shares, 6)
                if broker_cost_amount is not None and broker_cost_amount > 0 and shares > 0
                else None
            )
            record["costBasisAdjustment"] = (
                round(broker_cost_amount - price * shares, 2)
                if broker_cost_amount is not None and broker_cost_amount > 0 and price > 0 and shares > 0
                else None
            )
            explicit_pnl_pct = self.safe_float(record.get("pnl_pct"))
            record["pnlPct"] = (
                round(explicit_pnl_pct, 2)
                if explicit_pnl_pct is not None
                else round((exit_price / price - 1) * 100, 2)
                if price > 0 and exit_price > 0
                else None
            )
            record["executionEvidenceCount"] = int(record.get("execution_evidence_count") or 0)
            records.append(record)
        return records

    def fifo_open_trade_lots(self, symbol, position_shares=None):
        """Reconcile the broker position against real BUY fills in FIFO order.

        ``created_at`` is intentionally never treated as a buy date.  A lot is
        eligible only after a real/manual filled state, and uncovered broker
        shares remain explicit so time exits cannot fire on guessed dates.
        """
        code = str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
        try:
            requested = max(0, int(float(position_shares))) if position_shares is not None else None
        except (TypeError, ValueError):
            requested = None
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT *
                FROM trades
                WHERE symbol = ?
                  AND side = 'BUY'
                  AND status != 'paper'
                  AND exit_price IS NULL
                  AND exit_at IS NULL
                  AND (status IN ('filled', 'partial') OR COALESCE(filled_shares, 0) > 0)
                ORDER BY
                    CASE WHEN COALESCE(filled_at, buy_at) IS NULL THEN 1 ELSE 0 END,
                    COALESCE(filled_at, buy_at) ASC,
                    id ASC
            """, (code,)).fetchall()
        lots = []
        remaining = requested
        for row in rows:
            available = int(row["filled_shares"] or row["shares"] or 0)
            if available <= 0 or remaining == 0:
                continue
            use_shares = min(available, remaining) if remaining is not None else available
            true_buy_at = str(row["filled_at"] or row["buy_at"] or "").strip()
            execution_price = float(row["execution_price"] or row["price"] or 0)
            broker_cost_amount = self.safe_float(row["broker_cost_amount"])
            entry_cost_amount = (
                broker_cost_amount * use_shares / available
                if broker_cost_amount is not None and broker_cost_amount > 0 and available > 0
                else None
            )
            lots.append({
                "tradeId": int(row["id"]),
                "shares": use_shares,
                "price": execution_price,
                "executionPrice": execution_price,
                "buyDate": true_buy_at[:10] or None,
                "buyAt": true_buy_at or None,
                "buyDateKnown": bool(true_buy_at),
                "strategyHorizon": normalize_strategy_horizon(row["strategy_horizon"]),
                "strategyHorizonSource": row["strategy_horizon_source"] or "",
                "strategyHorizonLockedAt": row["strategy_horizon_locked_at"],
                "brokerOrderId": row["broker_order_id"],
                "brokerSeqno": row["broker_seqno"],
                "brokerOrdno": row["broker_ordno"],
                "brokerDseq": row["broker_dseq"],
                "entryCostIncludesBuyFee": bool(row["entry_cost_includes_buy_fee"]),
                "brokerCostAmount": row["broker_cost_amount"],
                "entryCostAmount": round(entry_cost_amount, 4) if entry_cost_amount is not None else None,
                "costBasisPrice": (
                    round(entry_cost_amount / use_shares, 6)
                    if entry_cost_amount is not None and use_shares > 0
                    else None
                ),
                "tradeCondition": row["trade_condition"],
                "executionEvidenceSource": row["execution_evidence_source"],
                "sourceLotKey": row["source_lot_key"],
            })
            if remaining is not None:
                remaining -= use_shares
        covered = sum(int(lot["shares"]) for lot in lots)
        unknown = max(0, (requested or 0) - covered) if requested is not None else 0
        return {
            "symbol": code,
            "positionShares": requested,
            "coveredShares": covered,
            "unknownShares": unknown,
            "fullyReconciled": requested is None or unknown == 0,
            "lots": lots,
        }

    def revert_unintended_legacy_horizon_locks(self, apply=True):
        """Revert the removed legacy bulk lock while retaining an append-only audit."""
        changed_at = now_text()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            marker = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?",
                (LEGACY_HORIZON_REVERT_META_KEY,),
            ).fetchone()
            if marker:
                return {
                    "ok": True,
                    "applied": False,
                    "alreadyApplied": True,
                    "changed": 0,
                    "marker": marker[0],
                }
            rows = conn.execute(
                """
                SELECT id, symbol, strategy_horizon, strategy_horizon_source,
                       strategy_horizon_locked_at, buy_at, filled_at, price,
                       shares, filled_shares
                FROM trades
                WHERE side = 'BUY'
                  AND status != 'paper'
                  AND exit_price IS NULL
                  AND exit_at IS NULL
                  AND strategy_horizon_source = 'manual_legacy_position_lock'
                  AND COALESCE(strategy_horizon, 'unknown') != 'unknown'
                ORDER BY id
                """
            ).fetchall()
            items = [dict(row) for row in rows]
            if not apply:
                return {
                    "ok": True,
                    "applied": False,
                    "alreadyApplied": False,
                    "changed": 0,
                    "candidateCount": len(items),
                    "items": items,
                }

            audit_id = None
            batch_key = None
            if items:
                audit_payload = {
                    "changedAt": changed_at,
                    "source": "legacy_manual_horizon_revert_v1",
                    "reason": "舊版首頁整批策略週期已移除；無原始交易意圖證據，不得繼續把全部持股當短期",
                    "newStrategyHorizon": "unknown",
                    "newStrategyHorizonSource": "legacy_bulk_lock_reverted",
                    "items": items,
                }
                batch_key = hashlib.sha256(
                    json.dumps(
                        audit_payload, ensure_ascii=False, sort_keys=True, default=str
                    ).encode("utf-8")
                ).hexdigest()
                cursor = conn.execute(
                    """
                    INSERT INTO portfolio_horizon_lock_batches (
                        batch_key, created_at, assignment_count, payload_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        batch_key,
                        changed_at,
                        len(items),
                        json.dumps(
                            audit_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ),
                    ),
                )
                audit_id = cursor.lastrowid
                trade_ids = [int(item["id"]) for item in items]
                placeholders = ",".join("?" for _ in trade_ids)
                updated = conn.execute(
                    f"""
                    UPDATE trades
                    SET strategy_horizon = 'unknown',
                        strategy_horizon_source = 'legacy_bulk_lock_reverted',
                        strategy_horizon_locked_at = NULL
                    WHERE id IN ({placeholders})
                      AND strategy_horizon_source = 'manual_legacy_position_lock'
                      AND COALESCE(strategy_horizon, 'unknown') != 'unknown'
                    """,
                    trade_ids,
                )
                if updated.rowcount != len(trade_ids):
                    raise RuntimeError("舊版持股週期在稽核回復期間被其他操作修改，整批取消")
            marker_payload = {
                "changedAt": changed_at,
                "changed": len(items),
                "auditId": audit_id,
                "batchKey": batch_key,
            }
            self.set_meta(
                conn,
                LEGACY_HORIZON_REVERT_META_KEY,
                json.dumps(marker_payload, ensure_ascii=False, separators=(",", ":")),
            )
        return {
            "ok": True,
            "applied": True,
            "alreadyApplied": False,
            "changed": len(items),
            "auditId": audit_id,
            "batchKey": batch_key,
            "items": items,
        }

    def lock_existing_position_horizon(
        self, symbol, strategy_horizon, holding, trade_id=None,
    ):
        """One-time manual migration for positions bought before horizon locking.

        Existing known horizons are immutable.  Unknown real fill rows are
        locked in place; broker shares not covered by local fills receive an
        explicit synthetic lot with an unknown buy date, so time exits remain
        disabled instead of inventing a date.
        """
        code = str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
        horizon = normalize_strategy_horizon(strategy_horizon)
        if not code or horizon == "unknown":
            raise ValueError("必須提供股票代號與已知策略週期")
        if not isinstance(holding, dict):
            raise ValueError("必須提供目前券商持股")
        requested_trade_id = None
        if trade_id not in (None, ""):
            try:
                requested_trade_id = int(trade_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("tradeId 必須是有效成交 lot 編號") from exc
            if requested_trade_id <= 0:
                raise ValueError("tradeId 必須是有效成交 lot 編號")
        direct_shares = self.safe_float(holding.get("shares"))
        position_shares = int(direct_shares) if direct_shares and direct_shares > 0 else int(
            max(0.0, self.safe_float(holding.get("quantity")) or 0.0) * 1000
        )
        avg_price = self.safe_float(holding.get("price") or holding.get("avgPrice")) or 0.0
        if position_shares <= 0 or avg_price <= 0:
            raise ValueError("目前券商股數與平均成本必須有效")

        locked_at = now_text()
        updated_ids = []
        synthetic_id = None
        uncovered_shares = 0
        buy_date_known = False
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            # Serialize the read/check/write sequence. A double click or two
            # browser tabs must not create duplicate synthetic position lots.
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT *
                FROM trades
                WHERE symbol = ?
                  AND side = 'BUY'
                  AND status != 'paper'
                  AND exit_price IS NULL
                  AND exit_at IS NULL
                  AND (status IN ('filled', 'partial') OR COALESCE(filled_shares, 0) > 0)
                ORDER BY
                    CASE WHEN COALESCE(filled_at, buy_at) IS NULL THEN 1 ELSE 0 END,
                    COALESCE(filled_at, buy_at) ASC,
                    id ASC
            """, (code,)).fetchall()
            remaining = position_shares
            covered_shares = 0
            selected_dates = []
            for row in rows:
                available = int(row["filled_shares"] or row["shares"] or 0)
                if available <= 0 or remaining <= 0:
                    continue
                use_shares = min(available, remaining)
                covered_shares += use_shares
                remaining -= use_shares
                selected_dates.append(bool(str(row["filled_at"] or row["buy_at"] or "").strip()))
                if normalize_strategy_horizon(row["strategy_horizon"]) == "unknown":
                    updated_ids.append(int(row["id"]))
            uncovered_shares = max(0, position_shares - covered_shares)
            if requested_trade_id is not None:
                if requested_trade_id not in updated_ids:
                    raise ValueError("指定 tradeId 已鎖定或不在目前持股 lot 中，禁止覆寫")
                updated_ids = [requested_trade_id]
                uncovered_shares = 0
            elif len(updated_ids) > 1:
                raise ValueError("此股票有多個未知週期 lot，必須逐筆提供 tradeId")
            elif updated_ids and uncovered_shares > 0:
                raise ValueError("持股同時包含成交 lot 與未匯入股數，請先完成 lot 核對")
            if not updated_ids and uncovered_shares <= 0:
                raise ValueError("此持股策略週期已鎖定，不能再次修改")

            if updated_ids:
                placeholders = ",".join("?" for _ in updated_ids)
                conn.execute(f"""
                    UPDATE trades
                    SET strategy_horizon = ?,
                        strategy_horizon_source = 'manual_legacy_position_lock',
                        strategy_horizon_locked_at = ?
                    WHERE id IN ({placeholders})
                      AND side = 'BUY'
                      AND (strategy_horizon IS NULL OR strategy_horizon = '' OR strategy_horizon = 'unknown')
                """, (horizon, locked_at, *updated_ids))
            if uncovered_shares > 0:
                cursor = conn.execute("""
                    INSERT INTO trades (
                        created_at, buy_at, symbol, side, price, shares, signal,
                        status, note, filled_shares, filled_at,
                        strategy_horizon, strategy_horizon_source, strategy_horizon_locked_at
                    ) VALUES (?, NULL, ?, 'BUY', ?, ?, ?, 'filled', ?, ?, NULL, ?, ?, ?)
                """, (
                    locked_at, code, avg_price, uncovered_shares,
                    "manual_legacy_position_lock",
                    "既有券商庫存一次性鎖定策略週期；真實買進日未知，禁止時間出場",
                    uncovered_shares, horizon, "manual_legacy_position_lock", locked_at,
                ))
                synthetic_id = cursor.lastrowid
                selected_dates.append(False)
            buy_date_known = bool(selected_dates) and all(selected_dates)
        return {
            "ok": True,
            "symbol": code,
            "strategyHorizon": horizon,
            "lockedAt": locked_at,
            "updatedTradeIds": updated_ids,
            "syntheticTradeId": synthetic_id,
            "syntheticShares": uncovered_shares,
            "buyDateKnown": buy_date_known,
        }

    def lock_existing_position_horizons(self, assignments, holdings, apply=False, require_all=True):
        """Preview or atomically lock every unknown FIFO lot by immutable trade id."""
        if isinstance(assignments, dict):
            raw_assignments = [
                {"symbol": symbol, "strategyHorizon": horizon}
                for symbol, horizon in assignments.items()
            ]
        elif isinstance(assignments, list):
            raw_assignments = assignments
        else:
            raise ValueError("策略週期批次資料格式錯誤")
        if not isinstance(holdings, dict) or not holdings:
            raise ValueError("目前沒有券商持股快照，禁止批次鎖定")

        normalized_assignments = []
        input_keys = set()
        for raw in raw_assignments:
            if not isinstance(raw, dict):
                raise ValueError("每筆策略週期必須包含股票代號、tradeId 與週期")
            code = str(raw.get("symbol") or raw.get("code") or "")
            code = code.replace(".TWO", "").replace(".TW", "").strip()
            horizon = normalize_strategy_horizon(
                raw.get("strategyHorizon") or raw.get("strategy_horizon")
            )
            if not code or horizon == "unknown":
                raise ValueError("每個 lot 都必須明確選擇短期、中期或長期")
            raw_trade_id = raw.get("tradeId")
            if raw_trade_id in (None, ""):
                raw_trade_id = raw.get("trade_id")
            trade_id = None
            if raw_trade_id not in (None, ""):
                try:
                    trade_id = int(raw_trade_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{code} 的 tradeId 無效") from exc
                if trade_id <= 0:
                    raise ValueError(f"{code} 的 tradeId 無效")
            input_key = (code, trade_id)
            if input_key in input_keys:
                lot_label = f"tradeId {trade_id}" if trade_id is not None else "未指定 tradeId"
                raise ValueError(f"{code} 的 {lot_label} 在批次資料中重複")
            input_keys.add(input_key)
            normalized_assignments.append({
                "symbol": code,
                "tradeId": trade_id,
                "strategyHorizon": horizon,
            })

        normalized_holdings = {}
        for raw_symbol, holding in holdings.items():
            if not isinstance(holding, dict):
                continue
            code = str(holding.get("code") or raw_symbol or "")
            code = code.replace(".TWO", "").replace(".TW", "").strip()
            direct_shares = self.safe_float(holding.get("shares"))
            position_shares = int(direct_shares) if direct_shares and direct_shares > 0 else int(
                max(0.0, self.safe_float(holding.get("quantity")) or 0.0) * 1000
            )
            if code and position_shares > 0:
                normalized_holdings[code] = {**holding, "_positionShares": position_shares}
        if not normalized_holdings:
            raise ValueError("券商持股快照沒有有效股數")

        locked_at = now_text()
        required_lots = []
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            for code, holding in sorted(normalized_holdings.items()):
                position_shares = int(holding["_positionShares"])
                avg_price = self.safe_float(holding.get("price") or holding.get("avgPrice")) or 0.0
                if avg_price <= 0:
                    raise ValueError(f"{code} 的券商平均成本無效")
                rows = conn.execute("""
                    SELECT *
                    FROM trades
                    WHERE symbol = ?
                      AND side = 'BUY'
                      AND status != 'paper'
                      AND exit_price IS NULL
                      AND exit_at IS NULL
                      AND (status IN ('filled', 'partial') OR COALESCE(filled_shares, 0) > 0)
                    ORDER BY
                        CASE WHEN COALESCE(filled_at, buy_at) IS NULL THEN 1 ELSE 0 END,
                        COALESCE(filled_at, buy_at) ASC,
                        id ASC
                """, (code,)).fetchall()
                remaining = position_shares
                covered_shares = 0
                for row in rows:
                    available = int(row["filled_shares"] or row["shares"] or 0)
                    if available <= 0 or remaining <= 0:
                        continue
                    use_shares = min(available, remaining)
                    remaining -= use_shares
                    covered_shares += use_shares
                    row_horizon = normalize_strategy_horizon(row["strategy_horizon"])
                    if row_horizon == "unknown":
                        if use_shares != available:
                            raise ValueError(
                                f"{code} tradeId {int(row['id'])} 股數與券商持股不一致，禁止部分鎖定"
                            )
                        row_keys = set(row.keys())
                        entry_cost_amount = self.safe_float(
                            row["broker_cost_amount"] if "broker_cost_amount" in row_keys else None
                        ) or self.safe_float(
                            row["entry_cost_amount"] if "entry_cost_amount" in row_keys else None
                        )
                        buy_at = str(row["filled_at"] or row["buy_at"] or "").strip()
                        required_lots.append({
                            "symbol": code,
                            "tradeId": int(row["id"]),
                            "shares": use_shares,
                            "buyDate": buy_at[:10] or None,
                            "buyAt": buy_at or None,
                            "buyPrice": self.safe_float(row["price"]),
                            "entryCostAmount": entry_cost_amount,
                            "entryCostPerShare": round(entry_cost_amount / available, 6)
                            if entry_cost_amount and available > 0 else self.safe_float(row["price"]),
                            "strategyHorizonSource": row["strategy_horizon_source"] or "",
                        })
                uncovered_shares = max(0, position_shares - covered_shares)
                if uncovered_shares > 0:
                    raise ValueError(
                        f"{code} 尚有 {uncovered_shares} 股未匯入真實 trade lot，請先同步成交明細"
                    )

            if not required_lots:
                raise ValueError("目前持股沒有未知週期 lot，沒有可批次寫入的資料")

            required_by_key = {
                (lot["symbol"], lot["tradeId"]): lot for lot in required_lots
            }
            required_by_symbol = {}
            for lot in required_lots:
                required_by_symbol.setdefault(lot["symbol"], []).append(lot)

            resolved = {}
            for assignment in normalized_assignments:
                code = assignment["symbol"]
                trade_id = assignment["tradeId"]
                if trade_id is None:
                    symbol_lots = required_by_symbol.get(code) or []
                    if len(symbol_lots) > 1:
                        raise ValueError(
                            f"{code} 有 {len(symbol_lots)} 個未知週期 lot，必須逐筆提供 tradeId"
                        )
                    if not symbol_lots:
                        raise ValueError(f"{code} 目前沒有未知週期 lot，禁止覆寫")
                    trade_id = int(symbol_lots[0]["tradeId"])
                key = (code, trade_id)
                if key not in required_by_key:
                    raise ValueError(
                        f"{code} tradeId {trade_id} 不在目前未知週期持股 lot 中，禁止覆寫"
                    )
                if key in resolved:
                    raise ValueError(f"{code} tradeId {trade_id} 在批次資料中重複")
                resolved[key] = assignment["strategyHorizon"]

            missing_keys = sorted(set(required_by_key) - set(resolved))
            if require_all and missing_keys:
                labels = ", ".join(f"{code}#{trade_id}" for code, trade_id in missing_keys)
                raise ValueError(f"尚有 {len(missing_keys)} 個 lot 未選策略週期：{labels}")
            selected_plans = [
                {
                    **required_by_key[key],
                    "strategyHorizon": horizon,
                }
                for key, horizon in sorted(resolved.items())
            ]
            if not selected_plans:
                raise ValueError("沒有可套用的 lot 策略週期")
            preview_lots = [
                {
                    **lot,
                    "strategyHorizon": resolved.get((lot["symbol"], lot["tradeId"])),
                }
                for lot in required_lots
            ]

            audit_id = None
            batch_key = None
            if apply:
                for plan in selected_plans:
                    cursor = conn.execute("""
                        UPDATE trades
                        SET strategy_horizon = ?,
                            strategy_horizon_source = 'manual_batch_lot_lock',
                            strategy_horizon_locked_at = ?
                        WHERE id = ?
                          AND symbol = ?
                          AND side = 'BUY'
                          AND (strategy_horizon IS NULL OR strategy_horizon = '' OR strategy_horizon = 'unknown')
                    """, (
                        plan["strategyHorizon"], locked_at,
                        plan["tradeId"], plan["symbol"],
                    ))
                    if cursor.rowcount != 1:
                        raise ValueError(
                            f"{plan['symbol']} tradeId {plan['tradeId']} 已被其他操作修改，整批取消"
                        )
                assignment_payload = [
                    {
                        "symbol": plan["symbol"],
                        "tradeId": plan["tradeId"],
                        "strategyHorizon": plan["strategyHorizon"],
                    }
                    for plan in selected_plans
                ]
                audit_payload = {
                    "lockedAt": locked_at,
                    "source": "manual_batch_lot_lock",
                    "assignments": assignment_payload,
                    "items": selected_plans,
                }
                batch_key = hashlib.sha256(
                    json.dumps(audit_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                cursor = conn.execute("""
                    INSERT INTO portfolio_horizon_lock_batches (
                        batch_key, created_at, assignment_count, payload_json
                    ) VALUES (?, ?, ?, ?)
                """, (
                    batch_key,
                    locked_at,
                    len(selected_plans),
                    json.dumps(audit_payload, ensure_ascii=False, separators=(",", ":"), default=str),
                ))
                audit_id = int(cursor.lastrowid)

        return {
            "ok": True,
            "applied": bool(apply),
            "preview": not bool(apply),
            "lockedAt": locked_at if apply else None,
            "requiredSymbols": sorted({lot["symbol"] for lot in required_lots}),
            "requiredTradeIds": sorted(lot["tradeId"] for lot in required_lots),
            "requiredLots": preview_lots,
            "assignmentCount": len(selected_plans),
            "assignments": [
                {
                    "symbol": plan["symbol"],
                    "tradeId": plan["tradeId"],
                    "strategyHorizon": plan["strategyHorizon"],
                }
                for plan in selected_plans
            ],
            "items": selected_plans,
            "auditId": audit_id,
            "batchKey": batch_key,
        }

    def import_sinopac_position_details(self, detail_payload, holdings=None, apply=False):
        """Reconcile and optionally import Shioaji open-position lots atomically.

        Shioaji position-detail ``price`` is the lot's actual broker cost, not
        a reliable gross fill price: commission discounts can differ by order.
        Imported rows therefore store cost per share and explicitly suppress a
        second buy-commission charge in portfolio P/L.
        """
        if not isinstance(detail_payload, dict) or detail_payload.get("ok") is not True:
            raise ValueError("永豐持股明細無效，禁止匯入")
        if detail_payload.get("simulation") is True:
            raise ValueError("模擬帳戶持股明細不得匯入真實 trades")
        if detail_payload.get("errors"):
            raise ValueError("永豐持股明細含錯誤，禁止部分匯入")

        raw_positions = detail_payload.get("positions") or []
        if not isinstance(raw_positions, list) or not raw_positions:
            raise ValueError("永豐持股明細沒有可匯入部位")
        reported_count = int(self.safe_float(detail_payload.get("positionCount")) or len(raw_positions))
        reported_reconciled = int(
            self.safe_float(detail_payload.get("reconciledPositionCount")) or 0
        )
        if reported_count != len(raw_positions) or reported_reconciled != len(raw_positions):
            raise ValueError("永豐持股明細未全部完成股數核對")

        holding_rows = []
        if holdings is not None:
            if isinstance(holdings, dict) and isinstance(holdings.get("holdings"), list):
                if holdings.get("ok") is False:
                    raise ValueError("永豐即時庫存讀取失敗，禁止匯入")
                holding_rows = holdings.get("holdings") or []
            elif isinstance(holdings, list):
                holding_rows = holdings
            elif isinstance(holdings, dict):
                holding_rows = [
                    ({"code": code, **value} if isinstance(value, dict) else {})
                    for code, value in holdings.items()
                ]
            else:
                raise ValueError("永豐即時庫存格式錯誤")
        holding_map = {}
        for raw in holding_rows:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or raw.get("symbol") or "").replace(".TW", "").replace(".TWO", "").strip()
            direct_shares = self.safe_float(raw.get("shares"))
            shares = int(direct_shares) if direct_shares and direct_shares > 0 else int(
                max(0.0, self.safe_float(raw.get("quantity")) or 0.0) * 1000
            )
            if code and shares > 0:
                holding_map[code] = shares

        positions = []
        all_lots = []
        source_keys = set()
        today = dt.date.today()
        for index, raw_position in enumerate(raw_positions, start=1):
            if not isinstance(raw_position, dict) or raw_position.get("reconciled") is not True:
                raise ValueError(f"第 {index} 個永豐部位尚未完成股數核對")
            code = str(raw_position.get("code") or "").replace(".TW", "").replace(".TWO", "").strip()
            expected_shares = int(self.safe_float(raw_position.get("expectedShares")) or 0)
            raw_lots = raw_position.get("lots") or []
            if not code or expected_shares <= 0 or not isinstance(raw_lots, list) or not raw_lots:
                raise ValueError(f"第 {index} 個永豐部位資料不完整")
            if code in {row["code"] for row in positions}:
                raise ValueError(f"永豐持股明細重複股票代號 {code}")
            if holding_map and holding_map.get(code) != expected_shares:
                raise ValueError(
                    f"{code} 明細 {expected_shares} 股與即時庫存 {holding_map.get(code) or 0} 股不一致"
                )

            lots = []
            for lot_index, raw_lot in enumerate(raw_lots, start=1):
                if not isinstance(raw_lot, dict):
                    raise ValueError(f"{code} 第 {lot_index} 批格式錯誤")
                lot_code = str(raw_lot.get("code") or code).replace(".TW", "").replace(".TWO", "").strip()
                date_text = str(raw_lot.get("date") or raw_lot.get("buyDate") or "").strip()[:10]
                try:
                    buy_date = dt.date.fromisoformat(date_text)
                except ValueError as exc:
                    raise ValueError(f"{code} 第 {lot_index} 批買進日格式錯誤") from exc
                shares = int(self.safe_float(raw_lot.get("shares")) or 0)
                cost_amount = self.safe_float(raw_lot.get("costAmount")) or 0.0
                if lot_code != code or shares <= 0 or cost_amount <= 0 or buy_date > today:
                    raise ValueError(f"{code} 第 {lot_index} 批日期、股數或成本無效")
                dseq = str(raw_lot.get("dseq") or "").strip()
                fingerprint = f"{code}|{date_text}|{dseq}|{shares}|{cost_amount:.4f}"
                source_key = f"sinopac-position-detail:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]}"
                if source_key in source_keys:
                    raise ValueError(f"{code} 第 {lot_index} 批重複")
                source_keys.add(source_key)
                lot = {
                    "code": code,
                    "buyDate": buy_date.isoformat(),
                    "shares": shares,
                    "costAmount": round(cost_amount, 4),
                    "costPrice": round(cost_amount / shares, 6),
                    "dseq": dseq,
                    "sourceLotKey": source_key,
                }
                lots.append(lot)
                all_lots.append(lot)
            lot_shares = sum(lot["shares"] for lot in lots)
            if lot_shares != expected_shares:
                raise ValueError(f"{code} 分批合計 {lot_shares} 股與部位 {expected_shares} 股不一致")
            positions.append({"code": code, "expectedShares": expected_shares, "lots": lots})

        if holding_map:
            missing = sorted(code for code in holding_map if code not in {row["code"] for row in positions})
            if missing:
                raise ValueError(f"永豐即時庫存有 {len(missing)} 檔缺少分批明細：{', '.join(missing[:8])}")
        reported_lots = int(self.safe_float(detail_payload.get("lotCount")) or len(all_lots))
        if reported_lots != len(all_lots):
            raise ValueError("永豐持股明細批數不一致")

        imported_at = now_text()
        plans = []
        inserted_ids = []
        audit_position_count = 0
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            codes = [row["code"] for row in positions]
            placeholders = ",".join("?" for _ in codes)
            open_rows = conn.execute(f"""
                SELECT * FROM trades
                WHERE symbol IN ({placeholders})
                  AND side = 'BUY'
                  AND status != 'paper'
                  AND exit_price IS NULL
                  AND exit_at IS NULL
                  AND (status IN ('filled', 'partial') OR COALESCE(filled_shares, 0) > 0)
                ORDER BY symbol, COALESCE(filled_at, buy_at), id
            """, tuple(codes)).fetchall()
            open_by_code = {}
            for row in open_rows:
                open_by_code.setdefault(row["symbol"], []).append(row)

            for position in positions:
                code = position["code"]
                existing = open_by_code.get(code, [])
                used_ids = set()
                lot_plans = []
                by_source = {
                    str(row["source_lot_key"] or ""): row
                    for row in existing
                    if str(row["source_lot_key"] or "")
                }
                for lot in position["lots"]:
                    matched = by_source.get(lot["sourceLotKey"])
                    match_type = "already_imported" if matched is not None else ""
                    if matched is None:
                        for candidate in existing:
                            candidate_id = int(candidate["id"])
                            if candidate_id in used_ids or str(candidate["source_lot_key"] or ""):
                                continue
                            candidate_date = str(candidate["filled_at"] or candidate["buy_at"] or "")[:10]
                            candidate_shares = int(candidate["filled_shares"] or candidate["shares"] or 0)
                            if candidate_date != lot["buyDate"] or candidate_shares != lot["shares"]:
                                continue
                            candidate_cost = float(candidate["price"] or 0) * candidate_shares
                            if not bool(candidate["entry_cost_includes_buy_fee"]):
                                candidate_cost *= 1 + BUY_COMMISSION_RATE
                            tolerance = max(2.0, lot["costAmount"] * 0.00002)
                            if abs(candidate_cost - lot["costAmount"]) <= tolerance:
                                matched = candidate
                                match_type = "matched_existing_fill"
                                break
                    if matched is not None:
                        used_ids.add(int(matched["id"]))
                    lot_plans.append({
                        **lot,
                        "action": match_type or "insert",
                        "tradeId": int(matched["id"]) if matched is not None else None,
                    })

                unmatched = [row for row in existing if int(row["id"]) not in used_ids]
                if unmatched:
                    ids = ", ".join(str(row["id"]) for row in unmatched[:8])
                    raise ValueError(f"{code} 本地仍有未對應的開放 BUY lot（trade {ids}），禁止重複匯入")
                planned_shares = sum(lot["shares"] for lot in lot_plans)
                if planned_shares != position["expectedShares"]:
                    raise ValueError(f"{code} 遷移計畫股數核對失敗")
                plans.append({
                    "code": code,
                    "expectedShares": position["expectedShares"],
                    "lots": lot_plans,
                    "newLotCount": sum(1 for lot in lot_plans if lot["action"] == "insert"),
                    "newShares": sum(lot["shares"] for lot in lot_plans if lot["action"] == "insert"),
                })

            if apply:
                for position_plan in plans:
                    imported_for_symbol = []
                    matched_cost = 0.0
                    for lot in position_plan["lots"]:
                        if lot["action"] in {"already_imported", "matched_existing_fill"}:
                            row = conn.execute("SELECT * FROM trades WHERE id = ?", (lot["tradeId"],)).fetchone()
                            row_shares = int(row["filled_shares"] or row["shares"] or 0)
                            row_cost = float(row["price"] or 0) * row_shares
                            if not bool(row["entry_cost_includes_buy_fee"]):
                                row_cost *= 1 + BUY_COMMISSION_RATE
                            matched_cost += row_cost
                            conn.execute("""
                                UPDATE trades
                                SET source_lot_key = ?,
                                    broker_cost_amount = ?,
                                    broker_seqno = COALESCE(NULLIF(broker_seqno, ''), ?)
                                WHERE id = ?
                                  AND (source_lot_key IS NULL OR source_lot_key = '' OR source_lot_key = ?)
                            """, (
                                lot["sourceLotKey"], lot["costAmount"], lot["dseq"] or None,
                                lot["tradeId"], lot["sourceLotKey"],
                            ))
                            continue
                        cursor = conn.execute("""
                            INSERT INTO trades (
                                created_at, buy_at, symbol, side, price, shares, signal,
                                status, note, broker_seqno, filled_shares, filled_at,
                                strategy_horizon, strategy_horizon_source,
                                entry_cost_includes_buy_fee, broker_cost_amount, source_lot_key
                            ) VALUES (?, ?, ?, 'BUY', ?, ?, ?, 'filled', ?, ?, ?, ?,
                                      'unknown', 'sinopac_position_detail_import', 1, ?, ?)
                        """, (
                            imported_at, lot["buyDate"], position_plan["code"], lot["costPrice"],
                            lot["shares"], "sinopac_position_detail_import",
                            "永豐庫存分批明細匯入；price 為實際成本單價且已含買進手續費；策略週期待人工鎖定",
                            lot["dseq"] or None, lot["shares"], lot["buyDate"],
                            lot["costAmount"], lot["sourceLotKey"],
                        ))
                        lot["tradeId"] = int(cursor.lastrowid)
                        lot["action"] = "inserted"
                        inserted_ids.append(int(cursor.lastrowid))
                        imported_for_symbol.append(dict(lot))
                    if imported_for_symbol:
                        audit_position_count += 1
                        broker_cost = sum(lot["costAmount"] for lot in position_plan["lots"])
                        imported_cost = sum(lot["costAmount"] for lot in imported_for_symbol)
                        cost_variance = matched_cost + imported_cost - broker_cost
                        conn.execute("""
                            INSERT INTO legacy_lot_imports (
                                imported_at, symbol, position_shares, migratable_shares,
                                broker_average_price, replaced_trade_ids_json,
                                replaced_trades_json, imported_trade_ids_json, lots_json,
                                cost_variance, note
                            ) VALUES (?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?)
                        """, (
                            imported_at, position_plan["code"], position_plan["expectedShares"],
                            sum(lot["shares"] for lot in imported_for_symbol),
                            round(broker_cost / position_plan["expectedShares"], 6),
                            json.dumps([lot["tradeId"] for lot in imported_for_symbol], separators=(",", ":")),
                            json.dumps(imported_for_symbol, ensure_ascii=False, separators=(",", ":")),
                            round(cost_variance, 2),
                            "永豐 list_position_detail 實際分批日期與成本；未推測策略週期",
                        ))

        return {
            "ok": True,
            "applied": bool(apply),
            "positionCount": len(plans),
            "lotCount": len(all_lots),
            "matchedExistingLotCount": sum(
                1 for position in plans for lot in position["lots"]
                if lot["action"] in {"matched_existing_fill", "already_imported"}
            ),
            "newLotCount": sum(position["newLotCount"] for position in plans),
            "newShares": sum(position["newShares"] for position in plans),
            "insertedTradeIds": inserted_ids,
            "auditPositionCount": audit_position_count,
            "unknownHorizonPositionCount": len(plans),
            "positions": plans,
            "importedAt": imported_at if apply else None,
            "note": "買進日與實際成本已核對；策略週期無法由券商成交資料推定，仍須人工鎖定",
        }

    def import_sinopac_execution_evidence(self, payload, apply=False):
        """Import user-verified SinoPac execution history with an audit trail.

        Position-detail cost and gross execution price are deliberately kept as
        separate facts.  Open-position rows are enriched in place; historical
        executions create FIFO round trips and use SinoPac realized P/L when an
        unambiguous broker record is already available.
        """
        if not isinstance(payload, dict):
            raise ValueError("永豐成交證據格式錯誤")
        batch_id = str(payload.get("batchId") or payload.get("batch_id") or "").strip()
        if not batch_id or len(batch_id) > 120:
            raise ValueError("永豐成交證據缺少有效批次代號")

        source_hashes = {}
        for source in payload.get("sources") or []:
            if not isinstance(source, dict):
                raise ValueError("成交證據來源格式錯誤")
            filename = str(source.get("filename") or "").strip()
            sha256 = str(source.get("sha256") or "").strip().lower()
            if not filename or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError("成交證據來源缺少檔名或 SHA-256")
            if filename in source_hashes and source_hashes[filename] != sha256:
                raise ValueError(f"成交證據來源 {filename} 的 SHA-256 衝突")
            source_hashes[filename] = sha256
        if not source_hashes:
            raise ValueError("永豐成交證據至少需要一個可稽核來源")

        raw_records = payload.get("records") or []
        if not isinstance(raw_records, list) or not raw_records or len(raw_records) > 1000:
            raise ValueError("永豐成交證據必須包含 1 至 1000 筆成交")
        normalized = []
        seen_keys = set()
        for index, raw in enumerate(raw_records, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {index} 筆成交證據格式錯誤")
            symbol = str(raw.get("symbol") or raw.get("code") or "").replace(".TW", "").replace(".TWO", "").strip()
            side = str(raw.get("side") or raw.get("action") or "").upper().strip()
            deal_text = str(raw.get("dealAt") or raw.get("deal_at") or "").strip().replace("T", " ")
            try:
                deal_dt = dt.datetime.fromisoformat(deal_text)
            except ValueError as exc:
                raise ValueError(f"第 {index} 筆成交時間格式錯誤") from exc
            deal_at = deal_dt.isoformat(sep=" ", timespec="milliseconds")
            price = self.safe_float(raw.get("price") or raw.get("executionPrice")) or 0.0
            try:
                shares = int(float(raw.get("shares") or 0))
            except (TypeError, ValueError):
                shares = 0
            dseq = str(raw.get("dseq") or raw.get("brokerDseq") or "").strip()
            source_filename = str(raw.get("sourceFile") or raw.get("source_filename") or "").strip()
            condition = str(raw.get("condition") or raw.get("tradeCondition") or "Cash").strip()
            scope = "open_position" if raw.get("openPosition") is True else "history"
            if not symbol or side not in {"BUY", "SELL"} or price <= 0 or shares <= 0 or not dseq:
                raise ValueError(f"第 {index} 筆成交的股票、買賣、價格、股數或委託序號無效")
            if scope == "open_position" and side != "BUY":
                raise ValueError(f"第 {index} 筆開放持倉證據只能是 BUY")
            if source_filename not in source_hashes:
                raise ValueError(f"第 {index} 筆成交找不到來源檔 {source_filename}")
            key_source = "|".join([
                "sinopac-execution-v1", symbol, side, deal_at,
                f"{price:.8f}", str(shares), dseq,
            ])
            evidence_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
            if evidence_key in seen_keys:
                raise ValueError(f"第 {index} 筆成交與同批次其他資料重複")
            seen_keys.add(evidence_key)
            normalized.append({
                "evidenceKey": evidence_key,
                "symbol": symbol,
                "side": side,
                "dealAt": deal_at,
                "price": round(price, 8),
                "shares": shares,
                "dseq": dseq,
                "condition": condition,
                "scope": scope,
                "sourceFilename": source_filename,
                "sourceSha256": source_hashes[source_filename],
                "raw": raw,
            })

        grouped = {}
        for record in normalized:
            group_key = (record["symbol"], record["side"], record["dseq"], record["scope"])
            grouped.setdefault(group_key, []).append(record)
        groups = []
        for (symbol, side, dseq, scope), records in grouped.items():
            records.sort(key=lambda row: (row["dealAt"], row["evidenceKey"]))
            shares = sum(row["shares"] for row in records)
            weighted_price = sum(row["price"] * row["shares"] for row in records) / shares
            conditions = {row["condition"] for row in records if row["condition"]}
            if len(conditions) > 1:
                raise ValueError(f"{symbol} 委託 {dseq} 含多種交易條件")
            groups.append({
                "symbol": symbol,
                "side": side,
                "dseq": dseq,
                "scope": scope,
                "dealAt": records[0]["dealAt"],
                "lastDealAt": records[-1]["dealAt"],
                "shares": shares,
                "price": round(weighted_price, 8),
                "condition": next(iter(conditions), "Cash"),
                "records": records,
            })
        groups.sort(key=lambda row: (row["dealAt"], 0 if row["side"] == "BUY" else 1, row["symbol"], row["dseq"]))

        historical_balance = {}
        for group in (row for row in groups if row["scope"] == "history"):
            symbol = group["symbol"]
            current = historical_balance.get(symbol, 0)
            current += group["shares"] if group["side"] == "BUY" else -group["shares"]
            if current < 0:
                raise ValueError(f"{symbol} 歷史成交缺少較早的 BUY，禁止用目前庫存補關帳")
            historical_balance[symbol] = current
        incomplete = {symbol: shares for symbol, shares in historical_balance.items() if shares != 0}
        if incomplete:
            detail = "、".join(f"{symbol} 尚餘 {shares} 股" for symbol, shares in sorted(incomplete.items()))
            raise ValueError(f"歷史成交不是完整 round-trip：{detail}")

        def evidence_state(conn, group):
            keys = [record["evidenceKey"] for record in group["records"]]
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT evidence_key, trade_id FROM trade_execution_evidence WHERE evidence_key IN ({placeholders})",
                tuple(keys),
            ).fetchall()
            if rows and len(rows) != len(keys):
                raise ValueError(
                    f"{group['symbol']} 委託 {group['dseq']} 只有部分成交證據已匯入，禁止混合覆寫"
                )
            return "duplicate" if len(rows) == len(keys) else "new"

        def match_open_position(conn, group):
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE symbol = ?
                  AND side = 'BUY'
                  AND status != 'paper'
                  AND exit_price IS NULL
                  AND exit_at IS NULL
                  AND (broker_dseq = ? OR broker_seqno = ?)
                ORDER BY id
            """, (group["symbol"], group["dseq"], group["dseq"])).fetchall()
            if not rows:
                rows = conn.execute("""
                    SELECT * FROM trades
                    WHERE symbol = ?
                      AND side = 'BUY'
                      AND status != 'paper'
                      AND exit_price IS NULL
                      AND exit_at IS NULL
                      AND SUBSTR(COALESCE(filled_at, buy_at, ''), 1, 10) = ?
                      AND COALESCE(NULLIF(filled_shares, 0), shares) = ?
                    ORDER BY id
                """, (group["symbol"], group["dealAt"][:10], group["shares"])).fetchall()
            if len(rows) != 1:
                raise ValueError(
                    f"{group['symbol']} 委託 {group['dseq']} 無法唯一對應目前持倉 lot（找到 {len(rows)} 筆）"
                )
            row = rows[0]
            row_shares = int(row["filled_shares"] or row["shares"] or 0)
            if row_shares != group["shares"]:
                raise ValueError(
                    f"{group['symbol']} 委託 {group['dseq']} 成交 {group['shares']} 股，持倉 lot 為 {row_shares} 股"
                )
            return row

        def match_history_trade(conn, group):
            row = conn.execute("""
                SELECT * FROM trades
                WHERE symbol = ? AND side = ? AND broker_dseq = ?
                ORDER BY id DESC LIMIT 1
            """, (group["symbol"], group["side"], group["dseq"])).fetchone()
            if row is not None:
                return row
            return conn.execute("""
                SELECT * FROM trades
                WHERE symbol = ?
                  AND side = ?
                  AND COALESCE(NULLIF(filled_shares, 0), shares) = ?
                  AND ABS(COALESCE(execution_price, price) - ?) < 0.000001
                  AND SUBSTR(COALESCE(filled_at, buy_at, created_at, ''), 1, 19) = ?
                ORDER BY id DESC LIMIT 1
            """, (
                group["symbol"], group["side"], group["shares"], group["price"],
                group["dealAt"][:19],
            )).fetchone()

        def realized_pnl_for_sell(conn, group):
            try:
                rows = conn.execute("""
                    SELECT dedup_key, pnl, pr_ratio, imported_at
                    FROM sinopac_realized_pnl
                    WHERE code = ?
                      AND realized_date = ?
                      AND ABS(price - ?) < 0.000001
                    ORDER BY imported_at DESC, rowid DESC
                """, (group["symbol"], group["dealAt"][:10], group["price"])).fetchall()
            except sqlite3.OperationalError:
                return None
            valid = [row for row in rows if row["pnl"] is not None]
            if not valid:
                return None
            signatures = {
                (round(float(row["pnl"]), 4), round(float(row["pr_ratio"] or 0), 8))
                for row in valid
            }
            if len(signatures) != 1:
                raise ValueError(
                    f"{group['symbol']} {group['dealAt'][:10]} 永豐已實現損益有衝突，禁止自動套用"
                )
            row = valid[0]
            return {
                "key": str(row["dedup_key"] or ""),
                "pnl": float(row["pnl"]),
                "pnlPct": round(float(row["pr_ratio"] or 0) * 100, 4),
            }

        plans = []
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            for group in groups:
                state = evidence_state(conn, group)
                if group["scope"] == "open_position":
                    matched = match_open_position(conn, group)
                else:
                    matched = match_history_trade(conn, group)
                realized = realized_pnl_for_sell(conn, group) if group["side"] == "SELL" else None
                plans.append({
                    **{key: group[key] for key in (
                        "symbol", "side", "dseq", "scope", "dealAt", "lastDealAt",
                        "shares", "price", "condition",
                    )},
                    "evidenceCount": len(group["records"]),
                    "state": state,
                    "tradeId": int(matched["id"]) if matched is not None else None,
                    "action": (
                        "duplicate" if state == "duplicate"
                        else "enrich_open_position" if group["scope"] == "open_position"
                        else "match_history_trade" if matched is not None
                        else "create_history_trade"
                    ),
                    "realizedPnlMatched": realized is not None,
                })

            if not apply:
                return {
                    "ok": True,
                    "applied": False,
                    "batchId": batch_id,
                    "sourceCount": len(source_hashes),
                    "recordCount": len(normalized),
                    "groupCount": len(groups),
                    "openPositionGroupCount": sum(row["scope"] == "open_position" for row in groups),
                    "historyGroupCount": sum(row["scope"] == "history" for row in groups),
                    "roundTripCount": sum(
                        row["scope"] == "history" and row["side"] == "SELL" for row in groups
                    ),
                    "realizedPnlMatchCount": sum(row["realizedPnlMatched"] for row in plans),
                    "plans": plans,
                }

            imported_at = now_text()
            evidence_inserted = 0
            open_enriched = 0
            history_created = 0
            history_matched = 0
            closed_round_trips = 0
            realized_matches = 0

            def insert_evidence(group, trade_id):
                nonlocal evidence_inserted
                for record in group["records"]:
                    cursor = conn.execute("""
                        INSERT OR IGNORE INTO trade_execution_evidence (
                            evidence_key, trade_id, batch_id, symbol, side, deal_at,
                            execution_price, shares, broker_dseq, trade_condition,
                            evidence_scope, source_filename, source_sha256, raw_json, imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        record["evidenceKey"], trade_id, batch_id, record["symbol"], record["side"],
                        record["dealAt"], record["price"], record["shares"], record["dseq"],
                        record["condition"], record["scope"], record["sourceFilename"],
                        record["sourceSha256"],
                        json.dumps(record["raw"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        imported_at,
                    ))
                    evidence_inserted += max(0, int(cursor.rowcount or 0))

            open_groups = [group for group in groups if group["scope"] == "open_position"]
            for group in open_groups:
                if evidence_state(conn, group) == "duplicate":
                    continue
                matched = match_open_position(conn, group)
                trade_id = int(matched["id"])
                conn.execute("""
                    UPDATE trades
                    SET buy_at = ?,
                        filled_at = ?,
                        execution_price = ?,
                        broker_dseq = ?,
                        trade_condition = ?,
                        execution_evidence_source = ?
                    WHERE id = ?
                """, (
                    group["dealAt"], group["dealAt"], group["price"], group["dseq"],
                    group["condition"], batch_id, trade_id,
                ))
                insert_evidence(group, trade_id)
                open_enriched += 1

            history_groups = [group for group in groups if group["scope"] == "history"]
            for group in history_groups:
                if evidence_state(conn, group) == "duplicate":
                    continue
                trade = match_history_trade(conn, group)
                if trade is None:
                    cursor = conn.execute("""
                        INSERT INTO trades (
                            created_at, buy_at, symbol, side, price, shares, signal, status, note,
                            filled_shares, filled_at, strategy_horizon, strategy_horizon_source,
                            execution_price, broker_dseq, trade_condition, execution_evidence_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?,
                                  'unknown', ?, ?, ?, ?, ?)
                    """, (
                        group["dealAt"], group["dealAt"] if group["side"] == "BUY" else None,
                        group["symbol"], group["side"], group["price"], group["shares"],
                        "sinopac_execution_evidence_import",
                        "永豐歷史成交證據匯入；策略週期未由成交資料推測",
                        group["shares"], group["dealAt"],
                        "sinopac_history_unknown" if group["side"] == "BUY" else "sell_fill",
                        group["price"], group["dseq"], group["condition"], batch_id,
                    ))
                    trade_id = int(cursor.lastrowid)
                    history_created += 1
                else:
                    trade_id = int(trade["id"])
                    conn.execute("""
                        UPDATE trades
                        SET execution_price = ?,
                            broker_dseq = ?,
                            trade_condition = ?,
                            execution_evidence_source = ?,
                            filled_at = ?,
                            buy_at = CASE WHEN side = 'BUY' THEN ? ELSE buy_at END
                        WHERE id = ?
                    """, (
                        group["price"], group["dseq"], group["condition"], batch_id,
                        group["dealAt"], group["dealAt"], trade_id,
                    ))
                    history_matched += 1
                insert_evidence(group, trade_id)
                if group["side"] != "SELL":
                    continue

                remaining = group["shares"]
                buys = conn.execute("""
                    SELECT * FROM trades
                    WHERE symbol = ?
                      AND side = 'BUY'
                      AND status != 'paper'
                      AND exit_price IS NULL
                      AND exit_at IS NULL
                      AND COALESCE(filled_at, buy_at, '') <= ?
                    ORDER BY COALESCE(filled_at, buy_at) ASC, id ASC
                """, (group["symbol"], group["dealAt"])).fetchall()
                selected = []
                for buy in buys:
                    if remaining <= 0:
                        break
                    buy_shares = int(buy["filled_shares"] or buy["shares"] or 0)
                    if buy_shares <= 0:
                        continue
                    close_shares = min(remaining, buy_shares)
                    selected.append((buy, close_shares, buy_shares))
                    remaining -= close_shares
                if remaining > 0:
                    raise ValueError(
                        f"{group['symbol']} 賣出 {group['shares']} 股，但歷史 BUY 尚缺 {remaining} 股"
                    )

                realized = realized_pnl_for_sell(conn, group)
                if realized is not None:
                    realized_matches += 1
                for buy, close_shares, buy_shares in selected:
                    buy_price = float(buy["execution_price"] or buy["price"] or 0)
                    gross_pnl = (group["price"] - buy_price) * close_shares
                    allocated_pnl = (
                        realized["pnl"] * close_shares / group["shares"]
                        if realized is not None else gross_pnl
                    )
                    pnl_pct = (
                        realized["pnlPct"] if realized is not None
                        else (group["price"] / buy_price - 1) * 100 if buy_price > 0 else None
                    )
                    pnl_basis = "sinopac_realized" if realized is not None else "gross_execution"
                    realized_key = realized["key"] if realized is not None else None
                    if close_shares == buy_shares:
                        conn.execute("""
                            UPDATE trades
                            SET exit_price = ?, exit_at = ?, pnl = ?, pnl_pct = ?,
                                pnl_basis = ?, realized_pnl_key = ?, status = 'closed'
                            WHERE id = ?
                        """, (
                            group["price"], group["lastDealAt"], round(allocated_pnl, 2),
                            round(pnl_pct, 4) if pnl_pct is not None else None,
                            pnl_basis, realized_key, int(buy["id"]),
                        ))
                    else:
                        original_cost = self.safe_float(buy["broker_cost_amount"])
                        closed_cost = (
                            original_cost * close_shares / buy_shares
                            if original_cost is not None and original_cost > 0 else None
                        )
                        remaining_cost = (
                            original_cost - closed_cost
                            if original_cost is not None and closed_cost is not None else original_cost
                        )
                        cursor = conn.execute("""
                            INSERT INTO trades (
                                created_at, buy_at, parent_trade_id, symbol, side, price, shares,
                                signal, stop_price, target_price, status, exit_price, exit_at, pnl,
                                note, broker_order_id, broker_seqno, broker_ordno, filled_shares, filled_at,
                                strategy_horizon, strategy_horizon_source, strategy_horizon_locked_at,
                                entry_cost_includes_buy_fee, broker_cost_amount, source_lot_key,
                                execution_price, broker_dseq, trade_condition, execution_evidence_source,
                                pnl_pct, pnl_basis, realized_pnl_key
                            ) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            buy["created_at"], buy["buy_at"], int(buy["id"]), buy["symbol"],
                            buy["price"], close_shares, buy["signal"], buy["stop_price"], buy["target_price"],
                            group["price"], group["lastDealAt"], round(allocated_pnl, 2),
                            "永豐歷史成交證據 FIFO 分批平倉", buy["broker_order_id"], buy["broker_seqno"],
                            buy["broker_ordno"], close_shares, buy["filled_at"],
                            normalize_strategy_horizon(buy["strategy_horizon"]),
                            buy["strategy_horizon_source"], buy["strategy_horizon_locked_at"],
                            int(bool(buy["entry_cost_includes_buy_fee"])), closed_cost,
                            buy["execution_price"], buy["broker_dseq"], buy["trade_condition"],
                            buy["execution_evidence_source"],
                            round(pnl_pct, 4) if pnl_pct is not None else None,
                            pnl_basis, realized_key,
                        ))
                        conn.execute("""
                            UPDATE trades
                            SET shares = ?, filled_shares = ?, broker_cost_amount = ?
                            WHERE id = ?
                        """, (
                            buy_shares - close_shares, buy_shares - close_shares,
                            remaining_cost, int(buy["id"]),
                        ))
                    closed_round_trips += 1

        return {
            "ok": True,
            "applied": True,
            "batchId": batch_id,
            "sourceCount": len(source_hashes),
            "recordCount": len(normalized),
            "groupCount": len(groups),
            "openPositionGroupCount": len(open_groups),
            "historyGroupCount": len(history_groups),
            "openPositionLotsEnriched": open_enriched,
            "historyTradesCreated": history_created,
            "historyTradesMatched": history_matched,
            "closedRoundTrips": closed_round_trips,
            "realizedPnlMatchCount": realized_matches,
            "evidenceInserted": evidence_inserted,
            "evidenceDuplicates": len(normalized) - evidence_inserted,
            "unknownHorizonPreserved": True,
            "plans": plans,
        }

    def import_legacy_position_lots(self, symbol, lots, holding, note=""):
        """Import dated/costed lots only for broker shares not backed by real fills.

        Broker-synced fills are immutable.  The only replaceable rows are the
        explicit unknown-date placeholders created by
        ``lock_existing_position_horizon``; their full before-image is stored
        in ``legacy_lot_imports`` before replacement.
        """
        code = str(symbol or "").replace(".TWO", "").replace(".TW", "").strip()
        if not code or not isinstance(holding, dict):
            raise ValueError("必須提供目前券商持股與股票代號")
        direct_shares = self.safe_float(holding.get("shares"))
        position_shares = int(direct_shares) if direct_shares and direct_shares > 0 else int(
            max(0.0, self.safe_float(holding.get("quantity")) or 0.0) * 1000
        )
        broker_average = self.safe_float(holding.get("price") or holding.get("avgPrice")) or 0.0
        if position_shares <= 0 or broker_average <= 0:
            raise ValueError("目前券商股數與平均成本必須有效")
        if not isinstance(lots, list) or not lots or len(lots) > 50:
            raise ValueError("分批明細必須為 1 至 50 筆")

        normalized_lots = []
        for index, raw in enumerate(lots, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {index} 批格式錯誤")
            date_text = str(raw.get("buyDate") or raw.get("buy_at") or "").strip()[:10]
            try:
                buy_date = dt.date.fromisoformat(date_text)
            except ValueError as exc:
                raise ValueError(f"第 {index} 批買進日格式錯誤") from exc
            if buy_date > dt.date.today():
                raise ValueError(f"第 {index} 批買進日不能晚於今天")
            price = self.safe_float(raw.get("price") or raw.get("buyPrice")) or 0.0
            try:
                shares = int(float(raw.get("shares") or 0))
            except (TypeError, ValueError):
                shares = 0
            horizon = normalize_strategy_horizon(
                raw.get("strategyHorizon") or raw.get("strategy_horizon")
            )
            if price <= 0 or shares <= 0 or horizon == "unknown":
                raise ValueError(f"第 {index} 批必須提供正確成本、股數與策略週期")
            normalized_lots.append({
                "buyDate": buy_date.isoformat(),
                "price": round(price, 4),
                "shares": shares,
                "strategyHorizon": horizon,
            })

        imported_at = now_text()
        imported_ids = []
        replaced_rows = []
        migratable_shares = 0
        cost_variance = 0.0
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT *
                FROM trades
                WHERE symbol = ?
                  AND side = 'BUY'
                  AND status != 'paper'
                  AND exit_price IS NULL
                  AND exit_at IS NULL
                  AND (status IN ('filled', 'partial') OR COALESCE(filled_shares, 0) > 0)
                ORDER BY
                    CASE WHEN COALESCE(filled_at, buy_at) IS NULL THEN 1 ELSE 0 END,
                    COALESCE(filled_at, buy_at) ASC,
                    id ASC
            """, (code,)).fetchall()
            remaining = position_shares
            selected = []
            for row in rows:
                available = int(row["filled_shares"] or row["shares"] or 0)
                if available <= 0 or remaining <= 0:
                    continue
                use_shares = min(available, remaining)
                selected.append((row, use_shares, available))
                remaining -= use_shares
            uncovered_shares = max(0, remaining)
            placeholder_rows = []
            placeholder_shares = 0
            known_cost = 0.0
            for row, use_shares, available in selected:
                source = str(row["strategy_horizon_source"] or "")
                date_known = bool(str(row["filled_at"] or row["buy_at"] or "").strip())
                if source == "manual_legacy_position_lock" and not date_known:
                    if use_shares != available:
                        raise ValueError("人工占位 lot 股數超過目前持股，請先同步真實成交回報")
                    placeholder_rows.append(row)
                    placeholder_shares += use_shares
                else:
                    known_cost += float(row["price"] or 0) * use_shares
            migratable_shares = uncovered_shares + placeholder_shares
            imported_shares = sum(item["shares"] for item in normalized_lots)
            if migratable_shares <= 0:
                raise ValueError("這檔持股已全部由真實成交或已知分批 lot 覆蓋")
            if imported_shares != migratable_shares:
                raise ValueError(
                    f"分批股數合計必須等於待補 {migratable_shares} 股，目前為 {imported_shares} 股"
                )

            locked_horizons = {
                normalize_strategy_horizon(row["strategy_horizon"])
                for row in placeholder_rows
                if normalize_strategy_horizon(row["strategy_horizon"]) != "unknown"
            }
            if len(locked_horizons) > 1:
                raise ValueError("既有人工占位 lot 含多種已鎖定週期，禁止合併改寫")
            if locked_horizons:
                locked_horizon = next(iter(locked_horizons))
                if any(item["strategyHorizon"] != locked_horizon for item in normalized_lots):
                    raise ValueError("分批匯入不得改變先前已鎖定的策略週期")

            replaced_rows = [dict(row) for row in placeholder_rows]
            replaced_ids = [int(row["id"]) for row in placeholder_rows]
            if replaced_ids:
                placeholders = ",".join("?" for _ in replaced_ids)
                conn.execute(f"""
                    DELETE FROM trades
                    WHERE id IN ({placeholders})
                      AND strategy_horizon_source = 'manual_legacy_position_lock'
                      AND COALESCE(filled_at, buy_at) IS NULL
                """, tuple(replaced_ids))

            for item in normalized_lots:
                cursor = conn.execute("""
                    INSERT INTO trades (
                        created_at, buy_at, symbol, side, price, shares, signal,
                        status, note, filled_shares, filled_at,
                        strategy_horizon, strategy_horizon_source, strategy_horizon_locked_at
                    ) VALUES (?, ?, ?, 'BUY', ?, ?, ?, 'filled', ?, ?, ?, ?, ?, ?)
                """, (
                    imported_at,
                    item["buyDate"],
                    code,
                    item["price"],
                    item["shares"],
                    "manual_legacy_lot_import",
                    "既有券商庫存分批匯入；買進日、成本與策略週期由使用者確認",
                    item["shares"],
                    item["buyDate"],
                    item["strategyHorizon"],
                    "manual_legacy_lot_import",
                    imported_at,
                ))
                imported_ids.append(int(cursor.lastrowid))

            imported_cost = sum(item["price"] * item["shares"] for item in normalized_lots)
            broker_position_cost = broker_average * position_shares
            reconciled_cost = known_cost + imported_cost
            cost_variance = reconciled_cost - broker_position_cost
            audit_lots = [
                {**item, "tradeId": trade_id}
                for item, trade_id in zip(normalized_lots, imported_ids)
            ]
            conn.execute("""
                INSERT INTO legacy_lot_imports (
                    imported_at, symbol, position_shares, migratable_shares,
                    broker_average_price, replaced_trade_ids_json,
                    replaced_trades_json, imported_trade_ids_json, lots_json,
                    cost_variance, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                imported_at,
                code,
                position_shares,
                migratable_shares,
                broker_average,
                json.dumps([row["id"] for row in replaced_rows], separators=(",", ":")),
                json.dumps(replaced_rows, ensure_ascii=False, separators=(",", ":"), default=str),
                json.dumps(imported_ids, separators=(",", ":")),
                json.dumps(audit_lots, ensure_ascii=False, separators=(",", ":")),
                round(cost_variance, 2),
                str(note or "")[:500],
            ))

        return {
            "ok": True,
            "symbol": code,
            "positionShares": position_shares,
            "migratedShares": migratable_shares,
            "importedTradeIds": imported_ids,
            "replacedTradeIds": [row["id"] for row in replaced_rows],
            "lots": normalized_lots,
            "brokerAveragePrice": broker_average,
            "costVariance": round(cost_variance, 2),
            "costVariancePct": round(cost_variance / (broker_average * position_shares) * 100, 4),
            "importedAt": imported_at,
        }

    def portfolio_exit_analysis(self, holdings, summary=None, evaluation_date=None, persist=True):
        """Build and persist the one canonical exit result used by every UI."""
        holding_map = holdings if isinstance(holdings, dict) else {}
        generated_at = now_text()
        summary_updated_at = str(
            (summary or {}).get("updatedAt") or ""
        ).strip() if isinstance(summary, dict) else ""
        items = []
        for raw_symbol, raw_holding in sorted(holding_map.items()):
            if not isinstance(raw_holding, dict):
                continue
            symbol = str(raw_holding.get("code") or raw_symbol or "").replace(".TWO", "").replace(".TW", "").strip()
            if not symbol:
                continue
            holding = dict(raw_holding)
            if summary_updated_at and not (
                holding.get("snapshotAt") or holding.get("updatedAt")
            ):
                holding["snapshotAt"] = summary_updated_at
            direct_shares = self.safe_float(holding.get("shares"))
            shares = int(direct_shares) if direct_shares and direct_shares > 0 else int(
                max(0.0, self.safe_float(holding.get("quantity")) or 0.0) * 1000
            )
            if shares <= 0:
                continue
            reconciliation = self.fifo_open_trade_lots(symbol, shares)
            lots = list(reconciliation["lots"])
            legacy_placeholder_shares = sum(
                int(lot.get("shares") or 0)
                for lot in lots
                if lot.get("strategyHorizonSource") == "manual_legacy_position_lock"
                and not lot.get("buyDate")
            )
            if reconciliation["unknownShares"] > 0:
                lots.append({
                    "tradeId": None,
                    "shares": reconciliation["unknownShares"],
                    "price": holding.get("price") or holding.get("avgPrice"),
                    "buyDate": None,
                    "strategyHorizon": "unknown",
                    "strategyHorizonSource": "broker_position_not_covered_by_local_fills",
                })
            rows = self.rows_with_verified_sources(self.load_price_rows(symbol))
            item = build_position_exit(
                symbol,
                holding.get("name") or symbol,
                holding,
                lots,
                rows,
                evaluation_date=evaluation_date,
                generated_at=generated_at,
            )
            item.update({
                "coveredShares": reconciliation["coveredShares"],
                "brokerUncoveredShares": reconciliation["unknownShares"],
                "brokerAveragePrice": self.safe_float(holding.get("price") or holding.get("avgPrice")),
                "legacyPlaceholderShares": legacy_placeholder_shares,
                "migratableShares": int(reconciliation["unknownShares"] or 0) + legacy_placeholder_shares,
                "hasUnknownHorizon": int(item.get("unknownShares") or 0) > 0,
                "positionBuyDateKnown": bool(lots) and all(lot.get("buyDate") for lot in lots),
                "fullyReconciled": reconciliation["fullyReconciled"],
            })
            items.append(item)

        if persist:
            with self.connect() as conn:
                for item in items:
                    payload_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
                    conn.execute("""
                    INSERT INTO portfolio_exit_snapshots (
                        symbol, decision_date, generated_at, policy_version,
                        strategy_horizon, decision_type, decision_verified, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        decision_date = excluded.decision_date,
                        generated_at = excluded.generated_at,
                        policy_version = excluded.policy_version,
                        strategy_horizon = excluded.strategy_horizon,
                        decision_type = excluded.decision_type,
                        decision_verified = excluded.decision_verified,
                        payload_json = excluded.payload_json
                    """, (
                        item["symbol"], item["decisionDate"], item["generatedAt"],
                        item["policyVersion"], item.get("strategyHorizon"), item.get("type"),
                        1 if item.get("decisionVerified") else 0,
                        payload_json,
                    ))
                    event_key = self.portfolio_exit_event_key(item)
                    conn.execute("""
                        INSERT OR IGNORE INTO portfolio_exit_history (
                            event_key, symbol, decision_date, generated_at, policy_version,
                            strategy_horizon, decision_type, decision_verified, trade_id,
                            buy_date, signal_price, shares, sell_shares, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        event_key,
                        item.get("symbol"),
                        item.get("decisionDate"),
                        item.get("generatedAt"),
                        item.get("policyVersion"),
                        item.get("strategyHorizon"),
                        item.get("type"),
                        1 if item.get("decisionVerified") else 0,
                        item.get("tradeId"),
                        item.get("buyDate"),
                        item.get("currentPrice"),
                        int(item.get("shares") or 0),
                        int(item.get("sellShares") or 0),
                        payload_json,
                        generated_at,
                    ))
        return {
            "ok": True,
            "generatedAt": generated_at,
            "summaryUpdatedAt": (summary or {}).get("updatedAt") if isinstance(summary, dict) else None,
            "policy": exit_policy_payload(),
            "items": items,
            "alerts": [alert for item in items for alert in item.get("alerts", [])],
            "counts": {
                "positions": len(items),
                "actionable": sum(
                    1 for item in items
                    if item.get("decisionVerified") and int(item.get("sellShares") or 0) >= 1000
                ),
                "unknownHorizon": sum(1 for item in items if item.get("hasUnknownHorizon")),
                "unknownBuyDate": sum(1 for item in items if not item.get("positionBuyDateKnown")),
            },
        }

    @staticmethod
    def portfolio_exit_event_key(item):
        event_signature = {
            "symbol": item.get("symbol"),
            "decisionDate": item.get("decisionDate"),
            "policyVersion": item.get("policyVersion"),
            "strategyHorizon": item.get("strategyHorizon"),
            "decisionType": item.get("type"),
            "decisionVerified": item.get("decisionVerified") is True,
            "tradeId": item.get("tradeId"),
            "buyDate": item.get("buyDate"),
            "sellShares": int(item.get("sellShares") or 0),
            "status": item.get("status"),
        }
        calculation_revision = item.get("calculationRevision")
        if item.get("type") == "phase2" and calculation_revision:
            event_signature["calculationRevision"] = calculation_revision
        return hashlib.sha256(
            json.dumps(
                event_signature,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def backfill_portfolio_exit_history_from_snapshots(self):
        """Seed append-only history from latest snapshots after deployment."""
        inserted = 0
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT payload_json, generated_at FROM portfolio_exit_snapshots ORDER BY symbol"
            ).fetchall()
            for row in rows:
                try:
                    item = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(item, dict) or not item.get("symbol") or not item.get("decisionDate"):
                    continue
                payload_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO portfolio_exit_history (
                        event_key, symbol, decision_date, generated_at, policy_version,
                        strategy_horizon, decision_type, decision_verified, trade_id,
                        buy_date, signal_price, shares, sell_shares, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.portfolio_exit_event_key(item),
                    item.get("symbol"),
                    item.get("decisionDate"),
                    item.get("generatedAt") or row["generated_at"],
                    item.get("policyVersion") or "unknown",
                    item.get("strategyHorizon"),
                    item.get("type"),
                    1 if item.get("decisionVerified") else 0,
                    item.get("tradeId"),
                    item.get("buyDate"),
                    item.get("currentPrice"),
                    int(item.get("shares") or 0),
                    int(item.get("sellShares") or 0),
                    payload_json,
                    now_text(),
                ))
                inserted += max(0, int(cursor.rowcount or 0))
        return {"ok": True, "checked": len(rows), "inserted": inserted}

    def list_portfolio_exit_snapshots(self, limit=120):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM portfolio_exit_snapshots
                ORDER BY decision_verified DESC, generated_at DESC, symbol ASC
                LIMIT ?
            """, (max(1, min(int(limit or 120), 500)),)).fetchall()
        items = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = dict(row)
            items.append(payload)
        return {"ok": True, "policy": exit_policy_payload(), "items": items}

    def list_portfolio_exit_history(self, limit=200):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT h.*,
                       (SELECT COUNT(1) FROM portfolio_exit_outcomes o WHERE o.history_id = h.id) AS settled_windows
                FROM portfolio_exit_history h
                ORDER BY h.decision_date DESC, h.id DESC
                LIMIT ?
            """, (max(1, min(int(limit or 200), 1000)),)).fetchall()
        items = []
        for row in rows:
            record = dict(row)
            try:
                record["payload"] = json.loads(record.pop("payload_json"))
            except (TypeError, json.JSONDecodeError):
                record["payload"] = {}
            items.append(record)
        return {"ok": True, "items": items, "count": len(items)}

    def settle_portfolio_exit_history(self, as_of_date=None):
        horizons = (1, 3, 5, 10, 20, 60)
        as_of = str(as_of_date or dt.date.today().isoformat())[:10]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            histories = conn.execute("""
                SELECT *
                FROM portfolio_exit_history
                WHERE decision_verified = 1
                  AND COALESCE(invalid_for_trading, 0) = 0
                  AND signal_price > 0
                  AND sell_shares > 0
                ORDER BY id
            """).fetchall()
            existing = {
                (int(row[0]), int(row[1]))
                for row in conn.execute("SELECT history_id, horizon_days FROM portfolio_exit_outcomes").fetchall()
            }

        settled = 0
        pending = 0
        considered = 0
        cost_factor = 1 - SELL_COMMISSION_RATE - SELL_TAX_RATE - EXIT_SLIPPAGE_RATE
        outcome_rows = []
        for history in histories:
            missing = [days for days in horizons if (int(history["id"]), days) not in existing]
            if not missing:
                continue
            considered += 1
            rows = [
                row for row in self.rows_with_verified_sources(self.load_price_rows(history["symbol"]))
                if str(row.get("date") or "")[:10] > str(history["decision_date"] or "")[:10]
                and str(row.get("date") or "")[:10] <= as_of
            ]
            signal_price = float(history["signal_price"] or 0)
            shares = int(history["sell_shares"] or history["shares"] or 0)
            signal_net = signal_price * shares * cost_factor
            for days in missing:
                if len(rows) < days:
                    pending += 1
                    continue
                future = rows[days - 1]
                future_close = self.safe_float(future.get("close")) or 0.0
                if future_close <= 0:
                    pending += 1
                    continue
                future_net = future_close * shares * cost_factor
                decision_net_pnl = signal_net - future_net
                decision_net_pct = decision_net_pnl / (signal_price * shares) * 100
                future_return_pct = (future_close / signal_price - 1) * 100
                outcome_rows.append((
                    int(history["id"]),
                    days,
                    str(future.get("date") or "")[:10],
                    future_close,
                    future.get("price_source") or "official daily close",
                    round(future_return_pct, 4),
                    round(decision_net_pnl, 2),
                    round(decision_net_pct, 4),
                    1 if decision_net_pnl > 0 else 0,
                    1 if decision_net_pnl < 0 else 0,
                    now_text(),
                ))
                settled += 1
        if outcome_rows:
            with self.connect() as conn:
                conn.executemany("""
                    INSERT OR IGNORE INTO portfolio_exit_outcomes (
                        history_id, horizon_days, outcome_date, future_close,
                        price_source, future_return_pct, decision_net_pnl,
                        decision_net_pct, correct, premature_sell, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, outcome_rows)
        return {
            "ok": True,
            "asOfDate": as_of,
            "considered": considered,
            "settled": settled,
            "pending": pending,
        }

    def portfolio_exit_performance(self, refresh=False, as_of_date=None):
        settlement = self.settle_portfolio_exit_history(as_of_date=as_of_date) if refresh else None
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT h.policy_version, h.strategy_horizon, h.decision_type, o.*
                FROM portfolio_exit_outcomes o
                JOIN portfolio_exit_history h ON h.id = o.history_id
                WHERE h.decision_verified = 1
                  AND COALESCE(h.invalid_for_trading, 0) = 0
                ORDER BY h.policy_version, h.strategy_horizon, o.horizon_days
            """).fetchall()
            pending_events = conn.execute("""
                SELECT COUNT(1)
                FROM portfolio_exit_history h
                WHERE h.decision_verified = 1
                  AND COALESCE(h.invalid_for_trading, 0) = 0
                  AND h.sell_shares > 0
                  AND (SELECT COUNT(1) FROM portfolio_exit_outcomes o WHERE o.history_id = h.id) < 6
            """).fetchone()[0]
            history_summary = conn.execute("""
                SELECT COUNT(1),
                       COALESCE(SUM(
                           CASE WHEN decision_verified = 1
                                  AND COALESCE(invalid_for_trading, 0) = 0
                                THEN 1 ELSE 0 END
                       ), 0),
                       COALESCE(SUM(invalid_for_trading), 0)
                FROM portfolio_exit_history
            """).fetchone()

        groups = {}
        for row in rows:
            key = (
                str(row["policy_version"] or "legacy"),
                normalize_strategy_horizon(row["strategy_horizon"]),
                int(row["horizon_days"]),
            )
            group = groups.setdefault(key, {
                "policyVersion": key[0],
                "strategyHorizon": key[1],
                "horizonDays": key[2],
                "samples": 0,
                "correct": 0,
                "premature": 0,
                "netPnl": 0.0,
                "netPcts": [],
                "wins": 0.0,
                "losses": 0.0,
                "decisionTypes": Counter(),
            })
            pnl = float(row["decision_net_pnl"] or 0)
            group["samples"] += 1
            group["correct"] += int(row["correct"] or 0)
            group["premature"] += int(row["premature_sell"] or 0)
            group["netPnl"] += pnl
            group["netPcts"].append(float(row["decision_net_pct"] or 0))
            group["decisionTypes"][str(row["decision_type"] or "unknown")] += 1
            if pnl > 0:
                group["wins"] += pnl
            elif pnl < 0:
                group["losses"] += abs(pnl)

        result_groups = []
        for group in groups.values():
            samples = group["samples"]
            result_groups.append({
                "policyVersion": group["policyVersion"],
                "strategyHorizon": group["strategyHorizon"],
                "horizonDays": group["horizonDays"],
                "samples": samples,
                "correct": group["correct"],
                "precision": round(group["correct"] / samples, 4) if samples else None,
                "netPnl": round(group["netPnl"], 2),
                "averageNetPnl": round(group["netPnl"] / samples, 2) if samples else None,
                "averageDecisionNetPct": round(sum(group["netPcts"]) / samples, 4) if samples else None,
                "profitFactor": round(group["wins"] / group["losses"], 4) if group["losses"] > 0 else None,
                "prematureSellRate": round(group["premature"] / samples, 4) if samples else None,
                "decisionTypes": dict(group["decisionTypes"]),
            })
        result_groups.sort(key=lambda item: (
            item["policyVersion"], item["strategyHorizon"], item["horizonDays"]
        ))
        return {
            "ok": True,
            "settlement": settlement,
            "groups": result_groups,
            "historyCount": int(history_summary[0] or 0),
            "verifiedEventCount": int(history_summary[1] or 0),
            "invalidEventCount": int(history_summary[2] or 0),
            "outcomeCount": len(rows),
            "pendingEvents": int(pending_events or 0),
            "method": "verified exit signal versus future official close, net of estimated sell costs on both paths",
        }

    def trade_duplicate_groups(self, apply=False):
        """Find exact duplicate local trade rows and optionally delete duplicates.

        This intentionally only removes rows that are identical on the fields that affect
        trade review accounting. It does not try to merge similar trades, because those can
        be legitimate split fills.
        """
        fields = [
            "symbol", "side", "price", "shares", "status", "buy_at", "exit_price", "exit_at",
            "pnl", "broker_order_id", "broker_seqno", "broker_ordno", "filled_at", "filled_shares",
            "strategy_horizon", "strategy_horizon_source", "strategy_horizon_locked_at",
        ]
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
            buckets = {}
            for row in rows:
                key = tuple(str(row[field] if row[field] is not None else "").strip() for field in fields)
                buckets.setdefault(key, []).append(row)
            groups = []
            delete_ids = []
            for key, bucket in buckets.items():
                if len(bucket) <= 1:
                    continue
                keep = int(bucket[0]["id"])
                duplicates = [int(row["id"]) for row in bucket[1:]]
                delete_ids.extend(duplicates)
                groups.append({
                    "keepId": keep,
                    "deleteIds": duplicates,
                    "count": len(bucket),
                    "symbol": bucket[0]["symbol"],
                    "side": bucket[0]["side"],
                    "price": bucket[0]["price"],
                    "shares": bucket[0]["shares"],
                    "status": bucket[0]["status"],
                    "buyAt": bucket[0]["buy_at"],
                    "exitAt": bucket[0]["exit_at"],
                    "brokerRefs": " / ".join(
                        str(bucket[0][field] or "") for field in ("broker_seqno", "broker_ordno", "broker_order_id")
                        if bucket[0][field]
                    ),
                })
            if apply and delete_ids:
                placeholders = ",".join("?" for _ in delete_ids)
                conn.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", delete_ids)
        return {
            "ok": True,
            "mode": "applied" if apply else "preview",
            "duplicateGroups": groups[:100],
            "groups": len(groups),
            "duplicateRows": len(delete_ids),
            "deleted": len(delete_ids) if apply else 0,
        }

    def backfill_strategy_horizons_from_execution_evidence(self, apply=True):
        """Lock only horizons proven by stored broker/import evidence.

        Holding age, symbol, price movement and portfolio labels are deliberately
        excluded.  At present the only explicit broker evidence we can classify
        is recurring-investment/stock-savings, which maps to ``long_trend``.
        """
        audited_at = now_text()
        details = []
        evidence_lots = classified_lots = updated_lots = 0
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute("""
                SELECT id, symbol, filled_at, buy_at, broker_order_id,
                       broker_seqno, broker_ordno, broker_dseq
                FROM trades
                WHERE side = 'BUY'
                  AND status != 'paper'
                  AND exit_at IS NULL
                  AND exit_price IS NULL
                  AND COALESCE(strategy_horizon, 'unknown') = 'unknown'
                ORDER BY id
            """).fetchall()
            if apply:
                conn.execute("BEGIN IMMEDIATE")
            for trade in trades:
                evidence_rows = conn.execute("""
                    SELECT 'trade_execution_evidence' AS evidence_source,
                           deal_at, raw_json
                    FROM trade_execution_evidence
                    WHERE trade_id = ? AND side = 'BUY'
                    ORDER BY deal_at, id
                """, (int(trade["id"]),)).fetchall()
                clauses = []
                params = [str(trade["symbol"])]
                for column, value in (
                    ("broker_order_id", trade["broker_order_id"]),
                    ("broker_seqno", trade["broker_seqno"]),
                    ("broker_ordno", trade["broker_ordno"]),
                    ("broker_ordno", trade["broker_dseq"]),
                ):
                    normalized = str(value or "").strip()
                    if normalized:
                        clauses.append(f"{column} = ?")
                        params.append(normalized)
                fill_rows = []
                if clauses:
                    fill_rows = conn.execute(f"""
                        SELECT 'sinopac_order_fills' AS evidence_source,
                               deal_at, raw_json
                        FROM sinopac_order_fills
                        WHERE action = 'BUY' AND code = ?
                          AND ({' OR '.join(clauses)})
                        ORDER BY deal_at, imported_at
                    """, params).fetchall()
                candidates = [*evidence_rows, *fill_rows]
                if candidates:
                    evidence_lots += 1
                classifications = []
                for row in candidates:
                    try:
                        raw = json.loads(row["raw_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        continue
                    classification = classify_explicit_buy_strategy_horizon(raw)
                    if classification.get("strategyHorizon") != "unknown":
                        classifications.append({
                            **classification,
                            "dealAt": str(row["deal_at"] or ""),
                            "evidenceSource": str(row["evidence_source"] or ""),
                        })
                horizons = {
                    item.get("strategyHorizon") for item in classifications
                    if item.get("strategyHorizon") and item.get("strategyHorizon") != "unknown"
                }
                if not horizons:
                    details.append({
                        "tradeId": int(trade["id"]),
                        "symbol": str(trade["symbol"]),
                        "evidenceRecords": len(candidates),
                        "status": "no_explicit_strategy_evidence",
                    })
                    continue
                if len(horizons) != 1:
                    details.append({
                        "tradeId": int(trade["id"]),
                        "symbol": str(trade["symbol"]),
                        "evidenceRecords": len(candidates),
                        "status": "conflicting_explicit_evidence",
                        "horizons": sorted(horizons),
                    })
                    continue
                classification = classifications[0]
                horizon = next(iter(horizons))
                classified_lots += 1
                evidence_times = sorted(
                    item["dealAt"] for item in classifications if item.get("dealAt")
                )
                locked_at = (
                    evidence_times[0] if evidence_times
                    else str(trade["filled_at"] or trade["buy_at"] or audited_at)
                )
                changed = 0
                if apply:
                    cursor = conn.execute("""
                        UPDATE trades
                        SET strategy_horizon = ?,
                            strategy_horizon_source = ?,
                            strategy_horizon_locked_at = ?
                        WHERE id = ?
                          AND COALESCE(strategy_horizon, 'unknown') = 'unknown'
                    """, (
                        horizon,
                        str(classification.get("strategyHorizonSource") or "explicit_execution_evidence"),
                        locked_at,
                        int(trade["id"]),
                    ))
                    changed = max(0, int(cursor.rowcount or 0))
                    updated_lots += changed
                details.append({
                    "tradeId": int(trade["id"]),
                    "symbol": str(trade["symbol"]),
                    "evidenceRecords": len(candidates),
                    "status": "updated" if changed else ("classified_preview" if not apply else "unchanged"),
                    "strategyHorizon": horizon,
                    "strategyHorizonSource": classification.get("strategyHorizonSource"),
                    "lockedAt": locked_at,
                    "evidence": classification.get("evidence") or [],
                })
            payload = {
                "ok": True,
                "auditedAt": audited_at,
                "applied": bool(apply),
                "scannedLots": len(trades),
                "evidenceLots": evidence_lots,
                "classifiedLots": classified_lots,
                "updatedLots": updated_lots,
                "details": details,
                "policy": "explicit_execution_evidence_only",
            }
            cursor = conn.execute("""
                INSERT INTO strategy_horizon_evidence_audits (
                    audited_at, apply_mode, scanned_lots, evidence_lots,
                    classified_lots, updated_lots, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                audited_at, "apply" if apply else "preview", len(trades), evidence_lots,
                classified_lots, updated_lots,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            ))
            payload["auditId"] = int(cursor.lastrowid)
            self.set_meta(conn, "last_strategy_horizon_evidence_audit_at", audited_at)
            self.set_meta(conn, "last_strategy_horizon_evidence_updated", str(updated_lots))
        return payload

    def sync_sinopac_order_fills(self, fills):
        """Save SinoPac order fills and update matching local trades by broker refs.

        Matching is intentionally strict. BUY fills update the matching local BUY order by
        broker refs. SELL fills first update the matching local SELL order, then closes open BUY
        rows by FIFO. Partial sells split the matched BUY into one closed child row and leave the
        original row open with the remaining shares.
        """
        imported = 0
        created = 0
        updated = 0
        closed = 0
        split = 0
        unmatched = 0
        details = []

        def text(value):
            return str(value or "").strip()

        def number(value):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if value == value else None

        def int_value(value):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0

        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            for fill in fills or []:
                if not isinstance(fill, dict):
                    continue
                code = text(fill.get("code") or fill.get("symbol"))
                if not code:
                    details.append({"action": "", "code": "", "matched": False, "reason": "missing_code"})
                    continue
                action = text(fill.get("action")).upper()
                buy_strategy = (
                    classify_explicit_buy_strategy_horizon(fill)
                    if action == "BUY" else {
                        "strategyHorizon": "unknown",
                        "strategyHorizonSource": "sell_fill",
                        "evidence": [],
                    }
                )
                persisted_buy_strategy_source = (
                    buy_strategy["strategyHorizonSource"]
                    if buy_strategy["strategyHorizon"] != "unknown"
                    else "external_fill_unknown"
                )
                price = number(fill.get("price"))
                shares = int_value(fill.get("shares"))
                deal_at = text(fill.get("dealAt") or fill.get("deal_at"))
                broker_order_id = text(fill.get("brokerOrderId") or fill.get("broker_order_id"))
                broker_seqno = text(fill.get("brokerSeqno") or fill.get("broker_seqno"))
                broker_ordno = text(fill.get("brokerOrdno") or fill.get("broker_ordno"))
                raw_json = json.dumps(fill.get("raw") or fill, ensure_ascii=False, sort_keys=True, default=str)
                dedup_source = "|".join([
                    broker_order_id, broker_seqno, broker_ordno, code, action,
                    deal_at, str(price or ""), str(shares or ""), raw_json,
                ])
                dedup_key = hashlib.sha1(dedup_source.encode("utf-8", "replace")).hexdigest()
                already_imported = conn.execute(
                    "SELECT 1 FROM sinopac_order_fills WHERE dedup_key = ?",
                    (dedup_key,),
                ).fetchone()
                if already_imported:
                    details.append({
                        "action": action,
                        "code": code,
                        "shares": shares,
                        "matched": True,
                        "reason": "duplicate_fill_ignored",
                    })
                    continue
                conn.execute("""
                    INSERT INTO sinopac_order_fills (
                        dedup_key, code, action, price, shares, deal_at,
                        broker_order_id, broker_seqno, broker_ordno, source, raw_json, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dedup_key, code, action, price, shares or None, deal_at or None,
                    broker_order_id or None, broker_seqno or None, broker_ordno or None,
                    text(fill.get("source")) or "sinopac", raw_json, now_text(),
                ))
                imported += 1

                if action not in {"BUY", "SELL"} or not price or shares <= 0 or not deal_at:
                    details.append({
                        "action": action,
                        "code": code,
                        "shares": shares,
                        "matched": False,
                        "reason": "invalid_fill_payload",
                    })
                    continue
                clauses = []
                params = []
                if broker_order_id:
                    clauses.append("broker_order_id = ?")
                    params.append(broker_order_id)
                if broker_seqno:
                    clauses.append("broker_seqno = ?")
                    params.append(broker_seqno)
                if broker_ordno:
                    clauses.append("broker_ordno = ?")
                    params.append(broker_ordno)
                if not clauses:
                    unmatched += 1
                    details.append({
                        "action": action,
                        "code": code,
                        "shares": shares,
                        "matched": False,
                        "reason": "missing_broker_refs",
                    })
                    continue
                match_params = [code, *params]
                row = conn.execute(
                    f"""
                    SELECT id, shares, filled_shares, strategy_horizon FROM trades
                    WHERE side = ?
                      AND symbol = ?
                      AND ({" OR ".join(clauses)})
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    [action, *match_params],
                ).fetchone()
                created_from_fill = False
                if not row:
                    cursor = conn.execute("""
                        INSERT INTO trades (
                            created_at, buy_at, symbol, side, price, shares, signal, status, note,
                            broker_order_id, broker_seqno, broker_ordno, filled_shares, filled_at,
                            strategy_horizon, strategy_horizon_source, strategy_horizon_locked_at,
                            execution_price, broker_dseq, execution_evidence_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        deal_at or now_text(),
                        deal_at if action == "BUY" else None,
                        code,
                        action,
                        price,
                        shares,
                        "sinopac_fill_import",
                        "永豐成交回報自動匯入；非本系統下單或本地委託紀錄不存在",
                        broker_order_id or None,
                        broker_seqno or None,
                        broker_ordno or None,
                        shares,
                        deal_at,
                        buy_strategy["strategyHorizon"] if action == "BUY" else "unknown",
                        (
                            persisted_buy_strategy_source
                            if action == "BUY" else "sell_fill"
                        ),
                        deal_at if action == "BUY" and buy_strategy["strategyHorizon"] != "unknown" else None,
                        price,
                        broker_ordno or None,
                        text(fill.get("source")) or "sinopac_order_fill",
                    ))
                    row = {
                        "id": cursor.lastrowid,
                        "shares": shares,
                        "filled_shares": 0,
                        "strategy_horizon": buy_strategy["strategyHorizon"],
                    }
                    created += 1
                    created_from_fill = True
                local_order_id = int(row["id"] if isinstance(row, dict) else row[0])
                order_shares = int((row["shares"] if isinstance(row, dict) else row[1]) or 0)
                previous_filled_shares = int(
                    (row.get("filled_shares") if isinstance(row, dict) else row[2]) or 0
                )
                newly_filled_shares = max(0, shares - previous_filled_shares)
                new_status = "filled" if shares >= order_shares else "partial"
                fill_detail = {
                    "action": action,
                    "code": code,
                    "shares": shares,
                    "price": price,
                    "dealAt": deal_at,
                    "matched": True,
                    "localOrderId": local_order_id,
                    "createdLocalTrade": created_from_fill,
                    "status": new_status,
                    "closedTradeIds": [],
                    "splitTradeIds": [],
                    "unclosedShares": 0,
                    "newlyFilledShares": newly_filled_shares,
                    "strategyHorizon": (
                        buy_strategy["strategyHorizon"] if action == "BUY" else "unknown"
                    ),
                    "strategyHorizonSource": (
                        persisted_buy_strategy_source if action == "BUY" else "sell_fill"
                    ),
                }
                if action == "BUY":
                    if buy_strategy["strategyHorizon"] != "unknown":
                        conn.execute("""
                            UPDATE trades
                            SET strategy_horizon = ?, strategy_horizon_source = ?
                            WHERE id = ?
                              AND COALESCE(strategy_horizon, 'unknown') = 'unknown'
                        """, (
                            buy_strategy["strategyHorizon"],
                            buy_strategy["strategyHorizonSource"],
                            local_order_id,
                        ))
                    conn.execute("""
                        UPDATE trades
                        SET buy_at = CASE
                                WHEN filled_at IS NULL OR NULLIF(?, '') < filled_at
                                THEN COALESCE(NULLIF(?, ''), buy_at)
                                ELSE buy_at
                            END,
                            price = CASE
                                WHEN broker_cost_amount IS NULL OR broker_cost_amount <= 0 THEN ?
                                ELSE price
                            END,
                            execution_price = ?,
                            filled_shares = ?,
                            filled_at = CASE
                                WHEN filled_at IS NULL OR NULLIF(?, '') < filled_at
                                THEN COALESCE(NULLIF(?, ''), filled_at)
                                ELSE filled_at
                            END,
                            status = ?,
                            strategy_horizon_locked_at = CASE
                                WHEN strategy_horizon IS NOT NULL
                                 AND strategy_horizon != ''
                                 AND strategy_horizon != 'unknown'
                                THEN COALESCE(strategy_horizon_locked_at, NULLIF(?, ''))
                                ELSE strategy_horizon_locked_at
                            END,
                            broker_dseq = COALESCE(NULLIF(broker_dseq, ''), NULLIF(?, '')),
                            execution_evidence_source = COALESCE(
                                NULLIF(execution_evidence_source, ''), NULLIF(?, '')
                            )
                        WHERE id = ?
                    """, (
                        deal_at, deal_at, price, price, shares, deal_at, deal_at,
                        new_status, deal_at, broker_ordno,
                        text(fill.get("source")) or "sinopac_order_fill", local_order_id,
                    ))
                else:
                    conn.execute("""
                        UPDATE trades
                            SET price = ?,
                            execution_price = ?,
                            filled_shares = ?,
                            filled_at = ?,
                            status = ?,
                            broker_dseq = COALESCE(NULLIF(broker_dseq, ''), NULLIF(?, '')),
                            execution_evidence_source = COALESCE(
                                NULLIF(execution_evidence_source, ''), NULLIF(?, '')
                            )
                        WHERE id = ?
                    """, (
                        price, price, shares, deal_at, new_status, broker_ordno,
                        text(fill.get("source")) or "sinopac_order_fill", local_order_id,
                    ))
                    if newly_filled_shares > 0:
                        remaining = newly_filled_shares
                        open_buys = conn.execute("""
                            SELECT *
                            FROM trades
                            WHERE side = 'BUY'
                              AND symbol = ?
                              AND status != 'paper'
                              AND (status IN ('filled', 'partial', 'closed') OR COALESCE(filled_shares, 0) > 0)
                              AND exit_price IS NULL
                              AND exit_at IS NULL
                            ORDER BY
                                CASE WHEN COALESCE(filled_at, buy_at) IS NULL THEN 1 ELSE 0 END,
                                COALESCE(filled_at, buy_at) ASC,
                                id ASC
                        """, (code,)).fetchall()
                        for buy in open_buys:
                            if remaining <= 0:
                                break
                            buy_id = int(buy["id"])
                            buy_shares = int(buy["shares"] or 0)
                            buy_price = float(buy["execution_price"] or buy["price"] or 0)
                            if buy_shares <= 0:
                                continue
                            close_shares = min(remaining, buy_shares)
                            pnl = (price - buy_price) * close_shares if buy_price > 0 else None
                            if close_shares == buy_shares:
                                conn.execute("""
                                    UPDATE trades
                                    SET exit_price = ?,
                                        exit_at = ?,
                                        pnl = ?,
                                        pnl_pct = ?,
                                        pnl_basis = 'gross_execution',
                                        status = 'closed'
                                    WHERE id = ?
                                """, (
                                    price, deal_at, pnl,
                                    round((price / buy_price - 1) * 100, 4) if buy_price > 0 else None,
                                    buy_id,
                                ))
                                fill_detail["closedTradeIds"].append(buy_id)
                            else:
                                cursor = conn.execute("""
                                    INSERT INTO trades (
                                        created_at, buy_at, parent_trade_id, symbol, side, price, shares,
                                        signal, stop_price, target_price, status, exit_price, exit_at, pnl,
                                        note, broker_order_id, broker_seqno, broker_ordno, filled_shares, filled_at,
                                        strategy_horizon, strategy_horizon_source, strategy_horizon_locked_at,
                                        entry_cost_includes_buy_fee, broker_cost_amount,
                                        execution_price, broker_dseq, trade_condition, execution_evidence_source,
                                        pnl_pct, pnl_basis
                                    ) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    buy["created_at"], buy["buy_at"], buy_id, buy["symbol"], buy_price, close_shares,
                                    buy["signal"], buy["stop_price"], buy["target_price"], price, deal_at, pnl,
                                    "分批賣出 FIFO 自動拆分平倉", buy["broker_order_id"], buy["broker_seqno"],
                                    buy["broker_ordno"], close_shares, buy["filled_at"],
                                    normalize_strategy_horizon(buy["strategy_horizon"]),
                                    buy["strategy_horizon_source"], buy["strategy_horizon_locked_at"],
                                    int(bool(buy["entry_cost_includes_buy_fee"])),
                                    (
                                        float(buy["broker_cost_amount"]) * close_shares / buy_shares
                                        if buy["broker_cost_amount"] is not None and buy_shares > 0 else None
                                    ),
                                    buy["execution_price"], buy["broker_dseq"], buy["trade_condition"],
                                    buy["execution_evidence_source"],
                                    round((price / buy_price - 1) * 100, 4) if buy_price > 0 else None,
                                    "gross_execution",
                                ))
                                split_id = cursor.lastrowid
                                conn.execute(
                                    """UPDATE trades
                                       SET shares = ?, filled_shares = ?,
                                           broker_cost_amount = CASE
                                               WHEN broker_cost_amount IS NULL THEN NULL
                                               ELSE broker_cost_amount * ? / ?
                                           END
                                       WHERE id = ?""",
                                    (
                                        buy_shares - close_shares, buy_shares - close_shares,
                                        buy_shares - close_shares, buy_shares, buy_id,
                                    ),
                                )
                                fill_detail["splitTradeIds"].append(split_id)
                                fill_detail["closedTradeIds"].append(split_id)
                                split += 1
                            closed += 1
                            remaining -= close_shares
                        if remaining > 0:
                            fill_detail["unclosedShares"] = remaining
                            fill_detail["reason"] = "not_enough_open_buy_shares"
                updated += 1
                details.append(fill_detail)
        realized_reconciliation = self.reconcile_realized_pnl_to_trades()
        return {
            "ok": True,
            "imported": imported,
            "createdTrades": created,
            "updatedTrades": updated,
            "closedTrades": closed,
            "splitTrades": split,
            "unmatched": unmatched,
            "details": details[-50:],
            "realizedPnlReconciliation": realized_reconciliation,
        }

    def reconcile_realized_pnl_to_trades(self, realized_date=None):
        """Replace gross FIFO P/L only when one broker result has one exact trade match."""

        result = {
            "ok": True,
            "checked": 0,
            "matched": 0,
            "alreadyMatched": 0,
            "missing": 0,
            "ambiguous": 0,
            "invalid": 0,
            "details": [],
        }
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            try:
                sql = """
                    SELECT dedup_key, code, quantity, price, pnl, pr_ratio,
                           realized_date
                    FROM sinopac_realized_pnl
                    WHERE pnl IS NOT NULL
                """
                params = []
                if realized_date:
                    sql += " AND realized_date = ?"
                    params.append(str(realized_date)[:10])
                sql += " ORDER BY realized_date, code, price, dedup_key"
                realized_rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return result

            groups = {}
            for row in realized_rows:
                result["checked"] += 1
                code = str(row["code"] or "").strip()
                date_text = str(row["realized_date"] or "")[:10]
                price = self.safe_float(row["price"])
                quantity = self.safe_float(row["quantity"])
                pnl = self.safe_float(row["pnl"])
                if not code or not date_text or price is None or price <= 0 or quantity is None or quantity <= 0 or pnl is None:
                    result["invalid"] += 1
                    result["details"].append({
                        "realizedPnlKey": str(row["dedup_key"] or ""),
                        "code": code,
                        "date": date_text,
                        "status": "invalid_realized_record",
                    })
                    continue
                shares = int(round(quantity * 1000))
                key = (code, date_text, round(price, 6), shares)
                groups.setdefault(key, []).append(row)

            for (code, date_text, price, shares), broker_rows in groups.items():
                trade_rows = conn.execute("""
                    SELECT id, realized_pnl_key
                    FROM trades
                    WHERE side = 'BUY'
                      AND status = 'closed'
                      AND symbol = ?
                      AND SUBSTR(COALESCE(exit_at, ''), 1, 10) = ?
                      AND ABS(COALESCE(exit_price, 0) - ?) < 0.000001
                      AND COALESCE(shares, 0) = ?
                    ORDER BY exit_at, id
                """, (code, date_text, price, shares)).fetchall()
                applied_keys = {
                    str(row["realized_pnl_key"] or "")
                    for row in trade_rows if str(row["realized_pnl_key"] or "")
                }
                pending_broker = [
                    row for row in broker_rows
                    if str(row["dedup_key"] or "") not in applied_keys
                ]
                result["alreadyMatched"] += len(broker_rows) - len(pending_broker)
                available_trades = [row for row in trade_rows if not row["realized_pnl_key"]]
                if not pending_broker:
                    continue
                if len(pending_broker) != 1 or len(available_trades) != 1:
                    status = "missing_trade" if not available_trades else "ambiguous_match"
                    counter = "missing" if not available_trades else "ambiguous"
                    result[counter] += len(pending_broker)
                    for row in pending_broker:
                        result["details"].append({
                            "realizedPnlKey": str(row["dedup_key"] or ""),
                            "code": code,
                            "date": date_text,
                            "price": price,
                            "shares": shares,
                            "status": status,
                            "candidateTrades": len(available_trades),
                            "candidateBrokerRows": len(pending_broker),
                        })
                    continue

                broker_row = pending_broker[0]
                trade_id = int(available_trades[0]["id"])
                pnl = float(broker_row["pnl"])
                pr_ratio = self.safe_float(broker_row["pr_ratio"])
                realized_key = str(broker_row["dedup_key"] or "")
                conn.execute("""
                    UPDATE trades
                    SET pnl = ?,
                        pnl_pct = ?,
                        pnl_basis = 'sinopac_realized',
                        realized_pnl_key = ?
                    WHERE id = ?
                      AND (realized_pnl_key IS NULL OR realized_pnl_key = '')
                """, (
                    round(pnl, 2),
                    round(pr_ratio * 100, 4) if pr_ratio is not None else None,
                    realized_key,
                    trade_id,
                ))
                result["matched"] += 1
                result["details"].append({
                    "realizedPnlKey": realized_key,
                    "tradeId": trade_id,
                    "code": code,
                    "date": date_text,
                    "price": price,
                    "shares": shares,
                    "pnl": round(pnl, 2),
                    "status": "matched",
                })
        result["details"] = result["details"][-100:]
        return result

    def save_realized_pnl(self, records):
        """把永豐已實現損益(list_profit_loss)寫進專用表 sinopac_realized_pnl——交易複盤用:每筆=
        一次真實已平倉(code/賣出日 realized_date/損益 pnl/報酬率 pr_ratio)。用永豐序號 seqno 去重,
        重抓會 INSERT OR REPLACE 更新。獨立表、不污染手動 trades 表。回傳寫入筆數。"""
        def num(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        saved = 0
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sinopac_realized_pnl (
                    dedup_key TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    quantity REAL,
                    price REAL,
                    pnl REAL,
                    pr_ratio REAL,
                    realized_date TEXT,
                    cond TEXT,
                    seqno TEXT,
                    imported_at TEXT
                )
            """)
            for r in (records or []):
                if not isinstance(r, dict):
                    continue
                code = str(r.get("code") or "").strip()
                if not code:
                    continue
                seqno = str(r.get("seqno") or "").strip()
                rdate = str(r.get("date") or "")
                key = seqno or f"{code}-{rdate}-{r.get('id')}"
                conn.execute("""
                    INSERT OR REPLACE INTO sinopac_realized_pnl
                        (dedup_key, code, quantity, price, pnl, pr_ratio, realized_date, cond, seqno, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    key, code, num(r.get("quantity")), num(r.get("price")), num(r.get("pnl")),
                    num(r.get("pr_ratio")), rdate, str(r.get("cond") or ""), seqno, now_text(),
                ))
                saved += 1
        self.reconcile_realized_pnl_to_trades()
        return saved

    def list_realized_pnl(self, limit=500):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM sinopac_realized_pnl ORDER BY realized_date DESC LIMIT ?", (limit,)
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # 表還沒建(尚未匯入過)
        return [dict(row) for row in rows]

    def record_market_session_validation(self, report):
        if not isinstance(report, dict):
            raise ValueError("交易日驗證報告格式錯誤")
        session_date = str(report.get("sessionDate") or "")[:10]
        stage = str(report.get("stage") or "").strip()
        checked_at = str(report.get("checkedAt") or now_text())
        if not session_date or stage not in {"open", "intraday", "close"}:
            raise ValueError("交易日驗證報告缺少日期或階段")
        with self.connect() as conn:
            cursor = conn.execute("""
                INSERT INTO market_session_validations (
                    session_date, stage, checked_at, ok, payload_json
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                session_date,
                stage,
                checked_at,
                1 if report.get("ok") is True else 0,
                json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str),
            ))
        return {"ok": True, "id": int(cursor.lastrowid)}

    def list_market_session_validations(self, limit=40):
        limit = max(1, min(int(limit or 40), 300))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM market_session_validations
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                payload = json.loads(record.pop("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            records.append({**record, "ok": bool(record.get("ok")), "report": payload})
        return records

    def market_session_acceptance(self, session_date=None):
        session_date = str(session_date or today_key())[:10]
        try:
            dt.date.fromisoformat(session_date)
        except ValueError as exc:
            raise ValueError("交易日驗收日期格式錯誤") from exc
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT *
                FROM market_session_validations
                WHERE session_date = ?
                ORDER BY id DESC
            """, (session_date,)).fetchall()
        stages = {}
        for row in rows:
            stage = str(row["stage"] or "")
            if stage in stages:
                continue
            try:
                report = json.loads(row["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                report = {}
            stages[stage] = {
                "id": int(row["id"]),
                "checkedAt": row["checked_at"],
                "ok": bool(row["ok"]),
                "failureCount": int(report.get("failureCount") or 0),
                "warningCount": int(report.get("warningCount") or 0),
                "failures": report.get("failures") or [],
                "warnings": report.get("warnings") or [],
                "report": report,
            }
        open_ready = bool((stages.get("open") or {}).get("ok"))
        intraday_ready = bool((stages.get("intraday") or {}).get("ok"))
        close_ready = bool((stages.get("close") or {}).get("ok"))
        missing = [stage for stage in ("open", "intraday", "close") if stage not in stages]
        failed = [stage for stage, value in stages.items() if not value.get("ok")]
        return {
            "ok": True,
            "sessionDate": session_date,
            "entryGuardReady": open_ready,
            "openReady": open_ready,
            "intradayReady": intraday_ready,
            "closeReady": close_ready,
            "fullDayReady": open_ready and intraday_ready and close_ready,
            "missingStages": missing,
            "failedStages": failed,
            "stages": stages,
        }

    def finalize_market_session_acceptance(self, session_date=None, source_validation_id=None):
        """Persist one immutable full-day acceptance result for a close validation."""
        session_date = str(session_date or today_key())[:10]
        acceptance = self.market_session_acceptance(session_date)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if source_validation_id is None:
                source_row = conn.execute("""
                    SELECT id FROM market_session_validations
                    WHERE session_date = ? AND stage = 'close'
                    ORDER BY id DESC LIMIT 1
                """, (session_date,)).fetchone()
                source_validation_id = int(source_row["id"]) if source_row else None
            else:
                source_row = conn.execute("""
                    SELECT id, stage, session_date FROM market_session_validations WHERE id = ?
                """, (int(source_validation_id),)).fetchone()
                if (
                    not source_row
                    or str(source_row["stage"] or "") != "close"
                    or str(source_row["session_date"] or "") != session_date
                ):
                    raise ValueError("全日驗收來源必須是同日 close 驗證")
            if source_validation_id is None:
                raise ValueError("尚無 close 驗證，不能封存全日驗收")

            missing = list(acceptance.get("missingStages") or [])
            failed = list(acceptance.get("failedStages") or [])
            if acceptance.get("fullDayReady") is True:
                summary = "開盤、盤中與收盤驗證均通過"
            else:
                parts = []
                if missing:
                    parts.append("缺少階段：" + "、".join(missing))
                if failed:
                    parts.append("失敗階段：" + "、".join(failed))
                summary = "；".join(parts) or "全日驗收尚未完成"
            next_actions = []
            if "open" in missing or "open" in failed:
                next_actions.append("確認盤前日 K、雷達有效性與戰績閘門")
            if "intraday" in missing or "intraday" in failed:
                next_actions.append("確認即時報價、風險否決、盤中確認與買進通知")
            if "close" in missing or "close" in failed:
                next_actions.append("確認官方收盤日 K、紙上快照、結算與賣出通知")
            finalized_at = now_text()
            payload = {
                **acceptance,
                "finalizedAt": finalized_at,
                "sourceValidationId": int(source_validation_id),
                "summary": summary,
                "nextActions": next_actions,
            }
            cursor = conn.execute("""
                INSERT OR IGNORE INTO market_session_acceptance_history (
                    session_date, source_validation_id, finalized_at,
                    full_day_ready, payload_json
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                session_date,
                int(source_validation_id),
                finalized_at,
                1 if acceptance.get("fullDayReady") is True else 0,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            ))
            self.set_meta(conn, "last_market_session_acceptance_date", session_date)
            self.set_meta(
                conn,
                "last_market_session_acceptance_status",
                "ready" if acceptance.get("fullDayReady") is True else "failed",
            )
            self.set_meta(conn, "last_market_session_acceptance_at", finalized_at)
            self.set_meta(
                conn,
                "last_market_session_acceptance_json",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            )
        return {"saved": int(cursor.rowcount or 0) > 0, **payload}

    def list_market_session_acceptance_history(self, limit=40):
        limit = max(1, min(int(limit or 40), 300))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM market_session_acceptance_history
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                payload = json.loads(record.pop("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            records.append({
                **record,
                "full_day_ready": bool(record.get("full_day_ready")),
                "acceptance": payload,
            })
        return records

    def start_stability_observation(
        self, observation_key, start_session_date=None,
        target_consecutive_sessions=5, scope=None,
    ):
        key = str(observation_key or "").strip()
        if not key:
            raise ValueError("stability observation key is required")
        start_date = str(start_session_date or today_key())[:10]
        dt.date.fromisoformat(start_date)
        target = max(1, int(target_consecutive_sessions or 5))
        started_at = now_text()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM stability_observation_runs WHERE observation_key = ?",
                (key,),
            ).fetchone()
            if not row:
                conn.execute("""
                    INSERT INTO stability_observation_runs (
                        observation_key, started_at, start_session_date,
                        target_consecutive_sessions, mode, status, scope_json
                    ) VALUES (?, ?, ?, ?, 'blockers_only', 'active', ?)
                """, (
                    key, started_at, start_date, target,
                    json.dumps(scope or [], ensure_ascii=False, separators=(",", ":")),
                ))
                self.set_meta(conn, "active_stability_observation_key", key)
                self.set_meta(conn, "stability_observation_started_at", started_at)
        return self.stability_observation_status(key)

    def record_stability_observation_day(self, session_date, acceptance):
        date_text = str(session_date or "")[:10]
        dt.date.fromisoformat(date_text)
        acceptance = dict(acceptance or {})
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            run = conn.execute("""
                SELECT * FROM stability_observation_runs
                WHERE status = 'active' AND start_session_date <= ?
                ORDER BY id DESC LIMIT 1
            """, (date_text,)).fetchone()
            if not run:
                return {"ok": True, "recorded": False, "reason": "no_active_observation"}
            blockers = []
            blockers.extend(f"missing:{stage}" for stage in acceptance.get("missingStages") or [])
            blockers.extend(f"failed:{stage}" for stage in acceptance.get("failedStages") or [])
            if acceptance.get("fullDayReady") is not True and not blockers:
                blockers.append(str(acceptance.get("summary") or "full_day_not_ready"))
            passed = acceptance.get("fullDayReady") is True
            recorded_at = now_text()
            conn.execute("""
                INSERT INTO stability_observation_days (
                    run_id, session_date, recorded_at, passed,
                    blockers_json, acceptance_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, session_date) DO UPDATE SET
                    recorded_at = excluded.recorded_at,
                    passed = excluded.passed,
                    blockers_json = excluded.blockers_json,
                    acceptance_json = excluded.acceptance_json
            """, (
                int(run["id"]), date_text, recorded_at, 1 if passed else 0,
                json.dumps(blockers, ensure_ascii=False, separators=(",", ":")),
                json.dumps(acceptance, ensure_ascii=False, separators=(",", ":"), default=str),
            ))
            days = conn.execute("""
                SELECT session_date, passed FROM stability_observation_days
                WHERE run_id = ? ORDER BY session_date
            """, (int(run["id"]),)).fetchall()
            consecutive = 0
            for day in reversed(days):
                if not bool(day["passed"]):
                    break
                consecutive += 1
            target = int(run["target_consecutive_sessions"] or 5)
            status = "completed" if consecutive >= target else "active"
            completed_at = recorded_at if status == "completed" else None
            conn.execute("""
                UPDATE stability_observation_runs
                SET status = ?, completed_at = COALESCE(completed_at, ?),
                    last_evaluated_at = ?
                WHERE id = ?
            """, (status, completed_at, recorded_at, int(run["id"])))
            self.set_meta(conn, "stability_observation_last_session_date", date_text)
            self.set_meta(conn, "stability_observation_consecutive_pass_days", str(consecutive))
            self.set_meta(conn, "stability_observation_status", status)
        result = self.stability_observation_status(str(run["observation_key"]))
        result.update({"recorded": True, "passed": passed, "blockers": blockers})
        return result

    def stability_observation_status(self, observation_key=None):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if observation_key:
                run = conn.execute(
                    "SELECT * FROM stability_observation_runs WHERE observation_key = ?",
                    (str(observation_key),),
                ).fetchone()
            else:
                run = conn.execute("""
                    SELECT * FROM stability_observation_runs
                    ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, id DESC
                    LIMIT 1
                """).fetchone()
            if not run:
                return {"ok": True, "active": False, "status": "not_started", "days": []}
            day_rows = conn.execute("""
                SELECT * FROM stability_observation_days
                WHERE run_id = ? ORDER BY session_date
            """, (int(run["id"]),)).fetchall()
        days = []
        for row in day_rows:
            try:
                blockers = json.loads(row["blockers_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                blockers = []
            days.append({
                "sessionDate": row["session_date"],
                "recordedAt": row["recorded_at"],
                "passed": bool(row["passed"]),
                "blockers": blockers,
            })
        consecutive = 0
        for day in reversed(days):
            if not day["passed"]:
                break
            consecutive += 1
        try:
            scope = json.loads(run["scope_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            scope = []
        target = int(run["target_consecutive_sessions"] or 5)
        return {
            "ok": True,
            "active": str(run["status"]) == "active",
            "observationKey": run["observation_key"],
            "status": run["status"],
            "mode": run["mode"],
            "startedAt": run["started_at"],
            "startSessionDate": run["start_session_date"],
            "targetConsecutiveTradingDays": target,
            "observedTradingDays": len(days),
            "consecutivePassDays": consecutive,
            "remainingPassDays": max(0, target - consecutive),
            "completedAt": run["completed_at"],
            "lastEvaluatedAt": run["last_evaluated_at"],
            "scope": scope,
            "days": days,
        }

    def set_meta(self, conn, key, value):
        conn.execute("INSERT OR REPLACE INTO model_meta (key, value) VALUES (?, ?)", (key, str(value)))


class MarketContext:
    def __init__(self, rows_by_key):
        self.rows_by_key = rows_by_key
        self.dates_by_key = {
            key: [row["date"] for row in rows]
            for key, rows in rows_by_key.items()
        }

    def row_index(self, key, date):
        dates = self.dates_by_key.get(key) or []
        index = bisect.bisect_right(dates, date) - 1
        return index if index >= 0 else None

    def close(self, key, date, offset=0):
        rows = self.rows_by_key.get(key) or []
        index = self.row_index(key, date)
        if index is None:
            return 0.0
        index -= offset
        if index < 0:
            return 0.0
        return float(rows[index].get("close") or 0)

    def latest_available_date(self, key, date):
        rows = self.rows_by_key.get(key) or []
        index = self.row_index(key, date)
        if index is None:
            return None
        return rows[index].get("date")

    def ret(self, key, date, days):
        latest = self.close(key, date)
        base = self.close(key, date, days)
        if latest <= 0 or base <= 0:
            return 0.0
        return (latest - base) / base

    def ma_gap(self, key, date, period):
        rows = self.rows_by_key.get(key) or []
        index = self.row_index(key, date)
        if index is None or index + 1 < period:
            return 0.0
        window = rows[index - period + 1:index + 1]
        closes = [float(row.get("close") or 0) for row in window]
        average = sum(closes) / len(closes)
        latest = closes[-1]
        return (latest - average) / average if average else 0.0

    def ma_value(self, key, date, period):
        """回傳 date 當下、最近 period 根『已收盤』日K的均線值(月線=period 20)。
        給大盤紅綠燈用即時指數價 vs 月線重算 gap 用(ma_gap 是用當日收盤算,這裡
        只回均線本身,呼叫端可拿即時價自行算 (live-ma)/ma)。資料不足回 0.0。"""
        rows = self.rows_by_key.get(key) or []
        index = self.row_index(key, date)
        if index is None or index + 1 < period:
            return 0.0
        window = rows[index - period + 1:index + 1]
        closes = [float(row.get("close") or 0) for row in window]
        return sum(closes) / len(closes) if closes else 0.0


backend = StockMLBackend()
