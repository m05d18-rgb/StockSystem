import tempfile
from pathlib import Path
from unittest.mock import patch

import server
from ml_backend import StockMLBackend


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_sell_notification_is_persisted_for_close_validation():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        payload = {
            "category": "portfolio_sell",
            "decision": {
                "code": "1409",
                "name": "新纖",
                "currentPrice": 25.8,
                "stopLoss": 30.92,
                "confirmSellPrice": 30.61,
                "decisionType": "stop",
                "decisionVerified": True,
                "decisionReasons": ["跌破最大停損", "即時報價已驗證"],
                "decisionAt": "2026-07-13 12:40:00",
                "decisionDataDate": "2026-07-13",
                "quoteSource": "Shioaji",
            },
        }
        with patch.object(server, "backend", backend), patch.object(
            server, "scheduler_today", return_value="2026-07-13"
        ):
            saved = server.record_frontend_portfolio_sell_notification(
                payload,
                {"ok": True, "sent": True},
                "1409 新纖已達出場條件",
                "line",
            )
            logs = server.list_exit_decision_logs()["logs"]

    assert saved == 1
    assert len(logs) == 1
    assert logs[0]["decision_date"] == "2026-07-13"
    assert logs[0]["symbol"] == "1409"
    assert logs[0]["channel"] == "line"
    assert logs[0]["reason"] == "frontend_portfolio_sell"
    assert logs[0]["decision_verified"] == 1


def test_non_sell_notifications_are_not_added_to_exit_audit():
    assert server.record_frontend_portfolio_sell_notification(
        {"category": "daily_digest"},
        {"ok": True, "sent": True},
        "摘要",
        "line",
    ) == 0


def test_legacy_sell_message_without_decision_payload_still_records_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        with patch.object(server, "backend", backend), patch.object(
            server, "scheduler_today", return_value="2026-07-13"
        ):
            saved = server.record_frontend_portfolio_sell_notification(
                {"category": "portfolio_sell"},
                {"ok": True, "sent": True},
                "【StockAI 後端出場提醒】\n1409 新纖已達出場條件",
                "line",
            )
            logs = server.list_exit_decision_logs()["logs"]

    assert saved == 1
    assert logs[0]["symbol"] == "1409"


def test_frontend_sends_decision_evidence_to_both_notification_channels():
    script = (ROOT / "app.js").read_text(encoding="utf-8")

    assert "body.decision = options.decision" in script
    assert 'category: "portfolio_sell",\n      decision,' in script
    assert 'sendLineNotification(lineMessage, { category: "portfolio_sell", decision })' in script


def test_portfolio_alert_color_uses_verified_backend_action_not_status_words():
    script = (ROOT / "app.js").read_text(encoding="utf-8")

    assert "function portfolioAlertStatusClass(alert)" in script
    assert "alert?.canNotify === true && alert?.decisionVerified === true" in script
    assert 'if (alert.typeClass === "sell") return "ok";' in script
    assert 'if (alert.typeClass === "confirm") return "danger";' in script
    assert "portfolioAlertStatusClass(priorityAlert)" in script
    assert 'status.includes("跌破")' not in script


def test_portfolio_alert_table_displays_broker_holding_price():
    script = (ROOT / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")

    assert "<th>我的持有價</th>" in html
    assert "holdingPrice: alert.holdingPrice" in script
    assert "alertPriceText(group.holdingPrice)" in script
    assert 'colspan="9"' in script
