"""会議の短縮・固定の議題の追加で収まらなくなった議題の警告と取り下げ・会議の変更が確定済みの記録を戻さないこと・説明と検査コマンドの一致."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agenda_bot import service, views
from agenda_bot.app import Handlers, parse_admins
from agenda_bot.repository import MemoryRepository

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)  # 金曜. 次の開催日は 10/5（月）
OWNER, MEMBER = "UOWNER", "UMEMBER"


@pytest.fixture()
def repo():
    return MemoryRepository()


@pytest.fixture()
def meeting(repo):
    return service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 60)  # 月曜 60 分


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kw):
        self.calls.append(kw)


class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            return {"ok": True}

        return call


def state(**values) -> dict:
    out = {}
    for k, v in values.items():
        if k == "weekday":
            out[k] = {"v": {"type": "static_select", "selected_option": {"value": v}}}
        else:
            out[k] = {"v": {"type": "plain_text_input", "value": v}}
    return {"values": out}


# --- 収まらなくなった議題 ---


def test_unfit_drafts_after_shortening_meeting(repo, meeting):
    big = service.add_draft(repo, meeting, TODAY, "長い議題", "", 50, "", MEMBER)
    small = service.add_draft(repo, meeting, TODAY, "短い議題", "", 10, "", MEMBER)
    assert service.unfit_drafts(repo, meeting) == []
    new, _ = service.update_meeting(repo, meeting, OWNER, "0", "10:00", "30", TODAY)
    assert [d.item_id for d in service.unfit_drafts(repo, new)] == [big.item_id]
    warning = service.unfit_warning(repo, new)
    assert "長い議題" in warning and "50 分" in warning and "30 分" in warning and f"<@{MEMBER}>" in warning
    assert small.title not in warning


def test_unfit_drafts_after_adding_fixed_item(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 40, "", MEMBER)
    service.add_fixed(repo, meeting.meeting_id, "振り返り", 30)  # 使える時間は 30 分に
    assert [x.item_id for x in service.unfit_drafts(repo, meeting)] == [d.item_id]
    assert "使える時間（30 分 = 会議 60 分 − 固定の議題 30 分）" in service.unfit_warning(repo, meeting)


def test_unfit_warning_is_empty_when_everything_fits(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "議題", "", 30, "", MEMBER)
    assert service.unfit_warning(repo, meeting) == ""


def test_edit_meeting_submit_warns_about_unfit_drafts(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "長い議題", "", 50, "", MEMBER)
    h = Handlers(repo, now=lambda: TODAY)
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": state(weekday="0", time="10:00", minutes="30")}
    h.edit_meeting_submit(ack, {"user": {"id": OWNER}}, view, client)
    assert ack.calls == [{}]  # 拒否はしない
    text = client.calls[-1][1]["text"]
    assert "長い議題" in text and "収まりません" in text


def test_fixed_submit_warns_about_unfit_drafts(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "長い議題", "", 40, "", MEMBER)
    h = Handlers(repo, now=lambda: TODAY)
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": state(title="振り返り", minutes="30")}
    h.fixed_submit(ack, {"user": {"id": OWNER}}, view, client)
    assert ack.calls == [{}]
    assert [n for n, _ in client.calls] == ["chat_postEphemeral"]
    assert "長い議題" in client.calls[0][1]["text"]


def test_agenda_shows_unfit_drafts_with_withdraw_button(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "長い議題", "", 50, "", MEMBER)
    new, _ = service.update_meeting(repo, meeting, OWNER, "0", "10:00", "30", TODAY)
    v = service.agenda(repo, new, TODAY)
    assert [x.item_id for x in v.unfit] == [d.item_id]
    blocks = views.agenda_blocks(v, MEMBER, False)
    buttons = [b["accessory"] for b in blocks if b.get("accessory", {}).get("action_id") == views.A_REMOVE_DRAFT]
    assert any(b["value"].endswith(f"|{d.item_id}") for b in buttons)
    assert any("会議時間に収まりません" in b.get("text", {}).get("text", "") for b in blocks)
    # ボタンの値で取り下げられる（出した本人）
    meeting_id, meeting_date, item_id = next(b["value"] for b in buttons if b["value"].endswith(d.item_id)).split("|")
    service.remove_draft(repo, new, meeting_date, item_id, MEMBER)
    assert service.agenda(repo, new, TODAY).unfit == []


def test_agenda_marks_unfit_drafts_carried_from_past(repo, meeting):
    """確定で繰り越された議題も、収まらないものは unfit に出る（毎週繰り越され続けるのを知らせる）."""
    d = service.add_draft(repo, meeting, TODAY, "長い議題", "", 50, "", MEMBER)
    new, _ = service.update_meeting(repo, meeting, OWNER, "0", "10:00", "30", TODAY)
    after = date(2026, 10, 6)
    service.finalize(repo, new, "2026-10-05", after)
    v = service.agenda(repo, repo.get_meeting(new.meeting_id), after)
    assert [x.item_id for x in v.unfit] == [d.item_id]
    assert v.unfit[0].carried_from == "2026-10-05"
    blocks = views.agenda_blocks(v, MEMBER, False)
    assert any("2026-10-05 から繰越" in b.get("text", {}).get("text", "") for b in blocks)


# --- 会議の変更が settled_through を戻さない ---


def _stale_update_keeps_settled(repo):
    m = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    service.add_draft(repo, m, TODAY, "A", "", 10, "", MEMBER)
    stale = repo.get_meeting(m.meeting_id)  # 変更の画面を開いた時点の会議
    after = date(2026, 10, 6)
    service.finalize(repo, stale, "2026-10-05", after)  # 別の人が確定した
    assert repo.get_meeting(m.meeting_id).settled_through == "2026-10-05"
    new, _ = service.update_meeting(repo, stale, OWNER, "0", "10:00", "45", after)
    got = repo.get_meeting(m.meeting_id)
    assert got.settled_through == "2026-10-05"
    assert (got.weekday, got.time, got.default_minutes) == (0, "10:00", 45)
    assert new == got


def test_update_meeting_does_not_revert_settled_through(repo):
    _stale_update_keeps_settled(repo)


def test_update_meeting_does_not_recreate_deleted_meeting(repo, meeting):
    stale = repo.get_meeting(meeting.meeting_id)
    service.delete_meeting(repo, meeting, OWNER)
    with pytest.raises(service.ValidationError, match="見つかりません"):
        service.update_meeting(repo, stale, OWNER, "0", "10:00", "45", TODAY)
    assert repo.get_meeting(meeting.meeting_id) is None


def test_dynamo_update_meeting_keeps_settled_with_moto(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    from agenda_bot.repository import DynamoRepository, create_tables

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    with moto.mock_aws():
        res = boto3.resource("dynamodb", region_name="ap-northeast-1")
        create_tables(res)
        r = DynamoRepository(resource=res)
        _stale_update_keeps_settled(r)
        assert r.update_schedule("nope", 0, "10:00", 30) is False
        assert r.get_meeting("nope") is None


# --- 説明と検査コマンド ---


def test_parse_admins_doc_matches_owner_only_default():
    doc = parse_admins.__doc__ or ""
    assert "誰でも操作できる" not in doc
    assert "会議の登録者" in doc


def test_copilot_instructions_check_command_matches_ci():
    text = (ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "mypy" in ci
    assert "ruff check . && ruff format --check . && mypy && pytest" in text
