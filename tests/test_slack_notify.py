from orchestrator import slack_notify


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_notify_is_noop_when_not_enabled(monkeypatch):
    monkeypatch.delenv("AI_PM_SLACK_ENABLED", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.invalid/x")

    called = []
    monkeypatch.setattr(slack_notify, "urlopen", lambda *a, **k: called.append(1))

    slack_notify.notify("hello")

    assert called == []


def test_notify_is_noop_without_webhook_url(monkeypatch):
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    called = []
    monkeypatch.setattr(slack_notify, "urlopen", lambda *a, **k: called.append(1))

    slack_notify.notify("hello")

    assert called == []


def test_notify_posts_when_enabled_with_webhook(monkeypatch):
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.invalid/x")

    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["url"] = request.full_url
        captured["data"] = request.data
        return _FakeResponse()

    monkeypatch.setattr(slack_notify, "urlopen", fake_urlopen)

    slack_notify.notify("stav: hotovo")

    assert captured["url"] == "https://hooks.example.invalid/x"
    assert b"stav: hotovo" in captured["data"]


def test_notify_swallows_network_errors(monkeypatch):
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.invalid/x")

    def raising_urlopen(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr(slack_notify, "urlopen", raising_urlopen)

    # Must not raise - a Slack outage must never break the orchestrator run
    # it is only trying to report status about.
    slack_notify.notify("stav: hotovo")
