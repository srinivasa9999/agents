from shared.models import Alert
from shared.telegram_notify import format_alert, send_alerts


def _news_alert(on_delivered=None):
    return Alert(
        condition_key="news:abc",
        category="news",
        title="News keyword match: RBI",
        current_value="RBI_MPC meet: crude *spikes* 4%",
        threshold="RBI, crude",
        detail="RBI_MPC meet: crude *spikes* 4%\nhttps://example.com/a_b_c?x=1",
        on_delivered=on_delivered,
    )


def test_format_alert_escapes_markdown_special_chars():
    text = format_alert(_news_alert())
    # every underscore/asterisk/backtick/bracket coming from interpolated
    # fields must be escaped so Telegram's Markdown parser doesn't choke on it
    assert "RBI_MPC" not in text
    assert "\\_MPC" in text
    assert "\\*spikes\\*" in text
    assert "a\\_b\\_c" in text


def test_send_alerts_invokes_on_delivered_only_when_send_succeeds(monkeypatch):
    import shared.telegram_notify as telegram_notify

    monkeypatch.setattr(telegram_notify, "send_telegram_message", lambda *a, **k: True)
    delivered = []
    send_alerts("token", "chat", [_news_alert(on_delivered=lambda: delivered.append(True))])
    assert delivered == [True]


def test_send_alerts_does_not_invoke_on_delivered_when_send_fails(monkeypatch):
    import shared.telegram_notify as telegram_notify

    monkeypatch.setattr(telegram_notify, "send_telegram_message", lambda *a, **k: False)
    delivered = []
    send_alerts("token", "chat", [_news_alert(on_delivered=lambda: delivered.append(True))])
    assert delivered == []
