import pytest

from scripts import connect_telegram_group


TOKEN = "123456789:dummy-group-connect-token"
BOT_USERNAME = "ExpectedResearch_bot"
OWNER_ID = 42424242
GROUP_ID = -1001234567


def update(group_id=GROUP_ID, owner_id=OWNER_ID, text=None, kind="group"):
    return {
        "update_id": abs(group_id),
        "message": {
            "text": text if text is not None else f"/start@{BOT_USERNAME} reports",
            "chat": {"id": group_id, "type": kind, "title": "private-group-title"},
            "from": {"id": owner_id},
        },
    }


def stub_api(monkeypatch, updates, username=BOT_USERNAME):
    sent = []

    def call_api(token, method, payload):
        assert token == TOKEN
        if method == "getMe":
            return {"is_bot": True, "username": username}
        if method == "getUpdates":
            assert payload["timeout"] == 0
            assert payload["limit"] == 100
            assert payload["allowed_updates"] == ["message"]
            return updates
        assert method == "sendMessage"
        sent.append(payload)
        return {"message_id": len(sent)}

    monkeypatch.setattr(connect_telegram_group, "call_api", call_api)
    return sent


def test_owner_group_start_sends_setup_only_to_group_and_owner(monkeypatch, capsys):
    sent = stub_api(monkeypatch, [update()])
    connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert [item["chat_id"] for item in sent] == [GROUP_ID, OWNER_ID]
    assert str(GROUP_ID) not in sent[0]["text"]
    assert str(GROUP_ID) in sent[1]["text"]
    assert "TELEGRAM_CHAT_ID" in sent[1]["text"]
    assert "/settings/secrets/actions/TELEGRAM_CHAT_ID" in sent[1]["text"]
    assert "그룹" in sent[1]["text"]
    captured = capsys.readouterr()
    for private in (TOKEN, str(OWNER_ID), str(GROUP_ID), "private-group-title"):
        assert private not in captured.out + captured.err


def test_owner_connect_command_supports_supergroup_and_bot_name_case(monkeypatch):
    sent = stub_api(monkeypatch, [update(kind="supergroup", text=f"/connect@{BOT_USERNAME.upper()}")])
    connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert [item["chat_id"] for item in sent] == [GROUP_ID, OWNER_ID]


def test_owner_directed_start_without_payload_connects_despite_other_members_starts(monkeypatch):
    sent = stub_api(monkeypatch, [
        update(owner_id=OWNER_ID + 1, group_id=GROUP_ID - 1, text=f"/start@{BOT_USERNAME}"),
        update(owner_id=OWNER_ID + 2, text=f"/start@{BOT_USERNAME}"),
        update(text=f"/start@{BOT_USERNAME}"),
    ])
    connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert [item["chat_id"] for item in sent] == [GROUP_ID, OWNER_ID]


def test_unrelated_private_foreign_owner_and_service_updates_do_not_enroll(monkeypatch):
    invalid = [
        None, {}, {"message": None},
        {"message": {"chat": {"id": GROUP_ID - 1, "type": "group"}, "new_chat_members": []}},
        update(group_id=OWNER_ID, kind="private"),
        update(group_id=GROUP_ID - 1, owner_id=OWNER_ID + 1),
        update(group_id=GROUP_ID - 2, text=f"/start@{BOT_USERNAME} wrong-payload"),
        update(group_id=GROUP_ID - 3, text="/connect@Another_bot"),
    ]
    sent = stub_api(monkeypatch, invalid + [update()])
    connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert [item["chat_id"] for item in sent] == [GROUP_ID, OWNER_ID]


@pytest.mark.parametrize("text", ["/start", "/connect", f"/start@{BOT_USERNAME} other"])
def test_missing_directed_enrollment_command_sends_nothing(monkeypatch, text):
    sent = stub_api(monkeypatch, [update(text=text)])
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert sent == []


def test_another_user_cannot_enroll_a_group(monkeypatch):
    sent = stub_api(monkeypatch, [update(owner_id=OWNER_ID + 1)])
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert sent == []


def test_multiple_owner_enrolled_groups_are_not_auto_selected(monkeypatch):
    sent = stub_api(monkeypatch, [update(), update(group_id=GROUP_ID - 1)])
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert sent == []


def test_repeated_commands_in_same_group_send_one_pair_of_messages(monkeypatch):
    sent = stub_api(monkeypatch, [update(), update(text=f"/connect@{BOT_USERNAME}")])
    connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert len(sent) == 2


@pytest.mark.parametrize("owner", ["", "0", "-1001234567", "not-a-number", "1.5"])
def test_owner_must_be_the_existing_positive_private_id(monkeypatch, owner):
    sent = stub_api(monkeypatch, [update()])
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group(TOKEN, owner, BOT_USERNAME)
    assert sent == []


def test_wrong_bot_identity_sends_nothing(monkeypatch):
    sent = stub_api(monkeypatch, [update()], username="AnotherResearch_bot")
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert sent == []


def test_missing_token_is_rejected_before_api_request(monkeypatch):
    monkeypatch.setattr(connect_telegram_group, "call_api", lambda *a, **k: pytest.fail("no API before token validation"))
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group("", str(OWNER_ID), BOT_USERNAME)


def test_malformed_updates_fail_without_any_send(monkeypatch):
    sent = stub_api(monkeypatch, {"unexpected": "shape"})
    with pytest.raises(connect_telegram_group.ConnectionSetupError):
        connect_telegram_group.connect_group(TOKEN, str(OWNER_ID), BOT_USERNAME)
    assert sent == []


def test_main_unexpected_exception_does_not_leak_request_or_private_ids(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER_ID))
    monkeypatch.setenv("EXPECTED_BOT_USERNAME", BOT_USERNAME)

    def fail(*args, **kwargs):
        raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/getUpdates {OWNER_ID} {GROUP_ID}")

    monkeypatch.setattr(connect_telegram_group, "call_api", fail)
    assert connect_telegram_group.main() == 1
    captured = capsys.readouterr()
    assert captured.err
    for private in (TOKEN, str(OWNER_ID), str(GROUP_ID), "api.telegram.org"):
        assert private not in captured.out + captured.err
