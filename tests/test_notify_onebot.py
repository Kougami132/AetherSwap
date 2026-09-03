import requests


def test_send_onebot_private_with_bearer(monkeypatch):
    from app.notify import send_onebot

    calls = {}

    class Response:
        status_code = 200

        def json(self):
            return {"retcode": 0}

    def fake_post(url, **kwargs):
        calls.update(url=url, kwargs=kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    assert send_onebot("http://bot:5700/", "secret", "private", "123", "标题", "内容")
    assert calls["url"] == "http://bot:5700/send_private_msg"
    assert calls["kwargs"]["headers"]["Authorization"] == "Bearer secret"
    assert calls["kwargs"]["json"]["user_id"] == 123


def test_send_onebot_group_without_token(monkeypatch):
    from app.notify import send_onebot

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(requests, "post", lambda url, **kwargs: (captured.update(url=url, kwargs=kwargs) or Response()))
    assert send_onebot("http://bot:5700", "", "group", "456", "标题", "正文<br/>链接")
    assert captured["url"].endswith("/send_group_msg")
    assert "Authorization" not in captured["kwargs"]["headers"]
    assert captured["kwargs"]["json"]["group_id"] == 456
    assert captured["kwargs"]["json"]["message"] == "标题\n正文\n链接"


def test_send_onebot_rejects_invalid_configuration():
    from app.notify import send_onebot

    assert not send_onebot("", "", "private", "1", "t", "c")
    assert not send_onebot("http://bot", "", "channel", "1", "t", "c")
