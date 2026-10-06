"""agenda-bot のテスト. 保存先はメモリ（DynamoDB は moto があれば同じテストを実行）、Slack は偽のクライアント."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from agenda_bot import service, views
from agenda_bot.model import CanvasRecord, DraftItem, Meeting, build_agenda
from agenda_bot.repository import TABLES, MemoryRepository

TODAY = date(2026, 10, 2)  # 金曜


@pytest.fixture()
def repo():
    return MemoryRepository()


@pytest.fixture()
def meeting(repo):
    return service.register_meeting(repo, "C1", "UOWNER", "開発チーム定例", 0, "10:00", 30)  # 月曜 30 分


def test_next_date():
    m = Meeting("m", "C", "x", 0, "10:00", "U")
    assert m.next_date(TODAY) == date(2026, 10, 5)
    assert m.next_date(date(2026, 10, 5)) == date(2026, 10, 5)  # 当日は当日


@pytest.mark.parametrize(
    ("args", "field"),
    [
        (("", 0, "10:00", 60), "name"),
        (("定例", 0, "25:00", 60), "time"),
        (("定例", 0, "10:00", "abc"), "minutes"),
        (("定例", 0, "10:00", 0), "minutes"),
    ],
)
def test_register_validation(repo, args, field):
    with pytest.raises(service.ValidationError) as e:
        service.register_meeting(repo, "C1", "U", *args)
    assert e.value.field == field


def test_agenda_orders_fixed_then_drafts_and_defers_overflow(repo, meeting):
    service.add_fixed(repo, meeting.meeting_id, "先週の振り返り", 10)
    service.add_fixed(repo, meeting.meeting_id, "課題の確認", 5, order=1)
    a = service.add_draft(repo, meeting, TODAY, "リリース日の判断", "決めたいこと: 日付", 10, "", "UA")
    service.add_draft(repo, meeting, TODAY, "長い議題", "", 20, "UB", "UB")
    v = service.agenda(repo, meeting, TODAY)
    assert v.meeting_date == date(2026, 10, 5)
    assert [r["title"] for r in v.rows] == ["課題の確認", "先週の振り返り", "リリース日の判断"]
    assert v.total == 25 and [d.title for d in v.deferred] == ["長い議題"]
    assert a.presenter == "UA"  # 説明する人が空なら出した人
    assert "| 3 | 議題 | リリース日の判断 | 10 分 | <@UA> |" in v.markdown
    assert "## 次回へ" in v.markdown and "決めたいこと: 日付" in v.markdown


def test_build_agenda_skips_deferred_status():
    m = Meeting("m", "C", "x", 0, "10:00", "U", 60)
    d = DraftItem("m", "2026-10-05", "i", "保留", status="deferred")
    rows, total, deferred = build_agenda(m, date(2026, 10, 5), [], [d])
    assert (rows, total, deferred) == ([], 0, [])


def test_remove_draft_permissions(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", "UA")
    with pytest.raises(service.ValidationError, match="出した人と会議の担当者"):
        service.remove_draft(repo, meeting, d.meeting_date, d.item_id, "UOTHER")
    service.remove_draft(repo, meeting, d.meeting_date, d.item_id, "UOWNER")  # 担当者は取り下げられる
    assert repo.drafts(meeting.meeting_id, d.meeting_date) == []
    with pytest.raises(service.ValidationError, match="見つかりません"):
        service.remove_draft(repo, meeting, d.meeting_date, d.item_id, "UA")


class FakeCanvas:
    def __init__(self):
        self.calls = []

    def canvases_create(self, **kw):
        self.calls.append(("create", kw))
        return {"ok": True, "canvas_id": "F_NEW"}

    def canvases_edit(self, **kw):
        self.calls.append(("edit", kw))
        return {"ok": True}

    def canvases_access_set(self, **kw):
        self.calls.append(("access", kw))
        return {"ok": True}

    def conversations_canvases_create(self, **kw):
        self.calls.append(("channel", kw))
        return {"ok": True, "canvas_id": "F_CH"}


def test_publish_canvas_standalone_then_edit(repo, meeting):
    c = FakeCanvas()
    service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", "UA")
    assert service.publish_canvas(repo, c, meeting, TODAY) == ("F_NEW", True)
    assert [k for k, _ in c.calls] == ["create", "access"]
    assert c.calls[1][1] == {"canvas_id": "F_NEW", "access_level": "write", "channel_ids": ["C1"]}
    assert c.calls[0][1]["document_content"]["type"] == "markdown"
    assert service.publish_canvas(repo, c, meeting, TODAY) == ("F_NEW", False)  # 同じ開催日は置き換え
    assert c.calls[-1][0] == "edit" and c.calls[-1][1]["changes"][0]["operation"] == "replace"


def test_publish_canvas_channel_mode(repo, meeting):
    c = FakeCanvas()
    assert service.publish_canvas(repo, c, meeting, TODAY, "channel") == ("F_CH", True)
    assert service.publish_canvas(repo, c, meeting, date(2026, 10, 9), "channel") == ("F_CH", False)  # 次の週も同じ Canvas
    assert [k for k, _ in c.calls] == ["channel", "edit"]


def test_views(repo, meeting):
    assert views.menu([])[1]["elements"][0]["action_id"] == views.A_REGISTER
    blocks = views.menu([meeting])
    assert [e["action_id"] for e in blocks[1]["elements"]] == [views.A_ADD_DRAFT, views.A_SHOW, views.A_FIXED, views.A_CANVAS]
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", "UA")
    ab = views.agenda_blocks(service.agenda(repo, meeting, TODAY), "UA")
    assert ab[2]["accessory"]["value"] == f"{meeting.meeting_id}|{d.meeting_date}|{d.item_id}"
    modal = views.draft_modal(meeting, "2026-10-05")
    assert {b.get("block_id") for b in modal["blocks"]} >= {"title", "detail", "minutes", "presenter"}
    state = {
        "state": {
            "values": {
                "title": {"v": {"type": "plain_text_input", "value": "x"}},
                "weekday": {"v": {"type": "static_select", "selected_option": {"value": "2"}}},
                "presenter": {"v": {"type": "users_select", "selected_user": None}},
            }
        }
    }
    assert views.values(state) == {"title": "x", "weekday": "2", "presenter": ""}
    err = views.errors(service.ValidationError("time", "形式"))
    assert err == {"response_action": "errors", "errors": {"time": "形式"}}


def test_dynamo_repository_with_moto(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    from agenda_bot.repository import DynamoRepository, create_tables

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        res = boto3.resource("dynamodb", region_name="ap-northeast-1")
        assert sorted(create_tables(res, "t_")) == sorted("t_" + k for k in TABLES)
        assert create_tables(res, "t_") == []  # 2 回目は作らない
        repo = DynamoRepository(prefix="t_", resource=res)
        m = service.register_meeting(repo, "C1", "UO", "定例", 0, "10:00", 30)
        assert repo.get_meeting(m.meeting_id) == m
        assert repo.meetings_in_channel("C1") == [m]
        service.add_fixed(repo, m.meeting_id, "振り返り", 5)
        d = service.add_draft(repo, m, TODAY, "議題", "", 10, "", "UA")
        v = service.agenda(repo, m, TODAY)
        assert [r["title"] for r in v.rows] == ["振り返り", "議題"] and isinstance(v.rows[0]["minutes"], int)
        service.remove_draft(repo, m, d.meeting_date, d.item_id, "UA")
        assert repo.drafts(m.meeting_id, d.meeting_date) == []
        assert service.publish_canvas(repo, FakeCanvas(), m, TODAY)[1] is True
        assert repo.get_canvas(m.meeting_id, "2026-10-05").canvas_id == "F_NEW"


def test_app_wiring_registers_handlers(repo):
    pytest.importorskip("slack_bolt")
    from agenda_bot.app import build_app

    app = build_app(repo, token="xoxb-test", token_verification_enabled=False)
    assert len(app._listeners) == 10  # /agenda 1、ボタン 6、モーダル 3


# --- タイムゾーン: サーバーが UTC でも、設定したタイムゾーン（既定は Asia/Tokyo）の日付を使う ---

FROZEN_UTC = datetime(2026, 10, 6, 23, 30, tzinfo=UTC)  # = 2026-10-07（水）08:30 JST


@pytest.fixture()
def frozen(monkeypatch):
    from agenda_bot import clock

    monkeypatch.setattr(clock, "_utcnow", lambda: FROZEN_UTC)
    monkeypatch.delenv("TIMEZONE", raising=False)
    return clock


def test_today_uses_jst_by_default(frozen):
    assert frozen.today() == date(2026, 10, 7)  # UTC ではまだ 10/6
    assert frozen.now().isoformat(timespec="seconds") == "2026-10-07T08:30:00+09:00"


def test_timezone_from_env_and_argument(frozen, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "UTC")
    assert frozen.today() == date(2026, 10, 6)
    assert frozen.today("Asia/Tokyo") == date(2026, 10, 7)
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValueError, match="TIMEZONE"):
        frozen.today()


def test_next_meeting_uses_jst_date_on_utc_server(frozen, repo):
    m = service.register_meeting(repo, "C1", "UO", "火曜定例", 1, "10:00", 30)  # 火曜
    # JST では 10/7（水）なので次回は 10/13（火）。UTC の日付（10/6 火）だと誤って 10/6 になる
    assert service.agenda(repo, m, frozen.today()).meeting_date == date(2026, 10, 13)
    d = service.add_draft(repo, m, frozen.today(), "議題", "", 10, "", "UA")
    assert d.meeting_date == "2026-10-13"
    assert d.created_at == "2026-10-07T08:30:00+09:00"
    assert CanvasRecord("m", "2026-10-13", "F").updated_at == "2026-10-07T08:30:00+09:00"


def test_app_today_uses_timezone(frozen):
    from agenda_bot.app import today_in

    assert today_in()() == date(2026, 10, 7)
    assert today_in("UTC")() == date(2026, 10, 6)
