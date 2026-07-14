from __future__ import annotations

import time

from ml_backend import backend, is_official_source

_runtime_health_provider = None
_runtime_quote_provider = None


def configure_brain_engine(health_provider=None, quote_provider=None):
    global _runtime_health_provider, _runtime_quote_provider
    if health_provider is not None:
        _runtime_health_provider = health_provider
    if quote_provider is not None:
        _runtime_quote_provider = quote_provider


def _runtime_health_snapshot():
    if callable(_runtime_health_provider):
        try:
            return _runtime_health_provider() or {}
        except Exception:
            return {}
    return {}


def _runtime_quote_for_symbol(symbol):
    if callable(_runtime_quote_provider):
        try:
            return _runtime_quote_provider(str(symbol or ''))
        except Exception:
            return None, ''
    return None, ''


def _brain_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _brain_pct(value):
    number = _brain_float(value)
    if number is None:
        return "無資料"
    return f"{number * 100:.1f}%"


def _brain_price(value):
    number = _brain_float(value)
    if number is None:
        return "無資料"
    return f"{number:.2f} 元"


def _brain_bool_text(value):
    if value is True:
        return "通過"
    if value is False:
        return "未通過"
    return "無資料"


def _brain_source_status(source):
    text = str(source or "").strip()
    if not text:
        return "無來源"
    if "yahoo" in text.lower():
        return "Yahoo fallback"
    if is_official_source(text):
        return "正式/授權來源"
    return "非正式來源"


def _brain_clamp(value, low=0.0, high=1.0):
    number = _brain_float(value)
    if number is None:
        return low
    return max(low, min(high, number))


def _brain_sma(values, period):
    numbers = [float(value) for value in values if value is not None]
    if len(numbers) < period:
        return None
    return sum(numbers[-period:]) / period


def _brain_kline_score(rows, prediction=None):
    prediction = prediction or {}
    if not rows or len(rows) < 25:
        return {
            "ok": False,
            "score": None,
            "text": "資料不足",
            "patterns": ["K線資料不足"],
            "components": {},
        }

    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else {}
    open_price = _brain_float(latest.get("open"))
    high = _brain_float(latest.get("high"))
    low = _brain_float(latest.get("low"))
    close = _brain_float(latest.get("close"))
    volume = _brain_float(latest.get("volume"))
    if not all(value is not None and value > 0 for value in (open_price, high, low, close, volume)) or high < low:
        return {
            "ok": False,
            "score": None,
            "text": "OHLCV不足",
            "patterns": ["OHLCV不足"],
            "components": {},
        }

    closes = [_brain_float(row.get("close")) for row in rows]
    volumes = [_brain_float(row.get("volume")) for row in rows]
    ma5 = _brain_sma(closes, 5)
    ma20 = _brain_sma(closes, 20)
    ma60 = _brain_sma(closes, 60)
    prior_volumes = [value for value in volumes[-21:-1] if value is not None and value > 0]
    if len(prior_volumes) < 5:
        prior_volumes = [value for value in volumes[-20:] if value is not None and value > 0]
    avg20_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else None
    volume_ratio = volume / avg20_volume if avg20_volume else None

    candle_range = max(high - low, close * 0.002, 0.01)
    body_pct = abs(close - open_price) / candle_range
    upper_shadow = max(high - max(open_price, close), 0) / candle_range
    lower_shadow = max(min(open_price, close) - low, 0) / candle_range
    close_near_high = (high - close) / candle_range
    bullish = close >= open_price
    previous_high = _brain_float(previous.get("high"))
    previous_close = _brain_float(previous.get("close"))

    patterns = []
    pattern_score = 0.45
    if bullish and body_pct >= 0.45:
        pattern_score += 0.20
        patterns.append("實體紅K")
    if lower_shadow >= 0.35 and bullish:
        pattern_score += 0.16
        patterns.append("下影線承接")
    if close_near_high <= 0.20:
        pattern_score += 0.10
        patterns.append("收近高點")
    if previous_high and close > previous_high:
        pattern_score += 0.14
        patterns.append("突破前高")
    if previous_close and open_price > previous_close * 1.035:
        pattern_score -= 0.08
        patterns.append("跳空偏高")
    if not bullish and body_pct >= 0.45:
        pattern_score -= 0.18
        patterns.append("實體黑K")
    if upper_shadow >= 0.40 and lower_shadow < 0.25:
        pattern_score -= 0.12
        patterns.append("上影線壓力")
    if not patterns:
        patterns.append("無明顯型態")
    pattern_score = _brain_clamp(pattern_score)

    # 量能是動能訊號，量能比值越高分數不該反而變低——原本 2.50/4.00 兩個
    # 邊界會讓分數從 0.90 驟降到 0.78 再驟降到 0.62，爆量股分數比溫和放量股
    # 還低，違背設計初衷。改成到 3.00 倍量之前持續爬升、之後打平在高分區，
    # 全程單調不遞減。
    if volume_ratio is None:
        volume_score = 0.50
    elif volume_ratio < 0.70:
        volume_score = 0.35
    elif volume_ratio <= 1.20:
        volume_score = 0.55
    elif volume_ratio <= 3.00:
        volume_score = 0.75 + min((volume_ratio - 1.20) / 1.80, 1) * 0.15
    else:
        volume_score = 0.90
    volume_score = _brain_clamp(volume_score)

    # 用嚴格 > 而非 >=：長期無量/一價鎖死的股票(close==ma5==ma20==ma60)才不會
    # 被四個條件同時判定成立，誤判成滿分多頭排列，跟真正價漲、5MA>20MA>60MA
    # 的多頭排列股票混在一起分不出來。
    ma_score = 0.25
    if ma5 and close > ma5:
        ma_score += 0.20
    if ma20 and close > ma20:
        ma_score += 0.25
    if ma5 and ma20 and ma5 > ma20:
        ma_score += 0.15
    if ma20 and ma60 and ma20 > ma60:
        ma_score += 0.15
    ma_score = _brain_clamp(ma_score)

    market_gate = prediction.get("marketGate") or {}
    trade_gate = prediction.get("tradeGate") or {}
    market_score = 0.50
    if market_gate.get("stockStrongerThanTaiex") or trade_gate.get("strongerThanMarket"):
        market_score += 0.25
    else:
        market_score -= 0.10
    if market_gate.get("allowBuy") or trade_gate.get("marketOk"):
        market_score += 0.15
    if market_gate.get("hotMarket"):
        market_score += 0.05
    if market_gate.get("taiexAboveMonthLine") is False:
        market_score -= 0.10
    market_score = _brain_clamp(market_score)

    # 模型沒產出結果(逾時/載入失敗/例外/資料格式不對)不能等同「模型100%看
    # 空」——volume_score/market_score 缺值時都給 0.50 中性分，model_score
    # 也要比照辦理，不能因為基礎設施故障就把整體分數系統性拉低。
    model_probability = _brain_float(prediction.get("probability"))
    model_score = 0.50 if model_probability is None else _brain_clamp(model_probability, 0.0, 1.0)
    # 2026-07-07 純規則化:拿掉模型那 15%,依比例重分配給型態量能。model_score 仍計算+
    # 放進 components 供「僅供參考」顯示,只是不進這個 score。
    score = _brain_clamp(
        pattern_score * 0.35 +
        volume_score * 0.24 +
        ma_score * 0.24 +
        market_score * 0.17
    )

    return {
        "ok": True,
        "score": score,
        "text": f"{score * 100:.1f}%",
        "patterns": patterns,
        "components": {
            "pattern": pattern_score,
            "volume": volume_score,
            "ma": ma_score,
            "market": market_score,
            "model": model_score,
        },
        "volumeRatio": volume_ratio,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "bodyPct": body_pct,
        "upperShadow": upper_shadow,
        "lowerShadow": lower_shadow,
    }


def _brain_indicator_score_text(score):
    number = _brain_float(score)
    if number is None:
        return "無資料"
    return f"{number * 100:.1f}%"


def _brain_number_text(value, digits=2):
    number = _brain_float(value)
    if number is None:
        return "無資料"
    return f"{number:.{digits}f}"


def _brain_rsi(closes, period=14):
    values = [_brain_float(value) for value in closes]
    values = [value for value in values if value is not None]
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for index in range(len(values) - period, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _brain_ema_series(values, period):
    numbers = [_brain_float(value) for value in values]
    if len(numbers) < period or any(value is None for value in numbers[:period]):
        return []
    multiplier = 2 / (period + 1)
    ema_values = []
    ema = sum(numbers[:period]) / period
    for index, value in enumerate(numbers):
        if value is None:
            ema_values.append(None)
            continue
        if index < period - 1:
            ema_values.append(None)
            continue
        if index == period - 1:
            ema_values.append(ema)
            continue
        ema = (value - ema) * multiplier + ema
        ema_values.append(ema)
    return ema_values


def _brain_macd(closes, fast=12, slow=26, signal_period=9):
    if not closes or len(closes) < slow + signal_period + 2:
        return {"macd": None, "signal": None, "hist": None, "prevHist": None}
    fast_ema = _brain_ema_series(closes, fast)
    slow_ema = _brain_ema_series(closes, slow)
    macd_line = []
    for fast_value, slow_value in zip(fast_ema, slow_ema):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
        else:
            macd_line.append(fast_value - slow_value)
    valid_macd = [value for value in macd_line if value is not None]
    signal_values = _brain_ema_series(valid_macd, signal_period)
    if len(signal_values) < 2:
        return {"macd": None, "signal": None, "hist": None, "prevHist": None}
    macd = valid_macd[-1]
    signal = signal_values[-1]
    prev_macd = valid_macd[-2]
    prev_signal = signal_values[-2]
    if signal is None or prev_signal is None:
        return {"macd": None, "signal": None, "hist": None, "prevHist": None}
    hist = macd - signal
    prev_hist = prev_macd - prev_signal
    return {
        "macd": macd,
        "signal": signal,
        "hist": hist,
        "prevHist": prev_hist,
        "histGrowing": hist > prev_hist,
        "greenShrinking": hist < 0 and hist > prev_hist,
        "redTurning": prev_hist <= 0 < hist,
        "bullCrossAboveZero": prev_macd <= prev_signal and macd > signal and macd > 0 and signal > 0,
    }


def _brain_kd(rows, period=9, k_period=3, d_period=3):
    if not rows or len(rows) < period + 2:
        return {"k": None, "d": None, "prevK": None, "prevD": None}
    k_values = []
    d_values = []
    k = 50.0
    d = 50.0
    for end in range(period - 1, len(rows)):
        if end < period - 1:
            continue
        window = rows[end - period + 1:end + 1]
        highs = [_brain_float(row.get("high")) for row in window]
        lows = [_brain_float(row.get("low")) for row in window]
        close = _brain_float(rows[end].get("close"))
        highs = [value for value in highs if value is not None]
        lows = [value for value in lows if value is not None]
        if not highs or not lows or close is None:
            continue
        highest = max(highs)
        lowest = min(lows)
        if highest <= lowest:
            rsv = 50.0
        else:
            rsv = (close - lowest) / (highest - lowest) * 100
        k = ((k_period - 1) * k + rsv) / k_period
        d = ((d_period - 1) * d + k) / d_period
        k_values.append(k)
        d_values.append(d)
    if not k_values:
        return {"k": None, "d": None, "prevK": None, "prevD": None}
    return {
        "k": k_values[-1],
        "d": d_values[-1],
        "prevK": k_values[-2] if len(k_values) >= 2 else None,
        "prevD": d_values[-2] if len(d_values) >= 2 else None,
    }


def _brain_score_rsi(rsi):
    # 妖股短線基準：RSI 70-85 是飆股的常態強勢區，不當弱勢懲罰；
    # 只有 RSI > 85 才視為過熱警戒。
    rsi = _brain_float(rsi)
    if rsi is None:
        return None
    if 45 <= rsi <= 70:
        return 0.82
    if 70 < rsi <= 85:
        return 0.75
    if 35 <= rsi < 45:
        return 0.62
    if 25 <= rsi < 35:
        return 0.50
    if rsi > 85:
        return 0.50
    return 0.25


def _brain_score_kd(k, d, prev_k=None, prev_d=None):
    k = _brain_float(k)
    d = _brain_float(d)
    if k is None or d is None:
        return None
    prev_k = _brain_float(prev_k)
    prev_d = _brain_float(prev_d)
    golden_cross = prev_k is not None and prev_d is not None and prev_k <= prev_d and k > d
    death_cross = prev_k is not None and prev_d is not None and prev_k >= prev_d and k < d
    # 妖股短線基準：K 值高檔（>75）且多方排列是飆股強勢延續的常態，
    # 不因 K 高就自動降分；只有真正死亡交叉才視為出場訊號。
    # 黃金交叉分數階梯：低檔交叉最強，隨 K 值墊高遞減，且每一檔都不低於
    # 同區間「無交叉純多方排列」的分數，交叉事件不能反而比沒交叉低分。
    if golden_cross and k < 20:
        return 0.90
    if golden_cross and k <= 50:
        return 0.86
    if golden_cross and k <= 75:
        return 0.84
    if golden_cross:
        return 0.80
    if death_cross and k > 80:
        return 0.25
    if death_cross and k >= 65:
        return 0.32
    if death_cross:
        return 0.38
    if k > d and 20 <= k <= 75:
        return 0.82
    if k > d and k > 75:
        return 0.78
    if k > d:
        return 0.72
    if k >= 85 and k < d:
        # 高檔且 K 已在 D 之下（空方排列延續中），視為轉弱，
        # 分數要落在 0.55 的通過門檻之下
        return 0.45
    if k >= 85:
        return 0.55
    if k < 20:
        return 0.42
    return 0.50


def _brain_score_volume(rows, close, previous_close):
    latest_volume = _brain_float((rows[-1] if rows else {}).get("volume"))
    volumes = [_brain_float(row.get("volume")) for row in rows or []]
    prior = [value for value in volumes[-21:-1] if value is not None and value > 0]
    if len(prior) < 5:
        prior = [value for value in volumes[-20:] if value is not None and value > 0]
    if not latest_volume or not prior:
        return None, None, "量能無資料"
    avg_volume = sum(prior) / len(prior)
    volume_ratio = latest_volume / avg_volume if avg_volume else None
    price_change = None
    if close is not None and previous_close:
        price_change = (close - previous_close) / previous_close
    if volume_ratio is None:
        return None, None, "量能無資料"
    # 爆量偏熱(>4.5 倍且價漲)必須排在「量增價揚(>=1.5)」之前判斷,否則永遠被 1.5 那條
    # 攔截、降權從不生效(爆量倒貨/追高風險反而給 0.88 高分)。妖股哲學:爆量偏熱要扣分。
    if volume_ratio > 4.5 and price_change is not None and price_change > 0:
        return 0.66, volume_ratio, "爆量偏熱"
    if volume_ratio >= 1.5 and price_change is not None and price_change > 0:
        return 0.88, volume_ratio, "量增價揚"
    if volume_ratio >= 1.2 and price_change is not None and price_change > 0:
        return 0.76, volume_ratio, "溫和放量上漲"
    if volume_ratio >= 1.5 and price_change is not None and price_change <= 0:
        return 0.42, volume_ratio, "放量但價格未跟上"
    if volume_ratio < 0.8:
        return 0.38, volume_ratio, "無量，容易盤整或回檔"
    return 0.55, volume_ratio, "量能普通"


def _brain_obv(rows):
    if not rows or len(rows) < 12:
        return {
            "ok": False,
            "score": None,
            "text": "OBV資料不足",
            "missing": ["OBV至少12日價量"],
        }

    closes = [_brain_float(row.get("close")) for row in rows]
    volumes = [_brain_float(row.get("volume")) for row in rows]
    if any(value is None or value <= 0 for value in closes[-12:]) or any(value is None or value <= 0 for value in volumes[-12:]):
        return {
            "ok": False,
            "score": None,
            "text": "OBV價量不足",
            "missing": ["收盤價", "成交量"],
        }

    obv = [0.0]
    for index in range(1, len(rows)):
        close = closes[index]
        previous_close = closes[index - 1]
        volume = volumes[index]
        previous_obv = obv[-1]
        if close is None or previous_close is None or volume is None:
            obv.append(previous_obv)
        elif close > previous_close:
            obv.append(previous_obv + volume)
        elif close < previous_close:
            obv.append(previous_obv - volume)
        else:
            obv.append(previous_obv)

    latest = obv[-1]
    ma10 = _brain_sma(obv, 10)
    delta5 = latest - obv[-6] if len(obv) >= 6 else None
    avg_volume20 = _brain_sma([value for value in volumes if value is not None and value > 0], 20)
    if avg_volume20 is None:
        avg_volume20 = _brain_sma([value for value in volumes if value is not None and value > 0], min(10, len(volumes)))
    delta_ratio = delta5 / max(avg_volume20, 1) if delta5 is not None and avg_volume20 else None
    price_change5 = None
    if len(closes) >= 6 and closes[-6]:
        price_change5 = (closes[-1] - closes[-6]) / closes[-6]

    above_ma = ma10 is not None and latest >= ma10
    rising = delta5 is not None and delta5 > 0
    falling = delta5 is not None and delta5 < 0
    price_up = price_change5 is not None and price_change5 > 0
    price_down = price_change5 is not None and price_change5 < 0

    if above_ma and rising and price_up:
        score = 0.86
        text = "OBV量價同步轉強"
        ok = True
    elif above_ma and rising:
        score = 0.76
        text = "OBV資金偏流入"
        ok = True
    elif rising:
        score = 0.64
        text = "OBV回升"
        ok = None
    elif falling and price_down:
        score = 0.30
        text = "OBV量價同步轉弱"
        ok = False
    elif falling:
        score = 0.42
        text = "OBV資金偏流出"
        ok = False
    else:
        score = 0.50
        text = "OBV持平"
        ok = None

    return {
        "ok": ok,
        "score": _brain_clamp(score),
        "text": text,
        "latest": latest,
        "ma10": ma10,
        "delta5": delta5,
        "deltaRatio": delta_ratio,
        "priceChange5": price_change5,
        "aboveMa10": above_ma,
        "rising": rising,
        "missing": [],
    }


def _brain_score_macd(macd):
    if not macd or _brain_float(macd.get("hist")) is None:
        return None
    hist = _brain_float(macd.get("hist"))
    prev_hist = _brain_float(macd.get("prevHist"))
    if macd.get("redTurning") or macd.get("bullCrossAboveZero"):
        return 0.88
    if hist is not None and prev_hist is not None and hist > 0 and hist > prev_hist:
        return 0.78
    if macd.get("greenShrinking"):
        return 0.68
    if hist is not None and prev_hist is not None and hist < prev_hist:
        return 0.35
    return 0.50


def _brain_technical_indicator_score(rows):
    if not rows or len(rows) < 35:
        return {
            "ok": False,
            "score": None,
            "text": "資料不足",
            "components": {},
            "missing": ["5MA/10MA", "量能", "OBV", "KD(9,3,3)", "MACD", "RSI"],
        }
    closes = [_brain_float(row.get("close")) for row in rows]
    close = _brain_float(rows[-1].get("close"))
    previous_close = _brain_float(rows[-2].get("close")) if len(rows) >= 2 else None
    ma5 = _brain_sma(closes, 5)
    ma10 = _brain_sma(closes, 10)
    prev_ma5 = _brain_sma(closes[:-1], 5)
    rsi = _brain_rsi(closes)
    kd = _brain_kd(rows)
    macd = _brain_macd(closes)
    volume_score, volume_ratio, volume_text = _brain_score_volume(rows, close, previous_close)
    obv = _brain_obv(rows)

    ma_score = None
    if close is not None and ma5 is not None and ma10 is not None:
        ma_score = 0.25
        if close >= ma5:
            ma_score += 0.30
        if close >= ma10:
            ma_score += 0.18
        if ma5 >= ma10:
            ma_score += 0.20
        if prev_ma5 is not None and ma5 >= prev_ma5:
            ma_score += 0.07
        ma_score = _brain_clamp(ma_score)

    rsi_score = _brain_score_rsi(rsi)
    kd_score = _brain_score_kd(kd.get("k"), kd.get("d"), kd.get("prevK"), kd.get("prevD"))
    macd_score = _brain_score_macd(macd)
    components = {
        "ma": ma_score,
        "volume": volume_score,
        "obv": obv.get("score"),
        "kd": kd_score,
        "macd": macd_score,
        "rsi": rsi_score,
    }
    weighted = []
    if ma_score is not None:
        weighted.append((ma_score, 0.22))
    if volume_score is not None:
        weighted.append((volume_score, 0.20))
    if obv.get("score") is not None:
        weighted.append((obv.get("score"), 0.15))
    if kd_score is not None:
        weighted.append((kd_score, 0.18))
    if macd_score is not None:
        weighted.append((macd_score, 0.17))
    if rsi_score is not None:
        weighted.append((rsi_score, 0.08))
    weight_sum = sum(weight for _, weight in weighted)
    score = sum(value * weight for value, weight in weighted) / weight_sum if weight_sum else None
    missing = [
        label for key, label in (
            ("ma", "5MA/10MA"),
            ("volume", "成交量"),
            ("obv", "OBV能量潮"),
            ("kd", "KD(9,3,3)"),
            ("macd", "MACD"),
            ("rsi", "RSI"),
        )
        if components.get(key) is None
    ]
    return {
        "ok": score is not None,
        "score": score,
        "text": _brain_indicator_score_text(score),
        "components": components,
        "missing": missing,
        "ma5": ma5,
        "ma10": ma10,
        "rsi": rsi,
        "kd": kd,
        "macd": macd,
        "obv": obv,
        "volumeRatio": volume_ratio,
        "volumeText": volume_text,
    }


def _brain_volume_lots(volume):
    number = _brain_float(volume)
    if number is None or number <= 0:
        return None
    return number / 1000.0


def _brain_signed_lots_text(value):
    number = _brain_float(value)
    if number is None:
        return "無資料"
    return f"{number:+.0f} 張"


def _brain_ratio_text(value):
    number = _brain_float(value)
    if number is None:
        return "無資料"
    return f"{number * 100:.1f}%"


def _brain_short_money_detail(key, label, ok, score, value, source="", note="", missing=None):
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "score": score,
        "value": value,
        "source": source or "無資料",
        "note": note,
        "missing": missing or [],
    }


def _brain_institutional_flow_score(rows):
    verified = [
        row for row in (rows or [])[-5:]
        if is_official_source(row.get("chip_source"))
        and (row.get("foreign_buy_sell") is not None or row.get("trust_buy_sell") is not None)
    ]
    if not verified:
        return _brain_short_money_detail(
            "institutionalFlow",
            "外資/投信連買佔量",
            None,
            None,
            "無正式外資/投信資料",
            missing=["外資買賣超", "投信買賣超"],
        )
    latest = verified[-1]
    latest_net = (_brain_float(latest.get("foreign_buy_sell"), 0) or 0) + (_brain_float(latest.get("trust_buy_sell"), 0) or 0)
    latest_volume_lots = _brain_volume_lots(latest.get("volume"))
    net_ratio = latest_net / latest_volume_lots if latest_volume_lots else None
    streak = 0
    for row in reversed(verified):
        net = (_brain_float(row.get("foreign_buy_sell"), 0) or 0) + (_brain_float(row.get("trust_buy_sell"), 0) or 0)
        if net > 0:
            streak += 1
            if streak >= 3:
                break
        else:
            break
    if net_ratio is not None and latest_net > 0 and net_ratio >= 0.10 and streak >= 3:
        score = 0.95
        ok = True
    elif net_ratio is not None and latest_net > 0 and net_ratio >= 0.10 and streak >= 1:
        score = 0.88
        ok = True
    elif net_ratio is not None and latest_net > 0 and net_ratio >= 0.03:
        score = 0.65
        ok = True
    elif latest_net > 0:
        score = 0.55
        ok = True
    elif latest_net < 0:
        score = 0.35
        ok = False
    else:
        score = 0.50
        ok = None
    return _brain_short_money_detail(
        "institutionalFlow",
        "外資/投信連買佔量",
        ok,
        score,
        f"外資+投信 {_brain_signed_lots_text(latest_net)} / 連買 {streak} 天 / 佔量 {_brain_ratio_text(net_ratio)}",
        source=latest.get("chip_source"),
        note="目前未含自營商；三大法人完整判斷需自營商資料",
        missing=[] if net_ratio is not None else ["成交量"],
    )


def _brain_turnover_proxy_score(latest, rows=None):
    latest = latest or {}
    value = _brain_float(latest.get("day_trade_ratio"))
    source = latest.get("price_source") or latest.get("finance_source") or ""
    value_label = "當沖/換手代理"
    note = "真週轉率公式需流通股數，尚未用假資料代替"
    if value is None:
        volumes = [_brain_float(row.get("volume")) for row in (rows or [])[-21:-1]]
        volumes = [item for item in volumes if item is not None and item > 0]
        latest_volume = _brain_float(latest.get("volume"))
        avg_volume = sum(volumes) / len(volumes) if volumes else None
        if latest_volume is not None and avg_volume and avg_volume > 0:
            value = latest_volume / avg_volume
            value_label = "量能換手代理"
            note = "缺流通股數與當沖資料，先用今日量/20日均量衡量市場換手活躍度"
        else:
            return _brain_short_money_detail(
                "turnoverProxy",
                "週轉/換手代理",
                None,
                None,
                "無資料",
                source=source,
                note="真週轉率需流通股數；目前可用當沖或量能代理",
                missing=["流通股數", "當沖週轉資料", "20日均量"],
            )
    if value_label == "量能換手代理":
        if value >= 3.0:
            score = 0.88
            ok = True
        elif value >= 1.5:
            score = 0.76
            ok = True
        elif value >= 1.0:
            score = 0.58
            ok = None
        elif value >= 0.7:
            score = 0.48
            ok = False
        else:
            score = 0.38
            ok = False
    elif value >= 0.12:
        score = 0.90
        ok = True
    elif value >= 0.05:
        score = 0.82
        ok = True
    elif value >= 0.02:
        score = 0.55
        ok = None
    else:
        score = 0.42
        ok = False
    return _brain_short_money_detail(
        "turnoverProxy",
        "週轉/換手代理",
        ok,
        score,
        f"{value_label} {_brain_ratio_text(value)}",
        source=source,
        note=note,
    )


def _brain_branch_flow_score(latest):
    latest = latest or {}
    source = latest.get("branch_flow_source")
    if not is_official_source(source):
        return _brain_short_money_detail(
            "branchFlow",
            "主力分點",
            None,
            None,
            "無正式分點資料",
            source=source,
            missing=["券商分點淨買賣"],
        )
    branch_net = _brain_float(latest.get("broker_branch_net_buy"))
    main_force = _brain_float(latest.get("main_force_buy_sell"))
    chosen = main_force if main_force is not None else branch_net
    if chosen is None:
        return _brain_short_money_detail(
            "branchFlow",
            "主力分點",
            None,
            None,
            "無資料",
            source=source,
            missing=["主力分點淨買賣"],
        )
    volume_lots = _brain_volume_lots(latest.get("volume"))
    ratio = chosen / volume_lots if volume_lots else None
    if ratio is not None and chosen > 0 and ratio >= 0.08:
        score = 0.88
        ok = True
    elif ratio is not None and chosen > 0 and ratio >= 0.03:
        score = 0.72
        ok = True
    elif chosen > 0:
        score = 0.58
        ok = None
    elif chosen < 0:
        score = 0.34
        ok = False
    else:
        score = 0.50
        ok = None
    return _brain_short_money_detail(
        "branchFlow",
        "主力分點",
        ok,
        score,
        f"主力淨買 {_brain_signed_lots_text(chosen)} / 佔量 {_brain_ratio_text(ratio)}",
        source=source,
        note="目前只看分點淨買賣，尚未取得美林/摩根等分點名稱",
        missing=[] if ratio is not None else ["成交量"],
    )


def _brain_realtime_flow_score(latest):
    latest = latest or {}
    source = latest.get("realtime_flow_source")
    if not is_official_source(source):
        return _brain_short_money_detail(
            "realtimeFlow",
            "盤中外盤/大單",
            None,
            None,
            "無正式即時資金流",
            source=source,
            note="需盤中 Shioaji Tick collector",
            missing=["即時資金流", "即時大單流向"],
        )
    money_flow = _brain_float(latest.get("realtime_money_flow"))
    large_order_flow = _brain_float(latest.get("realtime_large_order_flow"))
    if money_flow is None and large_order_flow is None:
        return _brain_short_money_detail(
            "realtimeFlow",
            "盤中外盤/大單",
            None,
            None,
            "無資料",
            source=source,
            note="需盤中 Shioaji Tick collector",
            missing=["即時資金流", "即時大單流向"],
        )
    close = _brain_float(latest.get("close"))
    volume = _brain_float(latest.get("volume"))
    traded_amount = close * volume if close and volume else None
    money_ratio = money_flow / traded_amount if money_flow is not None and traded_amount else None
    large_ratio = large_order_flow / volume if large_order_flow is not None and volume else None
    combined = 0.0
    weight = 0.0
    if money_ratio is not None:
        combined += _brain_clamp(0.50 + money_ratio * 18) * 0.6
        weight += 0.6
    if large_ratio is not None:
        combined += _brain_clamp(0.50 + large_ratio * 4) * 0.4
        weight += 0.4
    score = combined / weight if weight else None
    ok = None if score is None else score >= 0.60
    if score is not None and score < 0.45:
        ok = False
    return _brain_short_money_detail(
        "realtimeFlow",
        "盤中外盤/大單",
        ok,
        score,
        f"即時資金流 {_brain_number_text(money_flow, 0)} / 大單 {_brain_number_text(large_order_flow, 0)}",
        source=source,
        note="tick_type=1 視為外盤主動買，tick_type=2 視為內盤主動賣",
    )


def _brain_intraday_quote_for_symbol(symbol):
    return _runtime_quote_for_symbol(symbol)


def _brain_projected_volume_score(symbol, rows):
    quote, source = _brain_intraday_quote_for_symbol(symbol)
    now = time.localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    active = 9 * 60 <= minutes <= 9 * 60 + 15
    if not active:
        return _brain_short_money_detail(
            "projectedVolume",
            "09:00-09:15預估量",
            None,
            None,
            "僅 09:00-09:15 評估",
            source=source or "Shioaji quote",
        )
    if not quote:
        return _brain_short_money_detail(
            "projectedVolume",
            "09:00-09:15預估量",
            None,
            None,
            "等待 Shioaji quote",
            source="Shioaji quote",
            missing=["盤中成交量"],
        )
    volume_lots = _brain_float(quote.get("totalVolume"))
    previous_volume_lots = _brain_volume_lots((rows[-1] if rows else {}).get("volume"))
    elapsed = max(minutes - 9 * 60 + 1, 1)
    if not volume_lots or not previous_volume_lots:
        return _brain_short_money_detail(
            "projectedVolume",
            "09:00-09:15預估量",
            None,
            None,
            "成交量不足",
            source=source,
            missing=["盤中成交量", "前一日成交量"],
        )
    projected_lots = volume_lots / elapsed * 270
    ratio = projected_lots / previous_volume_lots
    if ratio >= 1.5:
        score = 0.88
        ok = True
    elif ratio >= 1.1:
        score = 0.62
        ok = None
    else:
        score = 0.38
        ok = False
    return _brain_short_money_detail(
        "projectedVolume",
        "09:00-09:15預估量",
        ok,
        score,
        f"預估量 {_brain_ratio_text(ratio)} 前日量",
        source=source,
        note="用 09:00-09:15 Shioaji 累積量推估全日量",
    )


def _brain_short_money_score(symbol, rows, latest):
    details = [
        _brain_turnover_proxy_score(latest, rows),
        _brain_institutional_flow_score(rows),
        _brain_branch_flow_score(latest),
        _brain_realtime_flow_score(latest),
        _brain_projected_volume_score(symbol, rows),
    ]
    weights = {
        "turnoverProxy": 0.20,
        "institutionalFlow": 0.25,
        "branchFlow": 0.20,
        "realtimeFlow": 0.20,
        "projectedVolume": 0.15,
    }
    weighted = [
        (detail.get("score"), weights.get(detail.get("key"), 0))
        for detail in details
        if detail.get("score") is not None
    ]
    weight_sum = sum(weight for _, weight in weighted)
    score = sum(value * weight for value, weight in weighted) / weight_sum if weight_sum else None
    missing = []
    for detail in details:
        missing.extend(detail.get("missing") or [])
    passed = sum(1 for detail in details if detail.get("ok") is True)
    available = sum(1 for detail in details if detail.get("score") is not None)
    return {
        "ok": score is not None,
        "score": score,
        "text": _brain_indicator_score_text(score),
        "passed": passed,
        "available": available,
        "missing": sorted(set(missing)),
        "details": details,
    }


def _brain_strategy_detail(key, label, ok, score, value, missing=None):
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "score": score,
        "value": value,
        "missing": missing or [],
    }


def _brain_recent_net_buy_streak(rows, key, days=3):
    values = [_brain_float(row.get(key)) for row in (rows or [])[-days:]]
    if len(values) < days or any(value is None for value in values):
        return None
    return all(value > 0 for value in values)


def _brain_decision_flow_score(rows, technical=None, short_money=None):
    """交易決策流程評分：把進場/持有/離場/強制離場與反轉10指標量化。

    這是決策層規則，不改 ML feature schema；先進 Brain/紙上績效觀察。
    """
    if not rows or len(rows) < 65:
        return {
            "ok": False,
            "score": None,
            "text": "資料不足",
            "entryScore": None,
            "holdScore": None,
            "exitScore": None,
            "riskScore": None,
            "reversalScore": None,
            "details": [],
            "missing": ["至少65日日線價量"],
        }
    technical = technical or {}
    short_money = short_money or {}
    closes = [_brain_float(row.get("close")) for row in rows]
    highs = [_brain_float(row.get("high")) for row in rows]
    lows = [_brain_float(row.get("low")) for row in rows]
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else latest
    close = _brain_float(latest.get("close"))
    prev_close = _brain_float(previous.get("close"))
    ma5 = _brain_sma(closes, 5)
    ma10 = _brain_sma(closes, 10)
    ma20 = _brain_sma(closes, 20)
    ma60 = _brain_sma(closes, 60)
    prev_ma5 = _brain_sma(closes[:-1], 5)
    prev_ma20 = _brain_sma(closes[:-1], 20)
    prev_ma60 = _brain_sma(closes[:-1], 60)
    rsi = _brain_float(technical.get("rsi")) if technical.get("rsi") is not None else _brain_rsi(closes)
    kd = technical.get("kd") or _brain_kd(rows)
    macd = technical.get("macd") or _brain_macd(closes)
    volume_ratio = _brain_float(technical.get("volumeRatio"))
    if volume_ratio is None:
        _, volume_ratio, _ = _brain_score_volume(rows, close, prev_close)

    k = _brain_float(kd.get("k"))
    d = _brain_float(kd.get("d"))
    prev_k = _brain_float(kd.get("prevK"))
    prev_d = _brain_float(kd.get("prevD"))
    hist = _brain_float(macd.get("hist"))
    prev_hist = _brain_float(macd.get("prevHist"))
    macd_line = _brain_float(macd.get("macd"))
    macd_signal = _brain_float(macd.get("signal"))
    macd_golden = bool(macd.get("redTurning") or macd.get("bullCrossAboveZero"))
    macd_death = hist is not None and prev_hist is not None and prev_hist >= 0 and hist < 0
    macd_above_zero = macd_line is not None and macd_signal is not None and macd_line > 0 and macd_signal > 0
    kd_golden = prev_k is not None and prev_d is not None and k is not None and d is not None and prev_k <= prev_d and k > d
    kd_death = prev_k is not None and prev_d is not None and k is not None and d is not None and prev_k >= prev_d and k < d
    ma_bull = close is not None and ma5 is not None and ma20 is not None and ma60 is not None and ma5 > ma20 > ma60 and close >= ma5
    ma20_up = ma20 is not None and prev_ma20 is not None and ma20 >= prev_ma20
    ma60_up = ma60 is not None and prev_ma60 is not None and ma60 >= prev_ma60
    close_above_ma20 = close is not None and ma20 is not None and close >= ma20
    ma5_cross_up_20 = prev_ma5 is not None and prev_ma20 is not None and ma5 is not None and ma20 is not None and prev_ma5 <= prev_ma20 and ma5 > ma20
    ma5_cross_down_20 = prev_ma5 is not None and prev_ma20 is not None and ma5 is not None and ma20 is not None and prev_ma5 >= prev_ma20 and ma5 < ma20
    recent_high = max([value for value in highs[-61:-1] if value is not None], default=None)
    breakout_high = close is not None and recent_high is not None and close > recent_high
    platform_high = max([value for value in highs[-21:-1] if value is not None], default=None)
    platform_breakout = close is not None and platform_high is not None and close >= platform_high
    boll_values = [value for value in closes[-20:] if value is not None]
    boll_mid = sum(boll_values) / len(boll_values) if len(boll_values) >= 20 else None
    boll_std = None
    if boll_mid is not None:
        variance = sum((value - boll_mid) ** 2 for value in boll_values) / len(boll_values)
        boll_std = variance ** 0.5
    boll_upper = boll_mid + 2 * boll_std if boll_mid is not None and boll_std is not None else None
    boll_mid_ok = close is not None and boll_mid is not None and close >= boll_mid
    boll_upper_break = close is not None and boll_upper is not None and close >= boll_upper
    volume_expanded = volume_ratio is not None and volume_ratio >= 1.5
    volume_normal = volume_ratio is not None and 0.8 <= volume_ratio <= 4.5
    foreign_3_buy = _brain_recent_net_buy_streak(rows, "foreign_buy_sell", 3)
    trust_3_buy = _brain_recent_net_buy_streak(rows, "trust_buy_sell", 3)
    institutional_3_buy = bool(foreign_3_buy or trust_3_buy)
    foreign_sell = _brain_float(latest.get("foreign_buy_sell")) is not None and _brain_float(latest.get("foreign_buy_sell")) < 0
    trust_sell = _brain_float(latest.get("trust_buy_sell")) is not None and _brain_float(latest.get("trust_buy_sell")) < 0
    institutional_sell = bool(foreign_sell or trust_sell)
    ret1 = (close - prev_close) / prev_close if close is not None and prev_close and prev_close > 0 else None
    ret5 = (close - closes[-6]) / closes[-6] if close is not None and len(closes) >= 6 and closes[-6] else None
    single_drop = ret1 is not None and ret1 <= -0.08
    break_ma20 = close is not None and ma20 is not None and close < ma20
    rsi_exit = rsi is not None and rsi < 50
    bearish_volume = ret1 is not None and ret1 < 0 and volume_ratio is not None and volume_ratio >= 1.5
    large_drop = ret5 is not None and ret5 <= -0.08
    higher_low = len(lows) >= 6 and lows[-1] is not None and lows[-3] is not None and lows[-5] is not None and lows[-1] >= lows[-3] >= lows[-5]
    trendline_broken = len(lows) >= 6 and lows[-1] is not None and lows[-3] is not None and lows[-5] is not None and lows[-1] < min(lows[-3], lows[-5])

    def bool_score(items):
        known = [item for item in items if item is not None]
        return sum(1 for item in known if item) / len(known) if known else None

    entry_checks = [
        ma_bull, macd_golden or (hist is not None and hist > 0 and (prev_hist is None or hist >= prev_hist)),
        volume_expanded, platform_breakout, institutional_3_buy, rsi is not None and rsi > 50,
    ]
    hold_checks = [
        ma20_up, macd_above_zero or (hist is not None and hist >= 0), not break_ma20,
        volume_normal, ma60_up,
    ]
    exit_checks = [macd_death, break_ma20, rsi_exit, trendline_broken, bearish_volume, institutional_sell]
    force_exit_checks = [single_drop, large_drop, ma5_cross_down_20, close is not None and ma60 is not None and close < ma60]
    reversal_checks = [
        macd_golden,
        rsi is not None and rsi > 30 and rsi > 50,
        kd_golden,
        close_above_ma20,
        ma5_cross_up_20,
        boll_mid_ok or boll_upper_break,
        volume_expanded,
        breakout_high or platform_breakout,
        institutional_3_buy,
        (macd_golden or (rsi is not None and rsi > 50)) and close_above_ma20,
    ]
    entry_score = bool_score(entry_checks)
    hold_score = bool_score(hold_checks)
    exit_score = bool_score(exit_checks)
    risk_score = bool_score(force_exit_checks)
    reversal_score = bool_score(reversal_checks)
    positive_parts = [value for value in (entry_score, hold_score, reversal_score) if value is not None]
    negative_parts = [value for value in (exit_score, risk_score) if value is not None]
    base = sum(positive_parts) / len(positive_parts) if positive_parts else None
    penalty = sum(negative_parts) / len(negative_parts) if negative_parts else 0.0
    score = _brain_clamp(base * 0.80 + (1 - penalty) * 0.20) if base is not None else None
    details = [
        {"key": "entry", "label": "進場分數", "score": entry_score, "ok": entry_score is not None and entry_score >= 0.67, "value": f"{sum(1 for item in entry_checks if item)} / {sum(1 for item in entry_checks if item is not None)}"},
        {"key": "hold", "label": "持有分數", "score": hold_score, "ok": hold_score is not None and hold_score >= 0.60, "value": f"{sum(1 for item in hold_checks if item)} / {sum(1 for item in hold_checks if item is not None)}"},
        {"key": "exit", "label": "離場分數", "score": exit_score, "ok": exit_score is not None and exit_score < 0.34, "value": f"{sum(1 for item in exit_checks if item)} / {sum(1 for item in exit_checks if item is not None)}"},
        {"key": "risk", "label": "強制離場風險", "score": risk_score, "ok": risk_score is not None and risk_score < 0.25, "value": f"{sum(1 for item in force_exit_checks if item)} / {sum(1 for item in force_exit_checks if item is not None)}"},
        {"key": "reversal10", "label": "股票反轉10指標", "score": reversal_score, "ok": reversal_score is not None and reversal_score >= 0.60, "value": f"{sum(1 for item in reversal_checks if item)} / {sum(1 for item in reversal_checks if item is not None)}"},
    ]
    return {
        "ok": score is not None,
        "score": score,
        "text": _brain_indicator_score_text(score),
        "entryScore": entry_score,
        "holdScore": hold_score,
        "exitScore": exit_score,
        "riskScore": risk_score,
        "reversalScore": reversal_score,
        "details": details,
        "signals": {
            "maBull": ma_bull,
            "macdGolden": macd_golden,
            "macdDeath": macd_death,
            "kdGolden": kd_golden,
            "kdDeath": kd_death,
            "rsiAbove50": rsi is not None and rsi > 50,
            "rsiBelow50": rsi_exit,
            "closeAboveMa20": close_above_ma20,
            "breakMa20": break_ma20,
            "volumeExpanded": volume_expanded,
            "platformBreakout": platform_breakout,
            "breakoutHigh": breakout_high,
            "institutional3Buy": institutional_3_buy,
            "institutionalSell": institutional_sell,
            "singleDrop8Pct": single_drop,
            "largeDrop5d8Pct": large_drop,
            "trendlineBroken": trendline_broken,
            "higherLow": higher_low,
        },
        "missing": [],
    }


def _brain_strategy_horizon(rows, decision_flow=None, technical=None, short_money=None):
    """判斷這檔目前比較適合短炒、中期波段或長期趨勢。

    這是策略標籤與紙上驗證分組，不直接改變 entryAllowed/canNotify。
    """
    if not rows or len(rows) < 65:
        return {
            "ok": False,
            "key": "unknown",
            "label": "週期不足",
            "holdingDays": "",
            "primaryHorizon": "",
            "score": None,
            "reason": "至少需要65日日線價量",
            "profiles": [],
        }
    decision_flow = decision_flow or {}
    technical = technical or {}
    closes = [_brain_float(row.get("close")) for row in rows]
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else latest
    close = _brain_float(latest.get("close"))
    prev_close = _brain_float(previous.get("close"))
    ma20 = _brain_sma(closes, 20)
    ma60 = _brain_sma(closes, 60)
    prev_ma20 = _brain_sma(closes[:-1], 20)
    prev_ma60 = _brain_sma(closes[:-1], 60)
    volume_ratio = _brain_float(technical.get("volumeRatio"))
    if volume_ratio is None:
        _, volume_ratio, _ = _brain_score_volume(rows, close, prev_close)
    signals = decision_flow.get("signals") or {}
    entry_score = _brain_float(decision_flow.get("entryScore"))
    hold_score = _brain_float(decision_flow.get("holdScore"))
    exit_score = _brain_float(decision_flow.get("exitScore"))
    risk_score = _brain_float(decision_flow.get("riskScore"))
    reversal_score = _brain_float(decision_flow.get("reversalScore"))
    risk_ok_score = None if risk_score is None else _brain_clamp(1 - risk_score)
    exit_ok_score = None if exit_score is None else _brain_clamp(1 - exit_score)
    close_above_ma20 = close is not None and ma20 is not None and close >= ma20
    close_above_ma60 = close is not None and ma60 is not None and close >= ma60
    ma20_up = ma20 is not None and prev_ma20 is not None and ma20 >= prev_ma20
    ma60_up = ma60 is not None and prev_ma60 is not None and ma60 >= prev_ma60
    ret20 = (close - closes[-21]) / closes[-21] if close is not None and len(closes) >= 21 and closes[-21] else None
    ret60 = (close - closes[-61]) / closes[-61] if close is not None and len(closes) >= 61 and closes[-61] else None

    def bool_score(value):
        return 1.0 if value else 0.0

    def ratio_bucket(value, weak=0.0, good=0.08):
        if value is None:
            return None
        if value >= good:
            return 1.0
        if value >= weak:
            return 0.70
        return 0.25

    def volume_bucket(value):
        if value is None:
            return None
        if value >= 1.5:
            return 1.0
        if value >= 1.0:
            return 0.68
        if value >= 0.75:
            return 0.45
        return 0.20

    def weighted_score(parts):
        usable = [(score, weight) for score, weight in parts if score is not None]
        weight_sum = sum(weight for _, weight in usable)
        return sum(score * weight for score, weight in usable) / weight_sum if weight_sum else None

    breakout = bool(signals.get("breakoutHigh") or signals.get("platformBreakout"))
    volume_expanded = bool(signals.get("volumeExpanded"))
    short_score = weighted_score([
        (entry_score, 0.28),
        (reversal_score, 0.18),
        (volume_bucket(volume_ratio), 0.16),
        (bool_score(breakout), 0.14),
        (risk_ok_score, 0.12),
        (exit_ok_score, 0.12),
    ])
    mid_score = weighted_score([
        (hold_score, 0.25),
        (bool_score(close_above_ma20), 0.17),
        (bool_score(ma20_up), 0.17),
        (entry_score, 0.14),
        (ratio_bucket(ret20, weak=-0.02, good=0.06), 0.12),
        (risk_ok_score, 0.08),
        (exit_ok_score, 0.07),
    ])
    long_score = weighted_score([
        (bool_score(close_above_ma60), 0.22),
        (bool_score(ma60_up), 0.20),
        (ratio_bucket(ret60, weak=0.0, good=0.10), 0.18),
        (hold_score, 0.17),
        (risk_ok_score, 0.13),
        (exit_ok_score, 0.10),
    ])
    profiles = [
        {
            "key": "short_trade",
            "label": "短期",
            "holdingDays": "10日內",
            "primaryHorizon": "10d",
            "score": short_score,
            "reason": "看量能、突破、反轉與10日短線風險",
        },
        {
            "key": "mid_swing",
            "label": "中期",
            "holdingDays": "20-60日",
            "primaryHorizon": "20d",
            "score": mid_score,
            "reason": "看20MA、波段續航、20日報酬與風險",
        },
        {
            "key": "long_trend",
            "label": "長期",
            "holdingDays": "60日以上",
            "primaryHorizon": "60d",
            "score": long_score,
            "reason": "看60MA、長線趨勢、60日報酬與續抱風險",
        },
    ]
    ranked = [item for item in profiles if item.get("score") is not None]
    if not ranked:
        return {
            "ok": False,
            "key": "unknown",
            "label": "週期不足",
            "holdingDays": "",
            "primaryHorizon": "",
            "score": None,
            "reason": "週期分數無法計算",
            "profiles": profiles,
        }
    by_key = {item["key"]: item for item in ranked}
    ret60_long_enough = ret60 is not None and ret60 >= 0.06 and (ret20 is None or ret20 <= ret60 * 0.75)
    if breakout and volume_expanded and (short_score or 0.0) >= 0.72:
        selected = by_key.get("short_trade") or max(ranked, key=lambda item: item.get("score") or 0.0)
    elif ret60_long_enough and close_above_ma60 and ma60_up and (long_score or 0.0) >= 0.72:
        selected = by_key.get("long_trend") or max(ranked, key=lambda item: item.get("score") or 0.0)
    elif close_above_ma20 and ma20_up and (mid_score or 0.0) >= 0.62:
        selected = by_key.get("mid_swing") or max(ranked, key=lambda item: item.get("score") or 0.0)
    else:
        selected = max(ranked, key=lambda item: item.get("score") or 0.0)
    return {
        "ok": True,
        **selected,
        "text": _brain_pct(selected.get("score")),
        "profiles": profiles,
    }


def _brain_score_bias(close, ma20):
    close = _brain_float(close)
    ma20 = _brain_float(ma20)
    if close is None or ma20 is None or ma20 <= 0:
        return None, None, "無資料", ["20MA"]
    bias = (close - ma20) / ma20
    # 妖股短線基準：飆股啟動時正乖離常態性放大，健康區間放寬到 +12%，
    # +12%~+20% 視為偏熱觀察，超過 +20% 才算乖離過高。
    if -0.04 <= bias <= 0.12:
        score = 0.82
        ok = True
        text = f"乖離健康 {_brain_ratio_text(bias)}"
    elif -0.08 <= bias < -0.04:
        score = 0.68
        ok = None
        text = f"回測乖離 {_brain_ratio_text(bias)}"
    elif 0.12 < bias <= 0.20:
        score = 0.62
        ok = None
        text = f"偏熱乖離 {_brain_ratio_text(bias)}"
    elif bias > 0.20:
        score = 0.30
        ok = False
        text = f"乖離過高 {_brain_ratio_text(bias)}"
    else:
        score = 0.36
        ok = False
        text = f"跌離均線 {_brain_ratio_text(bias)}"
    return score, ok, text, []


def _brain_score_max_min_period_bias(close, highs, lows):
    close = _brain_float(close)
    highs = [_brain_float(value) for value in highs or []]
    lows = [_brain_float(value) for value in lows or []]
    highs = [value for value in highs if value is not None and value > 0]
    lows = [value for value in lows if value is not None and value > 0]
    if close is None or close <= 0 or len(highs) < 10 or len(lows) < 10:
        return None, None, "無資料", ["20日高低價"]
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    if high20 <= low20:
        return None, None, "高低價異常", ["20日高低價"]
    high_gap = (high20 - close) / close
    low_gap = (close - low20) / close
    # 收盤在 20 日區間中的相對位置（0=貼低點、1=貼高點）。窄幅收斂平台
    # 的股票同時「離高點近也離低點近」，必須用相對位置判斷，不能只看
    # 絕對百分比，否則貼著高點的收斂型態會被誤判成「靠近低點」。
    position = (close - low20) / (high20 - low20)
    # 妖股短線基準：接近/突破 20 日高點是飆股啟動的核心訊號，給最高分；
    # 「區間中段最安全」是長線思維，短線妖股反而要離高點近。
    if close >= high20 * 0.995:
        return 0.90, True, f"接近/突破20日高點 / 離低點 {_brain_ratio_text(low_gap)}", []
    if high_gap <= 0.06 and position >= 0.6:
        return 0.75, True, f"靠近高點 {_brain_ratio_text(high_gap)}", []
    if low_gap <= 0.04 and position <= 0.4:
        return 0.45, None, f"靠近低點 {_brain_ratio_text(low_gap)}", []
    return 0.52, None, f"區間中段 / 離高點 {_brain_ratio_text(high_gap)}", []


def _brain_score_margin_short_ratio(rows):
    verified = [
        row for row in (rows or [])[-6:]
        if is_official_source(row.get("margin_source"))
        and (row.get("margin_balance") is not None or row.get("short_balance") is not None)
    ]
    if not verified:
        return None, None, "無正式融資券資料", ["融資餘額", "融券餘額"]
    latest = verified[-1]
    previous = verified[-2] if len(verified) >= 2 else {}
    margin = _brain_float(latest.get("margin_balance"))
    short = _brain_float(latest.get("short_balance"))
    prev_margin = _brain_float(previous.get("margin_balance"))
    if margin is None and short is None:
        return None, None, "無資料", ["融資餘額", "融券餘額"]
    margin_change = None if margin is None or prev_margin is None else margin - prev_margin
    if margin is not None and margin_change is not None and margin_change < 0:
        return 0.72, True, f"融資下降 {_brain_signed_lots_text(margin_change)}", []
    if short is not None and margin is not None and short > margin * 0.25:
        return 0.66, None, f"融券偏高 / 融資 {_brain_number_text(margin, 0)}", []
    if margin_change is not None and margin_change > 0:
        return 0.42, False, f"融資增加 {_brain_signed_lots_text(margin_change)}", []
    return 0.55, None, "融資券中性", []


def _brain_backtest_strategy_score(rows, prediction=None, technical=None, short_money=None, kline=None):
    if not rows or len(rows) < 35:
        return {
            "ok": False,
            "score": None,
            "text": "資料不足",
            "passed": 0,
            "available": 0,
            "missing": ["至少35日日線價量"],
            "details": [],
        }
    prediction = prediction or {}
    technical = technical or {}
    short_money = short_money or {}
    kline = kline or {}
    latest = rows[-1]
    close = _brain_float(latest.get("close"))
    closes = [_brain_float(row.get("close")) for row in rows]
    highs = [_brain_float(row.get("high")) for row in rows]
    lows = [_brain_float(row.get("low")) for row in rows]
    ma5 = _brain_sma(closes, 5)
    ma10 = _brain_sma(closes, 10)
    ma20 = _brain_sma(closes, 20)
    prev_ma5 = _brain_sma(closes[:-1], 5)
    prev_ma20 = _brain_sma(closes[:-1], 20)
    kd = technical.get("kd") or _brain_kd(rows)
    macd = technical.get("macd") or _brain_macd(closes)
    short_details = {
        detail.get("key"): detail
        for detail in (short_money.get("details") or [])
    }
    decision_flow = _brain_decision_flow_score(rows, technical=technical, short_money=short_money)

    details = []
    details.append(_brain_strategy_detail(
        "decisionFlow",
        "交易決策流程/反轉10指標",
        None if decision_flow.get("score") is None else decision_flow.get("score") >= 0.55,
        decision_flow.get("score"),
        (
            f"進場 {_brain_pct(decision_flow.get('entryScore'))} / "
            f"持有 {_brain_pct(decision_flow.get('holdScore'))} / "
            f"離場 {_brain_pct(decision_flow.get('exitScore'))} / "
            f"風險 {_brain_pct(decision_flow.get('riskScore'))} / "
            f"反轉10 {_brain_pct(decision_flow.get('reversalScore'))}"
        ),
        decision_flow.get("missing") or [],
    ))
    score, ok, value, missing = _brain_score_bias(close, ma20)
    details.append(_brain_strategy_detail("bias", "Bias 乖離率", ok, score, value, missing))

    probability = _brain_float(prediction.get("probability"))
    threshold = _brain_float(prediction.get("threshold"), 0.45)
    kline_score = _brain_float(kline.get("score"))
    # 妖股短線基準：續航看「今天短線趨勢還在不在」（收盤 > 5MA > 10MA），
    # 模型機率只做小幅修正，不用明日預測機率決定今天是否續抱。
    if close is not None and ma5 is not None and ma10 is not None:
        if close > ma5 and ma5 > ma10:
            holding_base = 0.85
            holding_text = "短線趨勢完整（收盤>5MA>10MA）"
        elif close > ma5:
            holding_base = 0.60
            holding_text = "股價守住5MA，均線尚未加速"
        else:
            holding_base = 0.32
            holding_text = "跌破5MA，短線續航轉弱"
        # 2026-07-07 純規則:續航只看短線趨勢(收盤>5MA>10MA),拿掉模型機率修正。
        holding_ok = holding_base >= 0.55
        details.append(_brain_strategy_detail("continueHolding", "ContinueHolding 續航", holding_ok, holding_base, holding_text, []))
    elif kline_score is None:
        details.append(_brain_strategy_detail("continueHolding", "ContinueHolding 續航", None, None, "無K線分數", ["K線"]))
    else:
        holding_score = _brain_clamp(kline_score)
        holding_ok = holding_score >= 0.55
        details.append(_brain_strategy_detail("continueHolding", "ContinueHolding 續航", holding_ok, holding_score, f"續航 {_brain_pct(holding_score)}", []))

    institutional = short_details.get("institutionalFlow") or {}
    details.append(_brain_strategy_detail(
        "institutionalInvestorsFollower",
        "InstitutionalInvestorsFollower 法人跟隨",
        institutional.get("ok"),
        institutional.get("score"),
        institutional.get("value") or "無資料",
        institutional.get("missing") or [],
    ))

    k = _brain_float(kd.get("k"))
    d = _brain_float(kd.get("d"))
    prev_k = _brain_float(kd.get("prevK"))
    prev_d = _brain_float(kd.get("prevD"))
    kd_score = _brain_score_kd(k, d, prev_k, prev_d)
    details.append(_brain_strategy_detail(
        "kd",
        "KD 數值",
        None if kd_score is None else kd_score >= 0.55,
        kd_score,
        f"K {_brain_number_text(k)} / D {_brain_number_text(d)}",
        [] if kd_score is not None else ["KD(9,3,3)"],
    ))
    golden_cross = prev_k is not None and prev_d is not None and k is not None and d is not None and prev_k <= prev_d and k > d
    death_cross = prev_k is not None and prev_d is not None and k is not None and d is not None and prev_k >= prev_d and k < d
    if k is None or d is None or prev_k is None or prev_d is None:
        cross_score, cross_ok, cross_text, cross_missing = None, None, "無資料", ["KD交叉"]
    elif golden_cross:
        cross_score, cross_ok, cross_text, cross_missing = 0.86, True, "KD黃金交叉", []
    elif death_cross:
        cross_score, cross_ok, cross_text, cross_missing = 0.28, False, "KD死亡交叉", []
    elif k > d:
        cross_score, cross_ok, cross_text, cross_missing = 0.66, True, "KD多方排列", []
    else:
        cross_score, cross_ok, cross_text, cross_missing = 0.46, None, "KD尚未交叉", []
    details.append(_brain_strategy_detail("kdCrossOver", "KD CrossOver", cross_ok, cross_score, cross_text, cross_missing))

    macd_score = _brain_score_macd(macd)
    details.append(_brain_strategy_detail(
        "macdCrossOver",
        "MACD CrossOver",
        None if macd_score is None else macd_score >= 0.55,
        macd_score,
        f"柱 {_brain_number_text(macd.get('hist'), 4)} / 前柱 {_brain_number_text(macd.get('prevHist'), 4)}",
        [] if macd_score is not None else ["MACD"],
    ))

    if ma5 is None or ma20 is None or prev_ma5 is None or prev_ma20 is None:
        ma_score, ma_ok, ma_text, ma_missing = None, None, "無資料", ["5MA/20MA"]
    elif prev_ma5 <= prev_ma20 and ma5 > ma20:
        ma_score, ma_ok, ma_text, ma_missing = 0.88, True, "5MA上穿20MA", []
    elif ma10 is not None and ma5 > ma10 > ma20 and close is not None and close >= ma5:
        # 妖股短線加速排列：5>10>20 且股價在 5MA 上，比單純多方排列更強
        ma_score, ma_ok, ma_text, ma_missing = 0.85, True, "短均加速排列（5>10>20）", []
    elif ma5 > ma20 and close is not None and close >= ma5:
        ma_score, ma_ok, ma_text, ma_missing = 0.78, True, "5MA站上20MA，股價在5MA上", []
    elif ma5 > ma20:
        ma_score, ma_ok, ma_text, ma_missing = 0.64, None, "均線多方但股價未站穩5MA", []
    elif prev_ma5 >= prev_ma20 and ma5 < ma20:
        ma_score, ma_ok, ma_text, ma_missing = 0.25, False, "5MA下穿20MA", []
    else:
        ma_score, ma_ok, ma_text, ma_missing = 0.42, False, "短均線未轉強", []
    details.append(_brain_strategy_detail("maCrossOver", "MA CrossOver", ma_ok, ma_score, ma_text, ma_missing))

    score, ok, value, missing = _brain_score_max_min_period_bias(close, highs, lows)
    details.append(_brain_strategy_detail("maxMinPeriodBias", "MaxMinPeriodBias 區間偏離", ok, score, value, missing))

    # 方向要跟 _brain_score_kd 一致：這套系統是妖股短線動能基準，K 值高檔
    # 是強勢延續的常態，不是教科書式「超買=弱勢」。原本這裡用課本方向
    # (K高=弱、K低=強)，跟 kd 這個分量互相抵銷，稀釋掉真正的訊號。
    naive_kd_score = None if k is None else (0.42 if k < 20 else 0.72 if k <= 75 else 0.78 if k <= 85 else 0.55)
    naive_kd_ok = None if naive_kd_score is None else naive_kd_score >= 0.55
    details.append(_brain_strategy_detail(
        "naiveKd",
        "NaiveKD",
        naive_kd_ok,
        naive_kd_score,
        "無資料" if k is None else f"K值 {_brain_number_text(k)}",
        [] if k is not None else ["KD K值"],
    ))

    score, ok, value, missing = _brain_score_margin_short_ratio(rows)
    details.append(_brain_strategy_detail("shortSaleMarginPurchaseRatio", "融資融券比", ok, score, value, missing))

    weights = {
        "decisionFlow": 0.12,
        "bias": 0.10,
        "continueHolding": 0.10,
        "institutionalInvestorsFollower": 0.12,
        "kd": 0.10,
        "kdCrossOver": 0.10,
        "macdCrossOver": 0.12,
        "maCrossOver": 0.14,
        "maxMinPeriodBias": 0.10,
        "naiveKd": 0.06,
        "shortSaleMarginPurchaseRatio": 0.06,
    }
    weighted = [
        (detail.get("score"), weights.get(detail.get("key"), 0))
        for detail in details
        if detail.get("score") is not None
    ]
    weight_sum = sum(weight for _, weight in weighted)
    final_score = sum(value * weight for value, weight in weighted) / weight_sum if weight_sum else None
    missing = []
    for detail in details:
        missing.extend(detail.get("missing") or [])
    passed = sum(1 for detail in details if detail.get("ok") is True)
    available = sum(1 for detail in details if detail.get("score") is not None)
    return {
        "ok": final_score is not None,
        "score": final_score,
        "text": _brain_indicator_score_text(final_score),
        "passed": passed,
        "available": available,
        "missing": sorted(set(missing)),
        "details": details,
        "decisionFlow": decision_flow,
    }


_BRAIN_BASE_WEIGHTS = {
    # 2026-07-07 型態量能純規則化:模型權重歸零,不進 v2_score/決策。
    # 模型仍獨立訓練+預測(供戰績量測與「僅供參考」顯示),只是不再影響買賣判斷。
    # v2_score 是加權平均(除以權重和),weight=0 等於把 formalModel 排除,其餘型態量能
    # 分量自動重新正規化。
    "formalModel": 0.0,
    "kline": 0.16,
    "volume": 0.13,
    "market": 0.11,
    "chipMoney": 0.11,
    "strategyBacktest": 0.09,
    "risk": 0.06,
    "dataConfidence": 0.05,
}


_BRAIN_STRATEGY_PROFILES = {
    "monster": {
        "name": "妖股短打",
        "aliases": {"monster", "intraday", "entry", "buy"},
        "entryThreshold": 0.60,  # 2026-07-07 純規則化校準:移除 formalModel(值偏低)後 v2_score 在買進邊界系統性升高~0.04,0.57→0.60 補償避免門檻變太鬆(對抗式審查 finding)
        "dataConfidenceThreshold": 0.62,
        "requiredComponents": {"kline", "volume", "market", "risk", "dataConfidence"},
        "weights": {
            "formalModel": 0.0,
            "kline": 0.17,
            "volume": 0.18,
            "market": 0.10,
            "chipMoney": 0.12,
            "strategyBacktest": 0.12,
            "risk": 0.06,
            "dataConfidence": 0.03,
        },
    },
    "portfolio_exit": {
        "name": "持股續抱風控",
        "aliases": {"portfolio_exit", "portfolio-exit", "exit", "sell-alert", "sell"},
        "entryThreshold": 0.45,
        "dataConfidenceThreshold": 0.62,
        # 已經買進的股票不需要重新符合「K線突破/量能剛啟動」這種進場型態條件，
        # 只留正式模型、大盤、風控、資料可信度這些跟「還能不能續抱」直接相關的必要條件。
        "requiredComponents": {"market", "risk", "dataConfidence"},
        "weights": {
            "formalModel": 0.0,
            "kline": 0.06,
            "volume": 0.06,
            "market": 0.20,
            "chipMoney": 0.14,
            "strategyBacktest": 0.20,
            "risk": 0.10,
            "dataConfidence": 0.04,
        },
    },
}


def _brain_strategy_profile(context_key):
    # 依 context 字串比對對應的策略腦袋：monster=妖股短打進場、
    # portfolio_exit=持股續抱風控；比對不到的字串一律退回 monster 這套門檻/權重判斷。
    normalized = str(context_key or "").strip().lower()
    for key, profile in _BRAIN_STRATEGY_PROFILES.items():
        if normalized == key or normalized in profile.get("aliases", set()):
            merged_weights = dict(_BRAIN_BASE_WEIGHTS)
            merged_weights.update(profile.get("weights") or {})
            return {
                **profile,
                "key": key,
                "weights": merged_weights,
                "requiredComponents": set(profile.get("requiredComponents") or set()),
            }
    profile = _BRAIN_STRATEGY_PROFILES["monster"]
    return {
        **profile,
        "key": "monster",
        "weights": dict(profile.get("weights") or {}),
        "requiredComponents": set(profile.get("requiredComponents") or set()),
    }


def _brain_v2_weights(context_key):
    return _brain_strategy_profile(context_key).get("weights") or dict(_BRAIN_BASE_WEIGHTS)


def _brain_v2_aggregate(
    v2_components,
    required_component_keys,
    probability,
    threshold,
    formal_component,
    kline_component,
    volume_component,
    market_component,
    risk_component,
    data_complete,
    model_ready,
    data_confidence_score,
    data_confidence_threshold,
    entry_score_threshold,
    entry_context,
):
    """Brain v2 的加權總分、必要條件檢查、進場/賣出判斷，抽成純函式方便單元測試。
    抽出來的邏輯必須跟原本 inline 在 build_brain_decision 裡的算法完全一致，
    不要在這裡跟呼叫端各改各的，否則兩邊會悄悄漂移。"""
    v2_weight_sum = sum(row["weight"] for row in v2_components if row.get("score") is not None)
    v2_score = (
        sum(_brain_float(row.get("score"), 0.0) * row["weight"] for row in v2_components if row.get("score") is not None) / v2_weight_sum
        if v2_weight_sum else None
    )
    v2_pass_count = sum(1 for row in v2_components if row.get("ok") is True)
    formal_pass = probability is not None and probability >= threshold
    # kline/volume 門檻只在該 profile 真的把它們列為必要條件時才檢查。
    # portfolio_exit 明確把 kline/volume 排除在 requiredComponents 外(已經
    # 買進的股票不需要重新符合「K線突破/量能剛啟動」這種進場型態條件)，
    # 但這裡如果照抄 monster 的固定門檻，會讓 technical_override 對
    # portfolio_exit 幾乎永遠算不出 True(持有中的股票K線/量能本來就不會像
    # 剛啟動的妖股那麼強)，等於變相把這條件排除的用意用另一個入口拉回來，
    # (formal_pass or technical_override) 整條路徑實質上只剩 formal_pass。
    # 2026-07-07 純規則:technical_override 拿掉「模型分≥0.40」,只看型態量能。
    technical_override = (
        ("kline" not in required_component_keys or (kline_component or 0) >= 0.64) and
        ("volume" not in required_component_keys or (volume_component or 0) >= 0.55) and
        (market_component or 0) >= 0.52 and
        (risk_component or 0) >= 0.55
    )
    # formalModel 本身也在 requiredComponents 清單裡(monster/portfolio_exit
    # 兩個 profile 都是)，它的 ok 定義就是 formal_pass 本身。如果不排除，
    # 下面 v2_entry_allowed 的 (formal_pass or technical_override) 這個 OR
    # 條件會被 required_component_failures 的 AND 條件單方面否決——只要
    # formal_pass 是 False，formalModel 就必然出現在 required_component_
    # failures 裡，technical_override 算出 True 也沒用，等於 technical_
    # override 在生產環境完全是死代碼(實測驗證過：K線/量能/大盤/風控全部
    # 強勢、technical_override=True，v2_entry_allowed 仍是 False)。只在
    # technical_override 真的成立時才把 formalModel 從必要條件失敗清單排除
    # ——formal_pass 也 technical_override 都不成立時，formalModel 仍要
    # 照常算失敗，敘事文字(_brain_v2_narrative_blocker)才不會漏講「正式
    # 模型分數不夠」這個真正的卡關原因。
    required_component_failures = [
        row for row in v2_components
        if row.get("key") in required_component_keys and row.get("ok") is not True and not row.get("auxiliary")
        and not (row.get("key") == "formalModel" and technical_override)
    ]
    # 2026-07-07 型態量能純規則化:進場只看型態量能——拿掉 model_ready 前置閘、
    # 拿掉 (formal_pass or technical_override) 的模型條件。v2_score(已不含模型,formalModel
    # 權重=0)過門檻 + 必要條件(kline/volume/market/risk/dataConfidence)全過 + 風控 +
    # 資料完整即可。模型獨立訓練+預測不變,只是不再進這個決策。
    v2_entry_allowed = (
        data_complete and
        data_confidence_score >= data_confidence_threshold and
        (risk_component or 0) >= 0.55 and
        v2_score is not None and v2_score >= entry_score_threshold and
        technical_override and
        not required_component_failures
    )
    # 2026-07-07 純規則化校準(live驗證 finding):原先只靠 v2_score>=門檻 太鬆(弱盤仍97%可買),
    # 把 technical_override(純規則:K線>=0.64+量能>=0.55+大盤>=0.52+風控>=0.55)加回當確認閘。
    # 它含大盤強度,弱盤自然壓低可買數,取代拆掉的 formal_pass 那條(現全純規則、無模型)。
    # 賣出/續抱判斷仍不綁模型,只要資料完整+可信度夠(保命網不綁模型)。
    v2_sell_data_ready = data_complete and data_confidence_score >= data_confidence_threshold
    entry_decision_blocked = entry_context and data_complete and not v2_entry_allowed
    soft_gate = _brain_v2_soft_gate(
        v2_components=v2_components,
        required_component_keys=required_component_keys,
        threshold=threshold,
        data_confidence_threshold=data_confidence_threshold,
        v2_score=v2_score,
        entry_score_threshold=entry_score_threshold,
        data_complete=data_complete,
        model_ready=model_ready,
        data_confidence_score=data_confidence_score,
        risk_component=risk_component,
    )
    return {
        "v2_weight_sum": v2_weight_sum,
        "v2_score": v2_score,
        "v2_pass_count": v2_pass_count,
        "required_component_failures": required_component_failures,
        "formal_pass": formal_pass,
        "technical_override": technical_override,
        "v2_entry_allowed": v2_entry_allowed,
        "v2_sell_data_ready": v2_sell_data_ready,
        "entry_decision_blocked": entry_decision_blocked,
        "soft_gate": soft_gate,
    }


def _brain_v2_soft_gate(
    v2_components,
    required_component_keys,
    threshold,
    data_confidence_threshold,
    v2_score,
    entry_score_threshold,
    data_complete,
    model_ready,
    data_confidence_score,
    risk_component,
    penalty_scale=0.6,
):
    """實驗性的「軟性扣分」評分方式：不對必要條件做硬性布林擋下，改成依照
    「離各自門檻多遠」按比例扣總分，讓「差一點」跟「差很多」的案例會有不同
    結果，而不是像 v2_entry_allowed 那樣只要有一項必要條件沒過就整個擋下。

    這是跟現行硬性 gate 並行計算、完全不影響 entryAllowed/decisionBlocked
    的實驗性數據，目的是累積進 brain_v2_snapshots，等幾個月後有足夠的歷史
    命中率資料，才能真的驗證哪種方式比較準，不是現在就要拿來取代硬性 gate。
    """
    def required_threshold(key):
        if key == "formalModel":
            return threshold
        if key == "dataConfidence":
            return data_confidence_threshold
        return 0.55

    penalty = 0.0
    penalty_details = []
    for row in v2_components:
        key = row.get("key")
        if key not in required_component_keys or row.get("auxiliary") or row.get("ok") is True:
            continue
        score = row.get("score")
        gap = max(0.0, required_threshold(key) - (score if score is not None else 0.0))
        component_penalty = gap * penalty_scale
        penalty += component_penalty
        penalty_details.append({"key": key, "gap": round(gap, 4), "penalty": round(component_penalty, 4)})

    soft_score = None if v2_score is None else max(0.0, v2_score - penalty)
    soft_entry_allowed = bool(
        data_complete and model_ready and
        data_confidence_score >= data_confidence_threshold and
        (risk_component or 0) >= 0.55 and
        soft_score is not None and soft_score >= entry_score_threshold
    )
    return {
        "penalty": round(penalty, 4),
        "penaltyDetails": penalty_details,
        "softScore": soft_score,
        "softEntryAllowed": soft_entry_allowed,
    }


def _brain_v2_narrative_blocker(
    v2_components,
    required_component_failures,
    soft_gate,
    technical_override,
    v2_score,
    entry_score_threshold,
    data_confidence_score,
    data_confidence_threshold,
    risk_component,
):
    """把「型態量能進場條件未通過：正式模型分數、風控分數」這種條列術語，
    改寫成講人話的敘事句子——說清楚卡在哪裡、還差多少、其餘條件是否已經過關，
    讓使用者一眼看出這是「差一點就過」還是「還差得遠」，不用自己去猜代號意思。
    """
    passed_labels = [row["label"] for row in v2_components if row.get("ok") is True and not row.get("auxiliary")]

    if not required_component_failures:
        reasons = []
        if v2_score is not None and v2_score < entry_score_threshold:
            reasons.append(f"整體分數 {_brain_pct(v2_score)} 還沒到門檻 {_brain_pct(entry_score_threshold)}")
        if data_confidence_score is not None and data_confidence_score < data_confidence_threshold:
            reasons.append(f"資料可信度 {_brain_pct(data_confidence_score)} 不足門檻 {_brain_pct(data_confidence_threshold)}")
        if (risk_component or 0) < 0.55:
            reasons.append(f"風險控管分數 {_brain_pct(risk_component)} 偏低")
        if not reasons:
            return None
        return "、".join(reasons) + "，尚未達到進場標準，建議再觀察。"

    gap_by_key = {row["key"]: row["gap"] for row in (soft_gate.get("penaltyDetails") or [])}
    reasons = []
    for row in required_component_failures:
        gap = gap_by_key.get(row.get("key"))
        label = row.get("label") or row.get("key")
        value_text = row.get("value") or _brain_pct(row.get("score"))
        if gap is None:
            closeness = ""
        elif gap <= 0.03:
            closeness = "，只差一點點"
        elif gap <= 0.08:
            closeness = "，還差一小段"
        else:
            closeness = "，差距較大"
        reasons.append(f"{label}目前{value_text}{closeness}")

    sentence = "、".join(reasons) + "，尚未達到進場標準"
    # technical_override=True 代表技術面替代條件「已經通過」，這裡走到這個
    # 分支代表 formalModel 以外還有別的必要條件沒過(例如大盤/風控)，不能
    # 再說「技術面替代條件也差一點沒能補上」——那句話跟 technical_override
    # 為真的事實剛好相反，只有在它沒通過時才該這樣講。
    if not technical_override:
        sentence += "（技術面替代條件也差一點沒能補上）"
    if passed_labels:
        shown = "、".join(passed_labels[:4])
        sentence += f"；{shown}等其餘條件已經過關"
    sentence += "，建議再觀察，等分數補上再考慮進場。"
    return sentence


def _brain_is_borderline(score, threshold, margin=0.05):
    """判斷分數是不是卡在門檻邊緣（不管是剛好壓線通過還是剛好沒過）。
    這種案例代表資料來源數量只要稍微變動（例如某天某個 FinMind 來源
    剛好晚到），明天判斷就可能整個翻盤，值得提醒使用者這次的判斷不算穩固，
    不是像分數穩穩超過或低於門檻那樣可靠。"""
    if score is None or threshold is None:
        return False
    return abs(score - threshold) <= margin


def _brain_score_trend(v2_score, previous_snapshot):
    """比較「今天」跟上一個有記錄快照的交易日之間的 v2_score，讓使用者一眼
    看出分數是變好還是變差，不用自己回想上次看到的數字是多少。"""
    if v2_score is None or not previous_snapshot:
        return None
    previous_score = previous_snapshot.get("v2_score")
    if previous_score is None:
        return None
    delta = v2_score - previous_score
    if delta > 0.005:
        direction = "up"
        arrow = "↑"
    elif delta < -0.005:
        direction = "down"
        arrow = "↓"
    else:
        direction = "flat"
        arrow = "→"
    sign = "+" if delta >= 0 else ""
    previous_date = previous_snapshot.get("price_date")
    return {
        "previousDate": previous_date,
        "previousScore": previous_score,
        "delta": round(delta, 4),
        "direction": direction,
        "text": f"{arrow} 較 {previous_date} {sign}{delta * 100:.1f}%",
    }


def build_brain_decision(symbol, context="monster", intraday_setup=None, use_model=False):
    # 正式股票分析預設只用真實資料與確定性規則；use_model=True 只供獨立模型
    # 實驗/回歸，不得由雷達、持股或提醒 API 啟用。
    # 請求內 prices memo:一次判斷會讀同一檔 prices 兩次(這裡開頭算 K線/量能 +
    # predict_symbol 結果快取 miss 時 ensure_model_ready_rows 又讀一次)。predict
    # 快取 TTL 只有 120 秒、開畫面幾乎必然 miss,所以第二次 DB 讀幾乎每次都發生。
    # 包一層 memo 讓同一檔在這次判斷內只讀一次 DB(持股 19 檔批次判斷省下約一半的
    # prices 讀取),範圍離開即清空,零過期風險。
    with backend.price_rows_memo():
        return _build_brain_decision(symbol, context=context, intraday_setup=intraday_setup, use_model=use_model)


def _build_brain_decision(symbol, context="monster", intraday_setup=None, use_model=False):
    symbol = "".join(char for char in str(symbol or "") if char.isdigit())[:4]
    if not symbol:
        return {"ok": False, "error": "缺少股票代號"}

    rows = backend.rows_with_verified_sources(backend.load_price_rows(symbol))
    latest = rows[-1] if rows else {}
    quality = (
        backend.model_data_quality(symbol, rows)
        if use_model else backend.rule_analysis_data_quality(symbol, rows)
    ) if rows else {
        "ok": False,
        "missing": ["priceRowsEnough"],
        "rows": 0,
        "recentRows": 0,
    }

    prediction = None
    prediction_error = ""
    try:
        if use_model:
            # 回歸比對用的舊路徑:跑完整模型推論。
            prediction = backend.predict_symbol(symbol, save=True, repair=False)
        else:
            # 正式分析只計算確定性的風險、大盤與價量輸入，不載 model.pkl、
            # 不跑 ensemble，也不寫 predictions；獨立模型由 15:10 排程處理。
            prediction = backend.brain_decision_inputs(rows)
    except Exception as exc:
        prediction_error = str(exc)

    health = _runtime_health_snapshot()
    model_health_ok = health.get("decisionsEnabled", health.get("ok", True)) is not False if use_model else True
    probability = _brain_float((prediction or {}).get("probability"))
    threshold = _brain_float((prediction or {}).get("threshold"), 0.45)
    trade_gate = (prediction or {}).get("tradeGate") or {}
    model_probabilities = (prediction or {}).get("modelProbabilities") or {}
    buy_signal = (prediction or {}).get("buySignal") or {}
    risk_penalty = _brain_float((prediction or {}).get("riskPenalty"))
    setup_score = _brain_float((prediction or {}).get("setupScore"))
    close = _brain_float((prediction or {}).get("close"), _brain_float(latest.get("close")))
    kline_score = _brain_kline_score(rows, prediction)
    if not use_model:
        (kline_score.get("components") or {}).pop("model", None)
    kline_components = kline_score.get("components") or {}
    technical_indicator_score = _brain_technical_indicator_score(rows)
    short_money_score = _brain_short_money_score(symbol, rows, latest)
    strategy_backtest_score = _brain_backtest_strategy_score(rows, prediction, technical_indicator_score, short_money_score, kline_score)
    strategy_horizon = _brain_strategy_horizon(
        rows,
        strategy_backtest_score.get("decisionFlow") or {},
        technical_indicator_score,
        short_money_score,
    )
    short_money_details = {
        detail.get("key"): detail
        for detail in (short_money_score.get("details") or [])
    }
    technical_kd = technical_indicator_score.get("kd") or {}
    technical_macd = technical_indicator_score.get("macd") or {}
    technical_obv = technical_indicator_score.get("obv") or {}
    technical_volume_ratio = technical_indicator_score.get("volumeRatio")
    technical_volume_text = "無資料" if technical_volume_ratio is None else f"{technical_volume_ratio:.2f}倍"
    technical_obv_text = technical_obv.get("text") or "無資料"

    context_key = str(context or "").strip().lower()
    strategy_profile = _brain_strategy_profile(context_key)
    strategy_key = strategy_profile.get("key") or "monster"
    strategy_name = strategy_profile.get("name") or "妖股短打"
    entry_score_threshold = _brain_float(strategy_profile.get("entryThreshold"), 0.55)
    data_confidence_threshold = _brain_float(strategy_profile.get("dataConfidenceThreshold"), 0.65)
    required_component_keys = set(strategy_profile.get("requiredComponents") or set())
    entry_context = strategy_key == "monster"
    # 這個集合曾經跟下面的 core_fields 一模一樣，導致 missing_core 永遠是
    # 空陣列(核心資料缺口的重罰路徑永遠打不到，data_complete 只剩quality
    # 在把關)。外資/投信/融資/融券本來就有T+1公告延遲，這件事已經由下面
    # recent_official_core_rows(往前20列回溯) 正確處理了；auxiliary_
    # source_keys 涵蓋全部core_fields是多此一舉的雙重保險，效果卻是連
    # 「20天內真的完全抓不到任何一筆官方資料」這種資料源真的斷線的情況
    # 也被悄悄降級成輕罰，不會被判定為核心缺口。清空這個集合，讓真正的
    # 長期缺口能被 missing_core 抓到，同時保留 recent_official_core_rows
    # 對正常T+1延遲的容忍。
    auxiliary_source_keys = set()
    core_fields = [
        ("foreign_buy_sell", "外資買賣超", "chip_source"),
        ("trust_buy_sell", "投信買賣超", "chip_source"),
        ("margin_balance", "融資餘額", "margin_source"),
        ("short_balance", "融券餘額", "margin_source"),
    ]
    missing_core = []
    soft_missing_core = []
    recent_official_core_rows = {}
    for key, label, source_key in core_fields:
        for row in reversed(rows[-20:]):
            value = row.get(key)
            source = row.get(source_key)
            if value is not None and value != "" and is_official_source(source):
                recent_official_core_rows[key] = row
                break

    source_rows = [
        {"label": "日線價量", "source": latest.get("price_source"), "status": _brain_source_status(latest.get("price_source"))},
        {"label": "外資/投信", "source": latest.get("chip_source"), "status": _brain_source_status(latest.get("chip_source"))},
        {"label": "融資融券", "source": latest.get("margin_source"), "status": _brain_source_status(latest.get("margin_source"))},
    ]
    for key, label, source_key in core_fields:
        value = latest.get(key)
        source = latest.get(source_key)
        if value is None or value == "" or not is_official_source(source):
            if key in recent_official_core_rows:
                continue
            if key in auxiliary_source_keys:
                soft_missing_core.append(label)
                continue
            missing_core.append(label)

    quality_missing = list(quality.get("missing") or [])
    input_rules_ready = bool(prediction) and not prediction_error
    data_complete = bool(rows) and bool(quality.get("ok")) and not missing_core and input_rules_ready
    model_ready = bool(prediction) and probability is not None and not prediction_error and model_health_ok if use_model else True

    source_score_rows = source_rows or []
    official_source_count = sum(1 for row in source_score_rows if row.get("status") == "正式/授權來源")
    source_score = official_source_count / len(source_score_rows) if source_score_rows else 0.0
    quality_score = 1.0 if quality.get("ok") else (0.35 if rows else 0.0)
    missing_penalty = min(len(missing_core) * 0.18 + len(soft_missing_core) * 0.04, 0.50)
    # 2026-07-07 純規則化(對抗式審查 HIGH finding):資料可信度拿掉 model_ready_score*0.20,
    # 原 0.20 均分給 quality/source(資料面),讓可信度純由資料品質+來源正式性決定;模型故障
    # 不再壓低可信度而間接擋買賣。總權重維持 0.90+0.10 missing,門檻 0.62 尺度不變。
    data_confidence_score = _brain_clamp(
        quality_score * 0.45 +
        source_score * 0.45 +
        (0.10 if not missing_core else 0.0) -
        missing_penalty
    )

    def _brain_best_score(*values):
        numbers = [_brain_float(value) for value in values]
        numbers = [value for value in numbers if value is not None]
        return max(numbers) if numbers else None

    formal_component = _brain_clamp(probability, 0.0, 1.0) if probability is not None else None
    kline_component = _brain_float(kline_score.get("score"))
    technical_volume_component = _brain_float((technical_indicator_score.get("components") or {}).get("volume"))
    technical_obv_component = _brain_float((technical_indicator_score.get("components") or {}).get("obv"))
    kline_volume_component = _brain_float(kline_components.get("volume"))
    raw_volume_component = _brain_best_score(kline_volume_component, technical_volume_component)
    if raw_volume_component is not None and technical_obv_component is not None:
        volume_component = _brain_clamp(raw_volume_component * 0.65 + technical_obv_component * 0.35)
    else:
        volume_component = _brain_best_score(raw_volume_component, technical_obv_component)
    volume_component_text = f"{technical_volume_text} / OBV {technical_obv_text}"
    # kline/volume 分量原本只憑「昨天收盤那根日K」計算，對盤中才發生、且已經被
    # server.py 完整驗證通過(時間窗、跳空、開高走低、停損都檢查過)的突破/回測/
    # V轉型態完全無感——可能導致一支盤中教科書等級的V轉，因為昨天日K偏弱就被
    # entryAllowed=false 擋下。有已驗證的盤中型態時，把這兩個分量墊高到剛好
    # 通過 technical_override 門檻，讓已確認的盤中證據至少不會被過時的日線分數蓋掉。
    intraday_confirmed_setup = bool((intraday_setup or {}).get("canBuy")) and bool((intraday_setup or {}).get("setupType"))
    if intraday_confirmed_setup:
        kline_component = max(kline_component or 0.0, 0.64)
        volume_component = max(volume_component or 0.0, 0.55)
        volume_component_text += f"｜盤中{intraday_setup.get('setupType')}型態已確認"
    market_component = _brain_float(kline_components.get("market"))
    if market_component is None:
        if trade_gate.get("marketOk") and trade_gate.get("strongerThanMarket"):
            market_component = 0.78
        elif trade_gate.get("marketOk") or trade_gate.get("strongerThanMarket"):
            market_component = 0.62
        else:
            market_component = 0.42
    risk_from_penalty = None if risk_penalty is None else _brain_clamp(1.0 - risk_penalty, 0.0, 1.0)
    risk_component = risk_from_penalty if risk_from_penalty is not None else (0.72 if trade_gate.get("riskOk") else 0.42)
    if trade_gate.get("riskOk") is False:
        risk_component = min(risk_component, 0.45)

    chip_money_component = _brain_float(short_money_score.get("score"))
    chip_money_value_text = (
        f"{short_money_score.get('text')} / "
        f"可用 {short_money_score.get('available', 0)}/5 / "
        f"通過 {short_money_score.get('passed', 0)}/5"
    )
    strategy_component = _brain_float(strategy_backtest_score.get("score"))
    strategy_available = int(strategy_backtest_score.get("available") or 0)
    strategy_value_text = (
        f"{strategy_backtest_score.get('text')} / "
        f"可用 {strategy_available} / "
        f"通過 {strategy_backtest_score.get('passed', 0)}/{strategy_available or 0}"
    )
    v2_weights = strategy_profile.get("weights") or _brain_v2_weights(context_key)
    response_weights = v2_weights if use_model else {
        key: value for key, value in v2_weights.items() if key != "formalModel"
    }
    short_money_passed = int(short_money_score.get("passed") or 0)
    short_money_available = int(short_money_score.get("available") or 0)
    v2_components = [
        {"key": "kline", "label": "K線型態", "score": kline_component, "weight": v2_weights["kline"], "ok": kline_component is not None and kline_component >= 0.55, "value": kline_score.get("text") or "無資料"},
        {"key": "volume", "label": "量能+OBV", "score": volume_component, "weight": v2_weights["volume"], "ok": volume_component is not None and volume_component >= 0.55, "value": volume_component_text},
        {"key": "market", "label": "大盤強弱", "score": market_component, "weight": v2_weights["market"], "ok": market_component is not None and market_component >= 0.55, "value": _brain_pct(market_component)},
        {"key": "chipMoney", "label": "籌碼/資金", "score": chip_money_component, "weight": v2_weights["chipMoney"], "ok": chip_money_component is not None and chip_money_component >= 0.55 and short_money_available >= 1 and short_money_passed >= 1, "value": chip_money_value_text},
        {"key": "strategyBacktest", "label": "回測策略", "score": strategy_component, "weight": v2_weights["strategyBacktest"], "ok": strategy_component is not None and strategy_component >= 0.55, "value": strategy_value_text},
        {"key": "risk", "label": "風險控管", "score": risk_component, "weight": v2_weights["risk"], "ok": risk_component is not None and risk_component >= 0.55, "value": "risk penalty " + ("無資料" if risk_penalty is None else f"{risk_penalty:.3f}")},
        {
            "key": "dataConfidence", "label": "資料可信度", "score": data_confidence_score, "weight": v2_weights["dataConfidence"],
            "ok": data_confidence_score >= data_confidence_threshold,
            "value": f"正式來源 {official_source_count}/{len(source_score_rows) if source_score_rows else 0}",
            "borderline": _brain_is_borderline(data_confidence_score, data_confidence_threshold),
        },
    ]
    if use_model:
        v2_components.insert(0, {
            "key": "formalModel",
            "label": "獨立模型分數（不進正式決策）",
            "score": formal_component,
            "weight": v2_weights["formalModel"],
            "ok": None,
            "auxiliary": True,
            "value": _brain_pct(formal_component),
        })
    v2_aggregate = _brain_v2_aggregate(
        v2_components=v2_components,
        required_component_keys=required_component_keys,
        probability=probability,
        threshold=threshold,
        formal_component=formal_component,
        kline_component=kline_component,
        volume_component=volume_component,
        market_component=market_component,
        risk_component=risk_component,
        data_complete=data_complete,
        model_ready=model_ready,
        data_confidence_score=data_confidence_score,
        data_confidence_threshold=data_confidence_threshold,
        entry_score_threshold=entry_score_threshold,
        entry_context=entry_context,
    )
    v2_weight_sum = v2_aggregate["v2_weight_sum"]
    v2_score = v2_aggregate["v2_score"]
    v2_pass_count = v2_aggregate["v2_pass_count"]
    required_component_failures = v2_aggregate["required_component_failures"]
    formal_pass = v2_aggregate["formal_pass"]
    technical_override = v2_aggregate["technical_override"]
    v2_entry_allowed = v2_aggregate["v2_entry_allowed"]
    v2_sell_data_ready = v2_aggregate["v2_sell_data_ready"]
    entry_decision_blocked = v2_aggregate["entry_decision_blocked"]
    v2_soft_gate = v2_aggregate["soft_gate"]
    previous_v2_snapshot = backend.get_previous_brain_v2_snapshot(symbol, latest.get("date"), context)
    v2_score_trend = _brain_score_trend(v2_score, previous_v2_snapshot)
    # 2026-07-07 純規則化(對抗式審查 HIGH finding):observe_only 拿掉 not model_ready——
    # 模型故障/逾時/預測失敗不再否決純規則的買/賣/續抱結論(型態量能算得出就給結論),
    # 保命網(賣出/停損)不再被模型狀態綁死。模型不可用只反映在「模型參考」顯示,不進
    # recommendation/can_notify/decisionBlocked。硬停損 LINE 走 server 守門員(本就零模型依賴)。
    observe_only = not data_complete

    if observe_only:
        recommendation = "只觀察"
        action_label = "資料不足，不給買賣結論"
    elif strategy_key == "portfolio_exit":
        if v2_entry_allowed:
            recommendation = "續抱觀察"
            action_label = f"{strategy_name}：型態量能續航條件通過，暫無賣出訊號"
        else:
            recommendation = "留意賣出訊號"
            action_label = f"{strategy_name}：型態量能續航條件轉弱，建議留意停利/停損"
    elif not v2_entry_allowed:
        recommendation = "只觀察"
        action_label = f"{strategy_name}：型態量能進場條件未通過"
    else:
        recommendation = "可列入買進觀察"
        action_label = f"{strategy_name}：型態量能進場條件通過，仍需盤中確認"

    # 2026-07-09 拆模型:action 原本 echo 模型的 action(BUY_CANDIDATE / WAIT_MARKET_RISK
    # /WAIT),改由 Brain 自己的決策狀態推導——拆模型後(無模型 action)也給得出有意義的
    # 碼,不再退化成一律 OBSERVE_ONLY。這是純顯示/狀態色碼,不影響 recommendation/決策。
    if observe_only:
        decision_action = "OBSERVE_ONLY"
    elif v2_entry_allowed and entry_context:
        decision_action = "BUY_CANDIDATE"
    else:
        decision_action = "WAIT"

    blockers = []
    if not rows:
        blockers.append("沒有已驗證日線資料")
    if quality_missing:
        blockers.extend([f"日線資料品質未通過：{item}" for item in quality_missing])
    if missing_core:
        blockers.append(f"核心資料缺口：{'、'.join(missing_core)}")
    if prediction_error:
        blockers.append(
            f"獨立模型預測失敗：{prediction_error}"
            if use_model else f"規則輸入計算失敗：{prediction_error}"
        )
    if use_model and not model_health_ok:
        blockers.append("獨立模型健康檢查未通過")
    if entry_decision_blocked:
        narrative_blocker = _brain_v2_narrative_blocker(
            v2_components=v2_components,
            required_component_failures=required_component_failures,
            soft_gate=v2_soft_gate,
            technical_override=technical_override,
            v2_score=v2_score,
            entry_score_threshold=entry_score_threshold,
            data_confidence_score=data_confidence_score,
            data_confidence_threshold=data_confidence_threshold,
            risk_component=risk_component,
        )
        if narrative_blocker:
            blockers.append(narrative_blocker)
        else:
            failed_v2 = [row["label"] for row in v2_components if row.get("ok") is False and not row.get("auxiliary")]
            blockers.append("型態量能進場條件未通過：" + ("、".join(failed_v2) if failed_v2 else "本機核心分數未達標"))

    v2_condition_rows = [
        {
            "label": f"型態量能：{row['label']}",
            "ok": row.get("ok"),
            "value": f"{_brain_pct(row.get('score'))}｜權重 {int(row.get('weight', 0) * 100)}%｜{row.get('value') or '無資料'}",
        }
        for row in v2_components
    ]
    conditions = [
        {"label": "日線價量來源正式", "ok": is_official_source(latest.get("price_source")), "value": latest.get("price_source") or "無資料"},
        {
            "label": "核心資料完整",
            "ok": not missing_core,
            "value": "完整" if not missing_core and not soft_missing_core else (
                ("缺 " + "、".join(missing_core)) if missing_core else "風控核心完整；輔助缺 " + "、".join(soft_missing_core)
            ),
        },
        {"label": "日線資料品質通過", "ok": bool(quality.get("ok")), "value": "通過" if quality.get("ok") else "缺 " + "、".join(quality_missing)},
        {
            "label": "技術面指標分數",
            "ok": None if not technical_indicator_score.get("ok") else _brain_float(technical_indicator_score.get("score")) >= 0.55,
            "value": (
                f"{technical_indicator_score.get('text')}｜"
                f"5MA {_brain_number_text(technical_indicator_score.get('ma5'))} / "
                f"10MA {_brain_number_text(technical_indicator_score.get('ma10'))} / "
                f"量能 {technical_volume_text} / "
                f"OBV {technical_obv_text} / "
                f"K {_brain_number_text(technical_kd.get('k'))} / "
                f"D {_brain_number_text(technical_kd.get('d'))} / "
                f"MACD柱 {_brain_number_text(technical_macd.get('hist'), 4)}"
            ),
        },
        {
            "label": "短線資金分數",
            "ok": None if not short_money_score.get("ok") else _brain_float(short_money_score.get("score")) >= 0.55,
            "value": (
                f"{short_money_score.get('text')}｜"
                f"可用 {short_money_score.get('available', 0)}/5｜"
                f"通過 {short_money_score.get('passed', 0)}/5"
            ),
        },
        {
            "label": "回測策略分數",
            "ok": None if not strategy_backtest_score.get("ok") else _brain_float(strategy_backtest_score.get("score")) >= 0.55,
            "value": (
                f"{strategy_backtest_score.get('text')}｜"
                f"可用 {strategy_available}｜"
                f"通過 {strategy_backtest_score.get('passed', 0)}/{strategy_available or 0}"
            ),
        },
        {
            "label": "股票策略週期",
            "ok": strategy_horizon.get("ok"),
            "value": (
                f"{strategy_horizon.get('label') or '無資料'}｜"
                f"{strategy_horizon.get('holdingDays') or '-'}｜"
                f"主驗證 {strategy_horizon.get('primaryHorizon') or '-'}｜"
                f"{strategy_horizon.get('text') or '無資料'}"
            ),
        },
        {
            "label": "週轉/換手代理",
            "ok": (short_money_details.get("turnoverProxy") or {}).get("ok"),
            "value": (short_money_details.get("turnoverProxy") or {}).get("value") or "無資料",
        },
        {
            "label": "外資/投信連買佔量",
            "ok": (short_money_details.get("institutionalFlow") or {}).get("ok"),
            "value": (short_money_details.get("institutionalFlow") or {}).get("value") or "無資料",
        },
        {
            "label": "主力分點淨買",
            "ok": (short_money_details.get("branchFlow") or {}).get("ok"),
            "value": (short_money_details.get("branchFlow") or {}).get("value") or "無資料",
        },
        {
            "label": "盤中外盤/大單",
            "ok": (short_money_details.get("realtimeFlow") or {}).get("ok"),
            "value": (short_money_details.get("realtimeFlow") or {}).get("value") or "無資料",
        },
        {
            "label": "09:00-09:15預估量",
            "ok": (short_money_details.get("projectedVolume") or {}).get("ok"),
            "value": (short_money_details.get("projectedVolume") or {}).get("value") or "無資料",
        },
        {
            "label": "K線型態分數",
            "ok": None if not kline_score.get("ok") else _brain_float(kline_score.get("score")) >= 0.55,
            "value": (kline_score.get("text") or "無資料") + "｜" + "、".join((kline_score.get("patterns") or [])[:3]),
        },
        {
            "label": "K線量能配合",
            "ok": None if not kline_score.get("ok") else _brain_float(kline_components.get("volume")) >= 0.55,
            "value": "量能 " + ("無資料" if kline_score.get("volumeRatio") is None else f"{kline_score.get('volumeRatio'):.2f} 倍"),
        },
        {
            "label": "OBV 能量潮",
            "ok": technical_obv.get("ok"),
            "value": (
                f"{technical_obv_text}｜"
                f"5日變化 {_brain_number_text(technical_obv.get('deltaRatio'))} 倍均量｜"
                f"站上10日均線 {_brain_bool_text(technical_obv.get('aboveMa10'))}"
            ),
        },
        {
            "label": "K線均線結構",
            "ok": None if not kline_score.get("ok") else _brain_float(kline_components.get("ma")) >= 0.55,
            "value": f"MA5 {_brain_price(kline_score.get('ma5'))} / MA20 {_brain_price(kline_score.get('ma20'))}",
        },
        {
            "label": "K線大盤強弱",
            "ok": None if not kline_score.get("ok") else _brain_float(kline_components.get("market")) >= 0.55,
            "value": _brain_pct(kline_components.get("market")),
        },
        {"label": "量能放大", "ok": trade_gate.get("volumeExpanded"), "value": _brain_bool_text(trade_gate.get("volumeExpanded"))},
        {"label": "強於大盤", "ok": trade_gate.get("strongerThanMarket"), "value": _brain_bool_text(trade_gate.get("strongerThanMarket"))},
        {"label": "風險可控", "ok": trade_gate.get("riskOk"), "value": "risk penalty " + ("無資料" if risk_penalty is None else f"{risk_penalty:.3f}")},
        {"label": "大盤條件", "ok": trade_gate.get("marketOk"), "value": _brain_bool_text(trade_gate.get("marketOk"))},
    ]
    if use_model:
        conditions.extend([
            {"label": "獨立模型可用", "ok": model_ready, "value": prediction_error or (prediction or {}).get("modelVersion") or "等待模型"},
            {"label": "獨立模型分數達門檻", "ok": probability is not None and probability >= threshold, "value": f"{_brain_pct(probability)} / 門檻 {_brain_pct(threshold)}"},
            {"label": "Learning to Rank 排名前段", "ok": trade_gate.get("rankTop"), "value": _brain_pct(model_probabilities.get("learning_to_rank"))},
            {"label": "Isolation Forest 異常啟動", "ok": trade_gate.get("anomalyOk"), "value": _brain_pct(model_probabilities.get("isolation_forest"))},
        ])
    conditions.extend(v2_condition_rows)

    rule_rows = [
        {"label": "技術面指標分數", "role": "5MA/10MA+量能+OBV+KD+MACD+RSI", "value": technical_indicator_score.get("score")},
        {"label": "OBV 能量潮", "role": "收盤價量累積資金流向", "value": technical_obv.get("score")},
        {"label": "短線資金分數", "role": "週轉/法人連買/主力分點/外盤大單/預估量", "value": short_money_score.get("score")},
        {"label": "回測策略分數", "role": "決策流程+反轉10指標+Bias+續航+法人+KD+MACD+均線+區間偏離+融資券", "value": strategy_backtest_score.get("score")},
        {"label": "交易決策流程", "role": "進場/持有/離場/強制離場 + 反轉10指標", "value": (strategy_backtest_score.get("decisionFlow") or {}).get("score")},
        {"label": "股票策略週期", "role": (strategy_horizon.get("label") or "短中長期判斷"), "value": strategy_horizon.get("score")},
        {"label": "K線型態分數", "role": "K線+量能+均線+大盤", "value": kline_score.get("score")},
        {"label": "Brain v2 總分", "role": "K線+量能/OBV+大盤+籌碼/資金+回測策略+風控+資料可信度", "value": v2_score},
    ]
    model_rows = [
        {"label": "Logistic Regression", "role": "基準勝率", "value": model_probabilities.get("logistic")},
        {"label": "XGBoost", "role": "非線性勝率", "value": model_probabilities.get("xgboost")},
        {"label": "LightGBM", "role": "快速樹模型", "value": model_probabilities.get("lightgbm")},
        {"label": "Gradient Boosting", "role": "大量特徵補強", "value": model_probabilities.get("gradient_boosting")},
        {"label": "Learning to Rank", "role": "排序優先度", "value": model_probabilities.get("learning_to_rank")},
        {"label": "Isolation Forest", "role": "異常啟動", "value": model_probabilities.get("isolation_forest")},
        {"label": "獨立模型總分", "role": "僅供獨立模型驗證", "value": probability},
    ] if use_model else []

    next_steps = []
    if observe_only:
        next_steps.append("正式資料未完整前，只能觀察，不通知買賣。")
    else:
        next_steps.append("買賣前仍要看盤中價量、跳空、是否開高走低與停損位置。")
    if missing_core:
        next_steps.append("先補齊正式資料，缺資料仍維持無資料。")
    if soft_missing_core:
        next_steps.append("短線進場不因輔助資料單獨缺失就封鎖；核心資料失敗才只觀察。")
    if decision_action == "BUY_CANDIDATE":
        next_steps.append("若要進場，等盤中確認表也通過，再看資金控管。")
    elif not observe_only and market_component is not None and market_component < 0.55:
        next_steps.append("大盤未轉強前，不放大部位。")

    if observe_only:
        can_notify = False
    elif strategy_key == "portfolio_exit":
        can_notify = not bool(v2_entry_allowed)
    else:
        can_notify = bool(v2_entry_allowed)

    brain_v2_payload = {
        "score": v2_score,
        "text": _brain_pct(v2_score),
        "passCount": v2_pass_count,
        "componentCount": len(v2_components),
        "entryAllowed": bool(v2_entry_allowed),
        "sellDataReady": bool(v2_sell_data_ready),
        "decisionBlocked": bool(entry_decision_blocked or observe_only),
        "dataConfidence": data_confidence_score,
        "entryThreshold": entry_score_threshold,
        "dataConfidenceThreshold": data_confidence_threshold,
        "requiredComponents": sorted(required_component_keys),
        "requiredComponentFailures": [row.get("key") for row in required_component_failures],
        "technicalOverride": bool(technical_override),
        "softGate": v2_soft_gate,
        "scoreTrend": v2_score_trend,
        "components": [
            {
                **row,
                "text": _brain_pct(row.get("score")),
            }
            for row in v2_components
        ],
    }
    if use_model:
        brain_v2_payload["formalPass"] = bool(formal_pass)

    result = {
        "ok": True,
        "engine": "Brain Engine",
        "engineVersion": "brain-v2.6-rule-only-production",
        "context": context,
        "strategyProfile": {
            "key": strategy_key,
            "name": strategy_name,
            "entryThreshold": entry_score_threshold,
            "dataConfidenceThreshold": data_confidence_threshold,
            "requiredComponents": sorted(required_component_keys),
            "weights": response_weights,
        },
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "date": latest.get("date"),
        "recommendation": recommendation,
        "actionLabel": action_label,
        "action": decision_action,
        "observeOnly": observe_only,
        "canNotify": can_notify,
        "decisionBlocked": bool(entry_decision_blocked or observe_only),
        "entryAllowed": bool(v2_entry_allowed),
        "sellDataReady": bool(v2_sell_data_ready),
        "brainV2": brain_v2_payload,
        "currentPrice": close,
        "setupScore": setup_score,
        "riskPenalty": risk_penalty,
        "klineScore": kline_score,
        "technicalIndicatorScore": technical_indicator_score,
        "shortMoneyScore": short_money_score,
        "strategyBacktestScore": strategy_backtest_score,
        "decisionFlowScore": strategy_backtest_score.get("decisionFlow") or {},
        "strategyHorizon": strategy_horizon,
        "dataQuality": quality,
        "missingCore": missing_core,
        "missingAuxiliary": soft_missing_core,
        "blockers": blockers,
        "conditions": conditions,
        "ruleBreakdown": [
            {
                **row,
                "text": _brain_pct(row.get("value")),
            }
            for row in rule_rows
        ],
        "sources": source_rows,
        "nextSteps": next_steps,
    }
    if use_model:
        result.update({
            "confidence": probability,
            "threshold": threshold,
            "modelVersion": (prediction or {}).get("modelVersion") or (health.get("model") or {}).get("version") or "",
            "trainedAt": (prediction or {}).get("trainedAt") or (health.get("model") or {}).get("trainedAt") or "",
            "modelBreakdown": [
                {
                    **row,
                    "text": _brain_pct(row.get("value")),
                }
                for row in model_rows
            ],
            "prediction": prediction,
            "buySignal": buy_signal,
        })
    return result

