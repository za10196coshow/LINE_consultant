from app.line.client import LineClient


class FakeApiContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_):
        return False


class BrokenMessagingApi:
    def __init__(self, _):
        pass

    def push_message(self, _):
        raise RuntimeError("LINE unavailable")


def test_line_push_failure_is_contained(monkeypatch):
    line = LineClient("test-token")
    monkeypatch.setattr(line, "_api", lambda: FakeApiContext())
    monkeypatch.setattr("app.line.client.MessagingApi", BrokenMessagingApi)

    assert line.push("G123", "message") is False
