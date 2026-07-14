"""Official TWSE planned trading-day calendar helpers.

The annual TWSE schedule includes both closed dates and explicit trading-day
markers such as the first/last trading day around Lunar New Year.  Keep the
parser independent from the server so schedule decisions can be regression
tested without network access.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TWSE_HOLIDAY_SCHEDULE_URL = "https://www.twse.com.tw/holidaySchedule/holidaySchedule"
DGPA_DAILY_CLOSURE_URL = "https://www.dgpa.gov.tw/typh/daily/nds.html"
CALENDAR_VERSION = "twse-planned-calendar-v1"
EMERGENCY_CLOSURE_VERSION = "official-emergency-closure-v1"

OPEN_MARKERS = ("開始交易", "最後交易")
CLOSED_MARKERS = ("市場無交易", "休市", "放假")


class _HtmlTableTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        value = str(data or "").strip()
        if not value:
            return
        self.text.append(value)
        if self._cell is not None:
            self._cell.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _dgpa_market_closed(status_text: str) -> tuple[bool, str]:
    normalized = re.sub(r"\s+", "", str(status_text or ""))
    if not normalized:
        return False, "unknown"
    if any(marker in normalized for marker in ("未達停止上班", "照常上班", "正常上班")):
        return False, "open"
    if "停止上班" not in normalized:
        return False, "unknown"
    afternoon_only = (
        any(marker in normalized for marker in ("下午", "晚間", "晚上"))
        and "上午" not in normalized
        and "全日" not in normalized
        and "一天" not in normalized
    )
    if afternoon_only:
        return False, "afternoon_only"
    return True, "full_day_or_morning"


def parse_dgpa_taipei_closure(html: str) -> dict[str, Any]:
    parser = _HtmlTableTextParser()
    parser.feed(str(html or ""))
    all_text = " ".join(parser.text)
    date_match = re.search(r"(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})日\s*天然災害", all_text)
    if not date_match:
        raise ValueError("DGPA 停班頁面缺少公告日期")
    year = int(date_match.group(1)) + 1911
    page_date = dt.date(year, int(date_match.group(2)), int(date_match.group(3))).isoformat()

    taipei_status = ""
    for row in parser.rows:
        for index, cell in enumerate(row):
            if re.sub(r"\s+", "", cell) in {"臺北市", "台北市"}:
                taipei_status = " ".join(row[index + 1:]).strip()
                break
        if taipei_status:
            break
    if not taipei_status:
        raise ValueError("DGPA 停班頁面缺少臺北市狀態")

    market_closed, closure_scope = _dgpa_market_closed(taipei_status)
    return {
        "version": EMERGENCY_CLOSURE_VERSION,
        "date": page_date,
        "marketClosed": market_closed,
        "closureScope": closure_scope,
        "taipeiStatus": taipei_status,
        "reason": f"臺北市：{taipei_status}",
        "source": "DGPA Taipei closure / TWSE natural-disaster rule",
        "evidenceUrl": DGPA_DAILY_CLOSURE_URL,
    }


def fetch_dgpa_taipei_closure(timeout: int = 20) -> dict[str, Any]:
    request = Request(
        DGPA_DAILY_CLOSURE_URL,
        headers={
            "User-Agent": "Mozilla/5.0 StockAI/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8-sig", errors="replace")
    return parse_dgpa_taipei_closure(html)


def load_market_session_overrides(path: str | Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, dict):
        raise ValueError("臨時休市覆寫檔格式錯誤")
    entries: dict[str, dict[str, Any]] = {}
    for date_text, raw in raw_entries.items():
        day = _date(date_text)
        if day is None or not isinstance(raw, dict) or raw.get("isTradingDay") is not False:
            continue
        entries[day.isoformat()] = {
            "known": True,
            "isTradingDay": False,
            "date": day.isoformat(),
            "reason": str(raw.get("reason") or "官方臨時休市"),
            "source": str(raw.get("source") or "official emergency closure"),
            "evidenceUrl": str(raw.get("evidenceUrl") or ""),
            "ruleUrl": str(raw.get("ruleUrl") or ""),
            "emergencyClosure": True,
        }
    return entries


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def parse_twse_calendar(payload: dict[str, Any], year: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("stat") or "").lower() != "ok":
        raise ValueError("證交所開休市 API 回傳狀態異常")
    entries: dict[str, dict[str, Any]] = {}
    for raw in payload.get("data") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        day = _date(raw[0])
        if day is None or day.year != int(year):
            continue
        name = str(raw[1] or "").strip()
        description = str(raw[2] or "").strip() if len(raw) >= 3 else ""
        text = f"{name} {description}"
        explicitly_open = any(marker in text for marker in OPEN_MARKERS)
        explicitly_closed = any(marker in text for marker in CLOSED_MARKERS)
        entries[day.isoformat()] = {
            "isTradingDay": bool(explicitly_open and not explicitly_closed),
            "name": name,
            "description": description,
        }
    if not entries:
        raise ValueError(f"證交所 {year} 年開休市表沒有有效日期")
    return {
        "version": CALENDAR_VERSION,
        "year": int(year),
        "title": str(payload.get("title") or ""),
        "entries": entries,
    }


def planned_market_day(target_date: dt.date | str, calendar: dict[str, Any] | None) -> dict[str, Any]:
    day = target_date if isinstance(target_date, dt.date) else _date(target_date)
    if day is None:
        return {"known": False, "isTradingDay": None, "reason": "日期格式錯誤"}
    if day.weekday() >= 5:
        return {
            "known": True,
            "isTradingDay": False,
            "date": day.isoformat(),
            "reason": "週末",
            "source": "weekday",
        }
    if not isinstance(calendar, dict) or int(calendar.get("year") or 0) != day.year:
        return {
            "known": False,
            "isTradingDay": None,
            "date": day.isoformat(),
            "reason": "官方年度開休市表不可用",
            "source": "unavailable",
        }
    entry = (calendar.get("entries") or {}).get(day.isoformat())
    if entry:
        return {
            "known": True,
            "isTradingDay": entry.get("isTradingDay") is True,
            "date": day.isoformat(),
            "reason": entry.get("name") or "官方行事曆",
            "source": "TWSE annual holiday schedule",
        }
    return {
        "known": True,
        "isTradingDay": True,
        "date": day.isoformat(),
        "reason": "官方開休市表未列為休市日",
        "source": "TWSE annual holiday schedule",
    }


def fetch_twse_calendar(year: int, timeout: int = 20) -> dict[str, Any]:
    roc_year = int(year) - 1911
    query = urlencode({"response": "json", "queryYear": roc_year})
    request = Request(
        f"{TWSE_HOLIDAY_SCHEDULE_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 StockAI/1.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    return parse_twse_calendar(payload, int(year))
