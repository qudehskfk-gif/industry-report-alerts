import json
from urllib.error import URLError

import pytest

from scripts import connect_telegram


TOKEN = "123456789:dummy-connect-token"
BOT_USERNAME = "ExpectedResearch_bot"
CHAT_ID = 42424242


def update(chat_id=CHAT_ID, text="/start", kind="private"):
    return {
        "update_id": chat_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": kind},
            "from": {"id": chat_id},
        },
    }


def stub_api(monkeypatch, updates, username=BOT_USERNAME):
    sent = []

    def call_api(token, method, payload):
        assert token == TOKEN
        if method == "getMe":
            return {"username": username, "is_bot": True}
        if method == "getUpdates":
            return updates
        assert method == "sendMessage"
        sent.append(payload)
        return {"message_id": 1}

    monkeypatch.setattr(connect_telegram, "call_api", call_api)
    return sent


def test_unique_private_start_sends_id_privately_without_logging(monkeypatch, capsys):
    sent = stub_api(monkeypatch, [update(), update(text="hello")])
    connect_telegram.connect(TOKEN, BOT_USERNAME)
    assert len(sent) == 1
    assert str(sent[0]["chat_id"]) == str(CHAT_ID)
    assert str(CHAT_ID) in sent[0]["text"]
    assert "TELEGRAM_CHAT_ID" in sent[0]["text"]
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert str(CHAT_ID) not in captured.out + captured.err


@pytest.mark.parametrize("updates", [[], [update(text="hello")], [update(kind="group")]])
def test_missing_private_start_fails_before_send(monkeypatch, updates):
    sent = stub_api(monkeypatch, updates)
    with pytest.raises(connect_telegram.ConnectionSetupError):
        connect_telegram.connect(TOKEN, BOT_USERNAME)
    assert sent == []


def test_multiple_private_starts_fail_before_send(monkeypatch):
    sent = stub_api(monkeypatch, [update(), update(chat_id=CHAT_ID + 1)])
    with pytest.raises(connect_telegram.ConnectionSetupError):
        connect_telegram.connect(TOKEN, BOT_USERNAME)
    assert sent == []


def test_group_start_does_not_compete_with_private_recipient(monkeypatch):
    sent = stub_api(monkeypatch, [update(), update(chat_id=-1001234, kind="group")])
    connect_telegram.connect(TOKEN, BOT_USERNAME)
    assert len(sent) == 1
    assert str(sent[0]["chat_id"]) == str(CHAT_ID)


def test_wrong_bot_username_fails_before_send(monkeypatch):
    sent = stub_api(monkeypatch, [update()], username="Unrelated_bot")
    with pytest.raises(connect_telegram.ConnectionSetupError):
        connect_telegram.connect(TOKEN, BOT_USERNAME)
    assert sent == []


def test_another_private_chat_is_ambiguous_even_without_start(monkeypatch):
    sent = stub_api(monkeypatch, [update(), update(chat_id=CHAT_ID + 1, text="hello")])
    with pytest.raises(connect_telegram.ConnectionSetupError):
        connect_telegram.connect(TOKEN, BOT_USERNAME)
    assert sent == []


def test_network_error_never_exposes_token_or_request_url(monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise URLError(f"https://api.telegram.org/bot{TOKEN}/getMe")

    monkeypatch.setattr(connect_telegram, "urlopen", unavailable)
    with pytest.raises(connect_telegram.ConnectionSetupError) as error:
        connect_telegram.call_api(TOKEN, "getMe", {})
    assert TOKEN not in str(error.value)
    assert "api.telegram.org" not in str(error.value)
    assert error.value.__suppress_context__
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_api_error_body_never_exposes_token_or_chat_id(monkeypatch, capsys):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"ok": False, "description": f"bad token {TOKEN}, chat {CHAT_ID}"}).encode()

    monkeypatch.setattr(connect_telegram, "urlopen", lambda *a, **k: Response())
    with pytest.raises(connect_telegram.ConnectionSetupError) as error:
        connect_telegram.call_api(TOKEN, "getMe", {})
    assert TOKEN not in str(error.value)
    assert str(CHAT_ID) not in str(error.value)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert str(CHAT_ID) not in captured.out + captured.err
