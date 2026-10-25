from subprocess import CompletedProcess

import pytest

from scripts import deploy_github


def test_secret_uses_stdin_and_is_never_exposed_in_arguments_or_errors(monkeypatch, capsys):
    token = "123456789:private-telegram-token"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        # Even if the external CLI echoes the credential, an error must not expose it.
        return CompletedProcess(args, 1, stdout=f"echo {token}", stderr=f"failure: {token}")

    monkeypatch.setattr(deploy_github.subprocess, "run", fake_run)
    with pytest.raises(deploy_github.SetupError) as error:
        deploy_github.command(
            "gh", "secret", "set", "TELEGRAM_BOT_TOKEN", "--repo", "owner/repo",
            secret_input=token,
        )
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["input"] == token
    assert kwargs["capture_output"] is True
    assert all(token not in arg for arg in args)
    assert token not in str(error.value)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_existing_parent_git_repository_is_refused_before_commands_or_writes(tmp_path, monkeypatch):
    parent = tmp_path / "existing-project"
    parent.mkdir()
    git_directory = parent / ".git"
    git_directory.mkdir()
    sentinel = git_directory / "config"
    sentinel.write_text("existing repository config", encoding="utf-8")
    extracted_bot = parent / "industry-report-bot"
    extracted_bot.mkdir()
    monkeypatch.setattr(deploy_github, "ROOT", extracted_bot)
    monkeypatch.setattr(deploy_github.shutil, "which", lambda name: f"/usr/bin/{name}")

    def forbidden(*args, **kwargs):
        pytest.fail("existing repository must be rejected before spawning commands")

    monkeypatch.setattr(deploy_github.subprocess, "run", forbidden)
    with pytest.raises(deploy_github.SetupError, match="기존 Git 저장소"):
        deploy_github.prerequisites()
    assert sentinel.read_text(encoding="utf-8") == "existing repository config"
    assert list(extracted_bot.iterdir()) == []
