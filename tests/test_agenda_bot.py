"""agenda-bot のテスト. 保存先はメモリ（DynamoDB は moto があれば同じテストを実行）、Slack は偽のクライアント."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

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


def test_build_agenda_puts_carried_items_first_and_skips_accepted():
    # deferred は「前の会議から繰り越した議題」. 後から出た議題より先に並べる。accepted（扱い済み）は並べない
    m = Meeting("m", "C", "x", 0, "10:00", "U", 20)
    new = DraftItem("m", "2026-10-05", "n", "新しい議題", minutes=10, created_at="2026-10-01T09:00:00+09:00")
    old = DraftItem("m", "2026-10-05", "o", "繰越の議題", minutes=10, created_at="2026-10-02T09:00:00+09:00", status="deferred")
    done = DraftItem("m", "2026-10-05", "d", "済み", status="accepted")
    rows, total, deferred = build_agenda(m, date(2026, 10, 5), [], [new, done, old])
    assert [(r["kind"], r["title"]) for r in rows] == [("繰越", "繰越の議題"), ("議題", "新しい議題")]
    assert total == 20 and deferred == []


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
    assert [k for k, _ in c.calls[-2:]] == ["edit", "access"]  # 置き換えて、権限を付け直す
    assert c.calls[-2][1]["changes"][0]["operation"] == "replace"


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
        # 条件付き put: 同じキーが既にあれば書かない
        assert repo.put_canvas_if(CanvasRecord(m.meeting_id, "2026-10-05", "F_DUP"), None) is False
        cur = repo.get_canvas(m.meeting_id, "2026-10-05")
        assert repo.put_canvas_if(CanvasRecord(m.meeting_id, "2026-10-05", "F2"), cur) is True
        repo.delete_canvas(m.meeting_id, "2026-10-05")
        assert repo.get_canvas(m.meeting_id, "2026-10-05") is None
        # 繰り越し（drafts_before のクエリ）
        service.add_draft(repo, m, TODAY, "収まる", "", 20, "", "UA")
        service.add_draft(repo, m, TODAY, "収まらない", "", 20, "", "UB")
        after = datetime(2026, 10, 6, 9, 0, tzinfo=JST)
        v = service.agenda(repo, m, after)
        assert [(r["kind"], r["title"]) for r in v.rows] == [("固定", "振り返り"), ("繰越", "収まらない")]
        assert [(x.title, x.status) for x in repo.drafts(m.meeting_id, "2026-10-05")] == [("収まる", "accepted")]
        assert repo.drafts_before(m.meeting_id, "2026-10-12") == []


def test_app_wiring_registers_handlers(repo):
    pytest.importorskip("slack_bolt")
    from agenda_bot.app import build_app

    app = build_app(repo, token="xoxb-test", token_verification_enabled=False)
    assert len(app._listeners) == 11  # /agenda 1、ボタン 7、モーダル 3


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


# --- 次の開催日: 今日の会議が終わった後に出した議題は翌週へ ---

JST = ZoneInfo("Asia/Tokyo")


def test_next_date_after_meeting_ends_is_next_week():
    m = Meeting("m", "C", "x", 0, "10:00", "U", 30)  # 月曜 10:00〜10:30
    monday = date(2026, 10, 5)
    assert m.next_date(datetime(2026, 10, 5, 9, 59, tzinfo=JST)) == monday  # 始まる前
    assert m.next_date(datetime(2026, 10, 5, 10, 15, tzinfo=JST)) == monday  # 会議中はまだ今日
    assert m.next_date(datetime(2026, 10, 5, 10, 30, tzinfo=JST)) == date(2026, 10, 12)  # 終わった後は翌週
    assert m.next_date(datetime(2026, 10, 5, 18, 0, tzinfo=JST)) == date(2026, 10, 12)
    assert m.next_date(monday) == monday  # 日付だけなら今までどおり当日


def test_add_draft_after_meeting_goes_to_next_week(repo, meeting):
    d = service.add_draft(repo, meeting, datetime(2026, 10, 5, 15, 0, tzinfo=JST), "会議の後に出した議題", "", 10, "", "UA")
    assert d.meeting_date == "2026-10-12"


# --- 繰り越し: 会議時間に収まらなかった議題は、開催日が過ぎたら次の開催日に移る ---


def test_overflow_is_carried_to_next_meeting(repo, meeting):
    service.add_fixed(repo, meeting.meeting_id, "振り返り", 10)
    service.add_draft(repo, meeting, TODAY, "A", "", 10, "", "UA")
    service.add_draft(repo, meeting, TODAY, "B（収まらない）", "", 20, "", "UB")
    assert [d.title for d in service.agenda(repo, meeting, TODAY).deferred] == ["B（収まらない）"]
    # 翌週（10/5 の会議の後）に新しい議題を出す
    after = datetime(2026, 10, 6, 9, 0, tzinfo=JST)
    service.add_draft(repo, meeting, after, "C（新しい）", "", 5, "", "UC")
    v = service.agenda(repo, meeting, after)
    assert v.meeting_date == date(2026, 10, 12)
    # 繰り越した B が、後から出た C より先に入る（C は 30 分に収まらず次回へ）
    assert [(r["kind"], r["title"]) for r in v.rows] == [("固定", "振り返り"), ("繰越", "B（収まらない）")]
    assert [d.title for d in v.deferred] == ["C（新しい）"]
    assert "| 2 | 繰越 | B（収まらない） |" in v.markdown
    # 元の開催日: 扱った A は accepted で残り、B は移った
    old = repo.drafts(meeting.meeting_id, "2026-10-05")
    assert [(d.title, d.status) for d in old] == [("A", "accepted")]
    moved = [d for d in repo.drafts(meeting.meeting_id, "2026-10-12") if d.title.startswith("B")][0]
    assert moved.status == "deferred" and moved.carried_from == "2026-10-05"
    # 何度呼んでも同じ（増えない・消えない）
    again = service.agenda(repo, meeting, after)
    assert [r["title"] for r in again.rows] == [r["title"] for r in v.rows]
    # 繰り越した議題も、出した人が取り下げられる
    service.remove_draft(repo, meeting, "2026-10-12", moved.item_id, "UB")
    assert "B（収まらない）" not in [r["title"] for r in service.agenda(repo, meeting, after).rows]


def test_carry_over_across_skipped_weeks(repo, meeting):
    # 2 週続けてボットを使わなかった: 10/5 で収まらなかった B は、次に開くアジェンダ（10/19）の先頭に来る（黙って消さない）
    service.add_draft(repo, meeting, TODAY, "A", "", 30, "", "UA")
    service.add_draft(repo, meeting, TODAY, "B", "", 30, "", "UB")
    v = service.agenda(repo, meeting, date(2026, 10, 13))
    assert v.meeting_date == date(2026, 10, 19)
    assert [(r["kind"], r["title"]) for r in v.rows] == [("繰越", "B")]
    assert [(d.title, d.status) for d in repo.drafts(meeting.meeting_id, "2026-10-05")] == [("A", "accepted")]


# --- Canvas: 作った直後に ID を保存し、重複して作らない ---


class FailingAccess(FakeCanvas):
    def __init__(self):
        super().__init__()
        self.fail = True
        self.n = 0

    def canvases_create(self, **kw):
        self.n += 1
        self.calls.append(("create", kw))
        return {"ok": True, "canvas_id": f"F_{self.n}"}

    def canvases_access_set(self, **kw):
        self.calls.append(("access", kw))
        if self.fail:
            raise RuntimeError("access_set failed")
        return {"ok": True}


def test_publish_canvas_saves_id_before_access_set(repo, meeting):
    c = FailingAccess()
    with pytest.raises(RuntimeError):
        service.publish_canvas(repo, c, meeting, TODAY)
    assert repo.get_canvas(meeting.meeting_id, "2026-10-05").canvas_id == "F_1"  # 失敗しても ID は残る
    c.fail = False
    assert service.publish_canvas(repo, c, meeting, TODAY) == ("F_1", False)  # 2 回目は作らずに更新
    assert [k for k, _ in c.calls].count("create") == 1
    assert c.calls[-1][0] == "access"  # 権限を付け直す


def test_publish_canvas_create_failure_allows_retry(repo, meeting):
    class Down(FakeCanvas):
        def canvases_create(self, **kw):
            raise RuntimeError("not_allowed")

    with pytest.raises(RuntimeError):
        service.publish_canvas(repo, Down(), meeting, TODAY)
    assert repo.get_canvas(meeting.meeting_id, "2026-10-05") is None  # 作成中の印は消える
    assert service.publish_canvas(repo, FakeCanvas(), meeting, TODAY) == ("F_NEW", True)


def test_publish_canvas_concurrent_press_creates_once(repo, meeting):
    # 1 回目の作成の途中で 2 回目が押された（連打）: 2 回目は作らずに「作成中」を返す
    seen = {}

    class Slow(FakeCanvas):
        def canvases_create(self, **kw):
            with pytest.raises(service.Busy):
                service.publish_canvas(repo, inner, meeting, TODAY)
            seen["inner"] = list(inner.calls)
            return super().canvases_create(**kw)

    inner = FakeCanvas()
    assert service.publish_canvas(repo, Slow(), meeting, TODAY) == ("F_NEW", True)
    assert seen["inner"] == []


def test_publish_canvas_stale_pending_is_taken_over(repo, meeting, monkeypatch):
    from agenda_bot import clock

    repo.put_canvas(CanvasRecord(meeting.meeting_id, "2026-10-05", service.PENDING, "2026-10-01T09:00:00+09:00"))
    monkeypatch.setattr(clock, "_utcnow", lambda: datetime(2026, 10, 2, 0, 0, tzinfo=UTC))
    assert service.publish_canvas(repo, FakeCanvas(), meeting, TODAY) == ("F_NEW", True)


def test_put_canvas_if_memory_semantics(repo):
    a = CanvasRecord("m", "d", "", "t1")
    assert repo.put_canvas_if(a, None) is True
    assert repo.put_canvas_if(CanvasRecord("m", "d", "", "t2"), None) is False  # もうある
    assert repo.put_canvas_if(CanvasRecord("m", "d", "F", "t3"), CanvasRecord("m", "d", "", "t9")) is False  # 期待と違う
    assert repo.put_canvas_if(CanvasRecord("m", "d", "F", "t3"), a) is True


# --- 入力の上限と無害化 ---


def test_title_length_limit(repo, meeting):
    with pytest.raises(service.ValidationError) as e:
        service.add_draft(repo, meeting, TODAY, "あ" * (service.MAX_TITLE + 1), "", 10, "", "UA")
    assert e.value.field == "title"
    with pytest.raises(service.ValidationError) as e:
        service.add_fixed(repo, meeting.meeting_id, "あ" * (service.MAX_TITLE + 1), 5)
    assert e.value.field == "title"
    with pytest.raises(service.ValidationError) as e:
        service.add_draft(repo, meeting, TODAY, "x", "あ" * (service.MAX_DETAIL + 1), 10, "", "UA")
    assert e.value.field == "detail"


def test_special_mentions_are_neutralized(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "<!channel> 至急 <@U999> & <#C1>", "<!here>", 5, "", "UA")
    assert views.esc(d.title) == "&lt;!channel&gt; 至急 &lt;@U999&gt; &amp; &lt;#C1&gt;"
    v = service.agenda(repo, meeting, TODAY)
    texts = str(views.agenda_blocks(v, ""))
    assert "<!channel>" not in texts and "<@U999>" not in texts
    assert "<!channel>" not in v.markdown and "<!here>" not in v.markdown
    assert "<@UA>" in v.markdown  # 担当者のメンションは残す


def test_remove_fixed(repo, meeting):
    f = service.add_fixed(repo, meeting.meeting_id, "振り返り", 5)
    assert service.remove_fixed(repo, meeting.meeting_id, f.item_id).title == "振り返り"
    assert repo.fixed_items(meeting.meeting_id) == []
    with pytest.raises(service.ValidationError, match="見つかりません"):
        service.remove_fixed(repo, meeting.meeting_id, f.item_id)
    modal = views.fixed_modal(meeting, [service.add_fixed(repo, meeting.meeting_id, "課題", 5)])
    buttons = [b["accessory"] for b in modal["blocks"] if "accessory" in b]
    assert buttons[0]["action_id"] == views.A_DELETE_FIXED and buttons[0]["value"].startswith(meeting.meeting_id + "|")
    readonly = views.fixed_modal(meeting, repo.fixed_items(meeting.meeting_id), can_manage=False)
    assert "submit" not in readonly and not any("accessory" in b for b in readonly["blocks"])


def test_drafts_keep_order_with_coarse_clock(repo, meeting, monkeypatch):
    # Windows の Python 3.12 のように時計の刻みが粗く、続けて出した議題が同じ時刻になっても、出した順に並ぶ
    from agenda_bot import clock

    monkeypatch.setattr(clock, "_utcnow", lambda: datetime(2026, 10, 2, 1, 0, tzinfo=UTC))
    titles = [f"議題{i}" for i in range(6)]
    for t in titles:
        service.add_draft(repo, meeting, TODAY, t, "", 5, "", "UA")
    assert [r["title"] for r in service.agenda(repo, meeting, TODAY).rows] == titles
