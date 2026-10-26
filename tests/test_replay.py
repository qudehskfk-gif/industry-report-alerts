from contextlib import nullcontext
from dataclasses import replace
from datetime import date
import sqlite3

import pytest

from reportbot import replay
from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State


DAY = date(2026, 10, 26)


def paper(source_id):
    return Report('naver', source_id, f'산업 심층 분석 {source_id}', '미래증권', date(2026, 10, 21),
                  f'https://example.com/{source_id}', f'https://example.com/{source_id}.pdf', sector='반도체')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = Config(tmp_path / 'state.sqlite3', '123:fake-replay-token', '-100424242', 20, 100, 86400, 14)
    State(config.state_path).close()
    sent, notices = [], []

    class Telegram:
        group = True
        fail_notices = False

        def __init__(self, token, chat_id):
            self.chat_id = chat_id

        def call(self, method, payload):
            assert method == 'getChat'
            return {'id': int(self.chat_id), 'type': 'supergroup' if self.group else 'private'}

        def send_report(self, report, decision):
            sent.append(report)

        def send_report_error(self, report, reason):
            if self.fail_notices:
                raise RuntimeError('private-credential-that-must-not-be-logged')
            notices.append(report)

    monkeypatch.setattr(replay, 'Telegram', Telegram)
    monkeypatch.setattr(replay.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(replay, 'make_session', lambda: nullcontext(object()))
    monkeypatch.setattr(replay, 'resolve', lambda report, session: report)
    monkeypatch.setattr(replay, 'classify', lambda report, *args: Decision(True, '산업 리포트 30쪽', 30, 'hash-' + report.source_id))
    return config, sent, notices, Telegram


def seed(config, source_id, timestamp='2026-10-26 04:00:00', outcome='sent'):
    item = paper(source_id)
    state = State(config.state_path)
    try:
        state.record(item, outcome, 'hash-' + source_id)
        with state.db:
            state.db.execute('UPDATE reports SET processed_at=? WHERE source_id=?', (timestamp, source_id))
    finally:
        state.close()
    return item


def ledger(config):
    with sqlite3.connect(config.state_path) as db:
        return (db.execute('SELECT * FROM reports ORDER BY source,source_id').fetchall(),
                db.execute('SELECT * FROM delivered ORDER BY kind,fingerprint').fetchall())


def test_korean_delivery_date_boundaries_and_only_actual_sent(setup):
    config, sent, notices, _ = setup
    seed(config, '1', '2026-10-25 14:59:59')
    first = seed(config, '2', '2026-10-25 15:00:00')
    last = seed(config, '3', '2026-10-26 14:59:59')
    seed(config, '4', '2026-10-26 15:00:00')
    seed(config, '5', outcome='pending')
    seed(config, '6', outcome='error_notified')
    before = ledger(config)
    assert replay.replay(config, DAY) == 0
    assert sent == [first, last]
    assert notices == []
    assert ledger(config) == before


def test_frozen_batch_and_checkpoints_skip_finished_without_changing_delivery_ledger(setup):
    config, sent, notices, _ = setup
    first, second = seed(config, '1'), seed(config, '2')
    before = ledger(config)
    assert replay.replay(config, DAY) == 0
    assert sent == [first, second]
    assert ledger(config) == before
    assert replay.replay(config, DAY) == 0
    assert sent == [first, second]
    late = seed(config, '3')
    assert replay.replay(config, DAY) == 0
    assert sent == [first, second]
    other_group = replace(config, chat_id='-100999999')
    assert replay.replay(other_group, DAY) == 0
    assert sent == [first, second, first, second, late]
    assert notices == []
    state = State(config.state_path)
    try:
        raw = str(state.db.execute('SELECT * FROM state_meta').fetchall())
        assert config.chat_id not in raw
        assert config.token not in raw
    finally:
        state.close()


def test_private_destination_is_rejected_before_snapshot_or_send(setup):
    config, sent, notices, telegram = setup
    seed(config, '1')
    telegram.group = False
    with pytest.raises(ValueError, match='그룹방'):
        replay.replay(config, DAY)
    assert sent == notices == []
    state = State(config.state_path)
    try:
        assert state.db.execute("SELECT count(*) FROM state_meta WHERE name LIKE 'group_replay:%'").fetchone()[0] == 0
    finally:
        state.close()


def test_error_link_is_terminal_for_batch_and_other_pdfs_continue(setup, monkeypatch):
    config, sent, notices, _ = setup
    failed, healthy = seed(config, '1'), seed(config, '2')
    before = ledger(config)
    original = replay.classify

    def partial(report, *args):
        if report == failed:
            raise RuntimeError('sensitive source URL')
        return original(report, *args)

    monkeypatch.setattr(replay, 'classify', partial)
    assert replay.replay(config, DAY) == 0
    assert sent == [healthy]
    assert notices == [failed]
    assert ledger(config) == before
    monkeypatch.setattr(replay, 'classify', lambda *args: pytest.fail('completed batch must not reprocess PDFs'))
    assert replay.replay(config, DAY) == 0
    assert sent == [healthy] and notices == [failed]


def test_failed_notice_recovery_retries_notice_only_and_redacts_errors(setup, monkeypatch, caplog):
    config, sent, notices, telegram = setup
    failed = seed(config, '1')
    telegram.fail_notices = True
    monkeypatch.setattr(replay, 'resolve', lambda *args: (_ for _ in ()).throw(RuntimeError('sensitive-source-secret')))
    assert replay.replay(config, DAY) == 1
    assert not sent and not notices
    assert 'private-credential' not in caplog.text
    assert 'sensitive-source-secret' not in caplog.text
    telegram.fail_notices = False
    monkeypatch.setattr(replay, 'resolve', lambda *args: pytest.fail('PDF must not be retried after first failure'))
    assert replay.replay(config, DAY) == 0
    assert notices == [failed]
    assert replay.replay(config, DAY) == 0
    assert notices == [failed]


def test_changed_pdf_hash_gets_link_instead_of_wrong_attachment(setup, monkeypatch):
    config, sent, notices, _ = setup
    item = seed(config, '1')
    monkeypatch.setattr(replay, 'classify', lambda *args: Decision(True, '30쪽', 30, 'different-pdf-hash'))
    assert replay.replay(config, DAY) == 0
    assert sent == [] and notices == [item]
