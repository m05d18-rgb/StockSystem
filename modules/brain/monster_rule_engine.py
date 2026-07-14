"""
monster_rule_engine.py — 妖股短線規則引擎(燈號式判斷，不是 Brain v2 那種加權分數)

妖股短線交易時間壓力大、訊號變化快，Brain v2「10個加權分量+複雜必要條件」
的骨架不適合直接套用——使用者在盤中需要的是清楚的「能不能買」燈號，不是
一堆條件卡片。這裡刻意只挑跟短線動能真正相關的資料維度(量能、當沖比例、
主力籌碼、強於大盤、過熱風控)，不放長期基本面(PER/殖利率/營收成長)這類
跟「今天要不要追這檔妖股」無關的慢變數，也不強塞正式模型機率進來(避免
門檻語意打架)。

目前是第一階段(收集資料、不影響既有 buyAllowed 判斷)：這裡的輸出只會另外
存進 brain_v2_snapshots 表的 rule_* 欄位(跟 Brain v2 的 soft_gate 合併成
同一張實驗性訊號快照，同一個 symbol/price_date 兩套訊號放在一起方便比對)、
疊加在 monster_score_for_symbol 回傳結果的新欄位 ruleEngine 裡，完全不改動
既有 score/probability/buyAllowed 的語意。等累積幾週真實命中率資料後，
才考慮讓否決/過熱煞車去「降級」既有判斷(只能讓判斷變保守，不能單獨覆蓋放行)。
"""


def _rule(key, label, ok, value, note=""):
    return {"key": key, "label": label, "ok": ok, "value": value, "note": note}


def monster_rule_engine(inputs):
    """inputs 缺的欄位一律用 None；除了資料可信度外，缺資料的規則視為「無資料」
    中性處理(ok=None)，不計入必要條件失敗——避免覆蓋率不足的中小型妖股被誤殺。

    inputs 需要的欄位：
      volume_ratio: float | None            今日量 / 20日均量
      breakout: bool                        是否接近/突破 20 日高點
      counter_trend_strength: bool          大盤弱但個股逆勢強
      day_trade_ratio: float | None         當沖比例(0~1)
      main_force_buy_sell_recent: list      最近幾天主力買賣超(舊到新)，只看正負號
      broker_branch_net_buy_recent: list    最近幾天分點淨買超(舊到新)，只看正負號
      rsi: float | None
      ret5: float | None                    5 日報酬(%)
      change1: float | None                 今日漲跌(%)
      stock_stronger: bool                  是否強於大盤
      data_confidence: float | None         關鍵籌碼欄位覆蓋率(0~1)
    """
    rules = []

    volume_ratio = inputs.get("volume_ratio")
    if volume_ratio is None:
        volume_surge_ok, volume_note = None, "無量能資料"
    elif volume_ratio >= 3:
        volume_surge_ok, volume_note = True, "強訊號"
    elif volume_ratio >= 2:
        volume_surge_ok, volume_note = True, "中等，需搭配其他證據"
    else:
        volume_surge_ok, volume_note = False, "量能不足"
    rules.append(_rule("volumeSurge", "量能爆發", volume_surge_ok, volume_ratio, volume_note))

    breakout = bool(inputs.get("breakout"))
    counter_trend_strength = bool(inputs.get("counter_trend_strength"))
    price_structure_ok = breakout or counter_trend_strength
    rules.append(_rule(
        "priceStructure", "價格結構", price_structure_ok,
        "突破新高" if breakout else ("逆勢強勢" if counter_trend_strength else "未突破"),
    ))

    day_trade_ratio = inputs.get("day_trade_ratio")
    if day_trade_ratio is None:
        day_trade_ok, day_trade_note = None, "無當沖資料"
    elif day_trade_ratio <= 0.30:
        day_trade_ok, day_trade_note = True, "正常"
    elif day_trade_ratio <= 0.45:
        day_trade_ok, day_trade_note = True, "偏高，留意"
    else:
        day_trade_ok, day_trade_note = False, "當沖過熱"
    rules.append(_rule("dayTradeRatio", "當沖比例", day_trade_ok, day_trade_ratio, day_trade_note))

    main_force = [v for v in (inputs.get("main_force_buy_sell_recent") or []) if v is not None]
    branch_flow = [v for v in (inputs.get("broker_branch_net_buy_recent") or []) if v is not None]
    if len(main_force) < 2 or len(branch_flow) < 2:
        chip_ok, chip_note = None, "籌碼資料不足"
    else:
        chip_ok = bool(all(v > 0 for v in main_force[-2:]) and all(v > 0 for v in branch_flow[-2:]))
        chip_note = "主力+分點連二日買超" if chip_ok else "籌碼未同步集中"
    rules.append(_rule("chipConcentration", "籌碼集中度", chip_ok, None, chip_note))

    data_confidence = inputs.get("data_confidence")
    if data_confidence is None:
        data_confidence_ok, data_confidence_note = None, "無可信度資料"
    elif data_confidence >= 0.55:
        data_confidence_ok, data_confidence_note = True, ""
    else:
        data_confidence_ok, data_confidence_note = False, "覆蓋率偏低"
    rules.append(_rule("dataConfidence", "資料可信度", data_confidence_ok, data_confidence, data_confidence_note))

    required_keys = {"volumeSurge", "priceStructure", "dayTradeRatio", "chipConcentration", "dataConfidence"}
    required_failed = [row for row in rules if row["key"] in required_keys and row["ok"] is False]

    # 否決規則：過熱煞車。連三日主力買超+當沖比例維持低檔時，降級為輕倉觀察
    # 而不是全面封殺(避免把「籌碼真的在進駐」的過熱誤判成出貨)。
    # overheated判斷式跟ml_backend.py(monster_score_for_symbol)完全對齊
    # (含change1>9這條、以及counter_trend_strength的豁免)，避免同一筆
    # 資料兩套系統對「過熱」燈號打架。
    rsi = inputs.get("rsi")
    ret5 = inputs.get("ret5")
    change1 = inputs.get("change1")
    overheated = bool(
        (
            (rsi is not None and rsi > 82) or
            (ret5 is not None and ret5 > 22) or
            (volume_ratio is not None and volume_ratio > 5.5) or
            (change1 is not None and change1 > 9)
        ) and not counter_trend_strength
    )
    chip_streak_strong = len(main_force) >= 3 and all(v > 0 for v in main_force[-3:])
    overheat_override = bool(overheated and chip_streak_strong and day_trade_ratio is not None and day_trade_ratio <= 0.30)

    vetoed = False
    veto_reason = None
    if overheated and not overheat_override:
        vetoed = True
        veto_reason = "短線過熱(RSI/5日漲幅/量比觸頂)，暫不追高"
    elif overheated and overheat_override:
        veto_reason = "過熱但籌碼穩定，降級為輕倉觀察"

    change1 = inputs.get("change1")
    stock_stronger = bool(inputs.get("stock_stronger"))
    bonus_tags = []
    if change1 is not None and change1 >= 9.5:
        bonus_tags.append("limitUpTouch")
    if stock_stronger or counter_trend_strength:
        bonus_tags.append("marketRelative")

    if vetoed:
        action = "REJECT"
    elif overheated:  # overheat_override 為真的情況
        action = "WATCH_ONLY"
    elif required_failed:
        action = "WATCH_ONLY"
    else:
        action = "CAN_BUY_NOW"

    return {
        "action": action,
        "vetoed": vetoed,
        "vetoReason": veto_reason,
        "overheated": overheated,
        "rules": rules,
        "bonusTags": bonus_tags,
    }
