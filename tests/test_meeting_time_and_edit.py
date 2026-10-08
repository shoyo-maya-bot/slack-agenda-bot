"""会議時間に入らない議題の拒否・固定の議題で埋まる会議の警告・会議の変更と削除・担当者による取り下げ."""

from __future__ import annotations

from datetime import date

import pytest

from agenda_bot import service, views
from agenda_bot.app import Handlers
from agenda_bot.repository import MemoryRepository

TODAY = date(2026, 10, 2)  # 金曜. 次の開催日は 10/5（月）
OWNER, ADMIN, MEMBER, OTHER = "UOWNER", "UADMIN", "UMEMBER", "UOTHER"


@pytest.fixture()
def repo():
    return MemoryRepository()


@pytest.fixture()
def meeting(repo):
    return service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)  # 月曜 30 分


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kw):
        self.calls.append(kw)

    @property
    def last(self) -> dict:
        return self.calls[-1]


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


# --- 1. 議題の時間と会議の長さ ---


def test_draft_longer_than_meeting_is_rejected(repo, meeting):
    with pytest.raises(service.ValidationError) as e:
        service.add_draft(repo, meeting, TODAY, "長すぎる議題", "", 31, "", MEMBER)
    assert e.value.field == "minutes" and "30 分まで" in str(e.value)
    assert repo.drafts(meeting.meeting_id, "2026-10-05") == []


def test_draft_longer_than_remaining_after_fixed_is_rejected(repo, meeting):
    service.add_fixed(repo, meeting.meeting_id, "振り返り", 20)
    with pytest.raises(service.ValidationError, match="10 分まで"):
        service.add_draft(repo, meeting, TODAY, "議題", "", 15, "", MEMBER)
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 10, "", MEMBER)  # ちょうど入る
    assert d.minutes == 10


def test_meeting_filled_by_fixed_items_rejects_drafts_and_warns(repo, meeting):
    assert service.fill_warning(repo, meeting) == ""
    service.add_fixed(repo, meeting.meeting_id, "振り返り", 30)
    assert "固定の議題の合計（30 分）" in service.fill_warning(repo, meeting)
    with pytest.raises(service.ValidationError, match="固定の議題だけで会議時間が埋まっている"):
        service.add_draft(repo, meeting, TODAY, "議題", "", 1, "", MEMBER)


def test_add_submit_reports_too_long_draft_on_minutes_field(repo, meeting):
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": state(title="議題", minutes="45")}
    Handlers(repo, now=lambda: TODAY).add_submit(ack=ack, body={"user": {"id": MEMBER}}, view=view, client=client)
    assert ack.last["response_action"] == "errors" and "minutes" in ack.last["errors"]
    assert client.calls == []


def test_fixed_submit_warns_when_fixed_items_fill_meeting(repo, meeting):
    h = Handlers(repo, now=lambda: TODAY)
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": state(title="振り返り", minutes="30")}
    h.fixed_submit(ack=ack, body={"user": {"id": OWNER}}, view=view, client=client)
    assert ack.calls == [{}]
    assert [n for n, _ in client.calls] == ["chat_postEphemeral"]
    assert "会議時間（30 分）と同じ" in client.calls[0][1]["text"] and client.calls[0][1]["user"] == OWNER


# --- 3. 会議の変更と削除 ---


def test_update_meeting_changes_schedule_and_moves_upcoming_drafts(repo, meeting):
    a = service.add_draft(repo, meeting, TODAY, "A", "", 10, "", MEMBER)
    assert a.meeting_date == "2026-10-05"
    new, moved = service.update_meeting(repo, meeting, OWNER, "2", "14:00", "60", TODAY)
    assert (new.weekday, new.time, new.default_minutes) == (2, "14:00", 60)
    assert repo.get_meeting(meeting.meeting_id) == new
    assert [d.meeting_date for d in moved] == ["2026-10-07"]  # 水曜の開催日へ
    assert repo.drafts(meeting.meeting_id, "2026-10-05") == []
    assert [d.title for d in repo.drafts(meeting.meeting_id, "2026-10-07")] == ["A"]
    assert service.agenda(repo, new, TODAY).meeting_date == date(2026, 10, 7)


def test_update_meeting_permissions(repo, meeting):
    with pytest.raises(service.ValidationError, match="登録者と担当者"):
        service.update_meeting(repo, meeting, MEMBER, "0", "10:00", "45", TODAY)
    new, _ = service.update_meeting(repo, meeting, ADMIN, "0", "10:00", "45", TODAY, admins={ADMIN})
    assert new.default_minutes == 45


def test_update_meeting_validates_input(repo, meeting):
    with pytest.raises(service.ValidationError) as e:
        service.update_meeting(repo, meeting, OWNER, "0", "25:00", "30", TODAY)
    assert e.value.field == "time"
    with pytest.raises(service.ValidationError) as e:
        service.update_meeting(repo, meeting, OWNER, "7", "10:00", "30", TODAY)
    assert e.value.field == "weekday"
    assert repo.get_meeting(meeting.meeting_id) == meeting


def test_update_meeting_requires_settling_past_meetings_first(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "A", "", 10, "", MEMBER)
    after = date(2026, 10, 6)  # 10/5 の会議の後
    with pytest.raises(service.ValidationError, match="2026-10-05 の会議を確定か中止"):
        service.update_meeting(repo, meeting, OWNER, "2", "10:00", "30", after)


def test_delete_meeting_removes_meeting_and_its_items(repo, meeting):
    other = service.register_meeting(repo, "C1", OWNER, "別の会議", 1, "10:00", 30)
    service.add_fixed(repo, meeting.meeting_id, "振り返り", 5)
    service.add_draft(repo, meeting, TODAY, "A", "", 10, "", MEMBER)
    service.add_fixed(repo, other.meeting_id, "残る", 5)
    with pytest.raises(service.ValidationError):
        service.delete_meeting(repo, meeting, MEMBER)
    service.delete_meeting(repo, meeting, OWNER)
    assert repo.get_meeting(meeting.meeting_id) is None
    assert repo.fixed_items(meeting.meeting_id) == [] and repo.drafts(meeting.meeting_id, "2026-10-05") == []
    assert [f.title for f in repo.fixed_items(other.meeting_id)] == ["残る"]


def test_edit_and_delete_handlers(repo, meeting):
    h = Handlers(repo, admins={ADMIN}, now=lambda: TODAY)
    # 変更のモーダル: 担当者でも登録者でもない人には開かない
    ack, respond, client = Recorder(), Recorder(), FakeClient()
    h.edit_meeting(
        ack=ack, body={"user": {"id": MEMBER}, "trigger_id": "T"}, client=client, action={"value": meeting.meeting_id}, respond=respond
    )
    assert client.calls == [] and "登録者と担当者" in respond.last["text"]
    client = FakeClient()
    h.edit_meeting(
        ack=Recorder(),
        body={"user": {"id": OWNER}, "trigger_id": "T"},
        client=client,
        action={"value": meeting.meeting_id},
        respond=Recorder(),
    )
    assert client.calls[0][0] == "views_open" and client.calls[0][1]["view"]["callback_id"] == views.V_EDIT_MEETING
    # 送信
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": state(weekday="4", time="09:30", minutes="45")}
    h.edit_meeting_submit(ack=ack, body={"user": {"id": ADMIN}}, view=view, client=client)
    assert ack.calls == [{}] and "毎週金曜 09:30〜、45 分" in client.calls[0][1]["text"]
    ack = Recorder()
    h.edit_meeting_submit(ack=ack, body={"user": {"id": MEMBER}}, view=view, client=FakeClient())
    assert ack.last["response_action"] == "errors"
    # 削除
    respond, client = Recorder(), FakeClient()
    h.delete_meeting(ack=Recorder(), body={"user": {"id": MEMBER}}, client=client, action={"value": meeting.meeting_id}, respond=respond)
    assert repo.get_meeting(meeting.meeting_id) is not None and client.calls == []
    respond, client = Recorder(), FakeClient()
    h.delete_meeting(ack=Recorder(), body={"user": {"id": OWNER}}, client=client, action={"value": meeting.meeting_id}, respond=respond)
    assert repo.get_meeting(meeting.meeting_id) is None
    assert respond.last["text"] == "会議を削除しました" and client.calls[0][0] == "chat_postMessage"


def test_menu_has_edit_and_delete_with_confirm(meeting):
    elements = views.menu([meeting])[1]["elements"]
    by_id = {e["action_id"]: e for e in elements}
    assert views.A_EDIT_MEETING in by_id and "confirm" in by_id[views.A_DELETE_MEETING]


# --- 4. AGENDA_ADMINS の担当者による取り下げ ---


def test_admin_can_withdraw_others_draft(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", MEMBER)
    with pytest.raises(service.ValidationError):
        service.remove_draft(repo, meeting, d.meeting_date, d.item_id, OTHER, admins={ADMIN})
    service.remove_draft(repo, meeting, d.meeting_date, d.item_id, ADMIN, admins={ADMIN})
    assert repo.drafts(meeting.meeting_id, d.meeting_date) == []


def test_remove_handler_passes_admins(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", MEMBER)
    respond = Recorder()
    Handlers(repo, admins={ADMIN}, now=lambda: TODAY).remove(
        ack=Recorder(),
        action={"value": f"{meeting.meeting_id}|{d.meeting_date}|{d.item_id}"},
        body={"user": {"id": ADMIN}},
        respond=respond,
    )
    assert respond.last["text"] == "取り下げました" and repo.drafts(meeting.meeting_id, d.meeting_date) == []


# --- 2. 保存先（moto）: 会議の削除が子の記録も消す ---


def test_dynamo_delete_meeting_with_moto(monkeypatch):
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
        m = service.register_meeting(r, "C1", OWNER, "定例", 0, "10:00", 30)
        service.add_fixed(r, m.meeting_id, "振り返り", 5)
        service.add_draft(r, m, TODAY, "A", "", 10, "", MEMBER)
        service.delete_meeting(r, m, OWNER)
        assert r.get_meeting(m.meeting_id) is None
        assert r.fixed_items(m.meeting_id) == [] and r.drafts(m.meeting_id, "2026-10-05") == []


def test_docs_state_owner_only_default_when_admins_empty():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "その会議を登録した人だけ" in example
    assert "空なら、その会議を登録した人だけ" in readme
    assert "空なら誰でもできる\n" not in readme
