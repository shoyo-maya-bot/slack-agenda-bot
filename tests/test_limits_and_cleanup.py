"""第 3 回の指摘の再現テスト: Canvas の消失・Block Kit の上限・W のユーザー ID・扱い済みの議題の後片付け."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from agenda_bot import service, views
from agenda_bot.app import Handlers
from agenda_bot.model import CanvasRecord, DraftItem, Meeting, accepted_expiry, agenda_markdown, build_agenda
from agenda_bot.repository import TTL_ATTRIBUTE, MemoryRepository

TODAY = date(2026, 10, 2)  # 金曜. 次の開催日は 2026-10-05（月）


@pytest.fixture()
def repo():
    return MemoryRepository()


@pytest.fixture()
def meeting(repo):
    return service.register_meeting(repo, "C1", "UOWNER", "定例", 0, "10:00", 240)


# --- 1. canvas_not_found なら記録を消して作り直す ---


class SlackApiError(Exception):
    """slack_sdk.errors.SlackApiError と同じく response["error"] にコードを持つ偽物."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class GoneCanvas:
    def __init__(self, gone: set[str], code: str = "canvas_not_found"):
        self.gone, self.code, self.calls, self.n = gone, code, [], 0

    def canvases_create(self, **kw):
        self.n += 1
        self.calls.append(("create", kw))
        return {"ok": True, "canvas_id": f"F_{self.n}"}

    def conversations_canvases_create(self, **kw):
        self.n += 1
        self.calls.append(("channel", kw))
        return {"ok": True, "canvas_id": f"F_CH{self.n}"}

    def canvases_edit(self, **kw):
        self.calls.append(("edit", kw))
        if kw["canvas_id"] in self.gone:
            raise SlackApiError(self.code)
        return {"ok": True}

    def canvases_access_set(self, **kw):
        self.calls.append(("access", kw))
        return {"ok": True}

    def canvases_delete(self, **kw):
        self.calls.append(("delete", kw))
        return {"ok": True}


@pytest.mark.parametrize("mode,key", [("standalone", "2026-10-05"), ("channel", "channel")])
def test_canvas_not_found_recreates(repo, meeting, mode, key):
    repo.put_canvas(CanvasRecord(meeting.meeting_id, key, "F_OLD"))
    c = GoneCanvas({"F_OLD"})
    canvas_id, created = service.publish_canvas(repo, c, meeting, TODAY, mode)
    assert created is True and canvas_id != "F_OLD"
    # 新しい ID を記録する（channel はチャンネル単位の記録。以前の版の会議単位の記録から写して使う）
    owner = service.channel_canvas_key(meeting.channel_id) if mode == "channel" else meeting.meeting_id
    assert repo.get_canvas(owner, key).canvas_id == canvas_id
    assert [k for k, _ in c.calls][0] == "edit"
    # 次は新しい Canvas を更新する（もう作らない）
    assert service.publish_canvas(repo, c, meeting, TODAY, mode) == (canvas_id, False)


def test_other_canvas_errors_are_not_swallowed(repo, meeting):
    repo.put_canvas(CanvasRecord(meeting.meeting_id, "2026-10-05", "F_OLD"))
    with pytest.raises(SlackApiError):
        service.publish_canvas(repo, GoneCanvas({"F_OLD"}, code="not_allowed"), meeting, TODAY)
    assert repo.get_canvas(meeting.meeting_id, "2026-10-05").canvas_id == "F_OLD"  # 記録は消さない


def test_canvas_not_found_while_someone_else_recreates_is_busy(repo, meeting):
    rec = CanvasRecord(meeting.meeting_id, "2026-10-05", "F_OLD")
    repo.put_canvas(rec)

    class Racing(GoneCanvas):
        def canvases_edit(self, **kw):
            repo.put_canvas(CanvasRecord(meeting.meeting_id, "2026-10-05", service.PENDING))  # 別の人が先に作り直し始めた
            return super().canvases_edit(**kw)

    c = Racing({"F_OLD"})
    with pytest.raises(service.Busy):
        service.publish_canvas(repo, c, meeting, TODAY)
    assert "create" not in [k for k, _ in c.calls]


# --- 2. Block Kit の上限（50 ブロック・3000 字） ---


def _many(repo, meeting, n: int, title: str = "議題") -> None:
    for i in range(n):
        service.add_draft(repo, meeting, TODAY, f"{title}{i}", "", 1, "", "UA")


def _check_blocks(blocks: list[dict], limit: int = views.MAX_MESSAGE_BLOCKS) -> None:
    assert len(blocks) <= limit
    for b in blocks:
        texts = [b.get("text", {}).get("text", "")] + [e.get("text", "") for e in b.get("elements", []) if e.get("type") == "mrkdwn"]
        for t in texts:
            assert len(t) <= views.MAX_TEXT


def test_agenda_blocks_fit_in_50_blocks(repo, meeting):
    _many(repo, meeting, 80)
    v = service.agenda(repo, meeting, TODAY)
    assert len(v.rows) == 80
    blocks = views.agenda_blocks(v, "", True)
    _check_blocks(blocks)
    assert "ほか" in json.dumps(blocks, ensure_ascii=False)  # 省略した件数を出す
    # チャンネルへの投稿（can_manage=None）も
    _check_blocks(views.agenda_blocks(v, ""))


def test_long_titles_in_deferred_context_fit_3000_chars(repo):
    m = service.register_meeting(repo, "C1", "UO", "定例", 0, "10:00", 1)
    for i in range(60):
        service.add_draft(repo, m, TODAY, "長" * 98 + f"{i:02d}", "", 1, "", "UA")
    v = service.agenda(repo, m, TODAY)
    assert len(v.deferred) == 59
    blocks = views.agenda_blocks(v, "", True)
    _check_blocks(blocks)
    assert "ほか" in blocks[-1]["elements"][0]["text"] or any("ほか" in json.dumps(b, ensure_ascii=False) for b in blocks)


def test_menu_fits_in_50_blocks(repo):
    ms = [service.register_meeting(repo, "C1", "UO", f"会議{i}", 0, "10:00", 30) for i in range(30)]
    blocks = views.menu(ms)
    _check_blocks(blocks)
    assert blocks[-1]["elements"][0]["action_id"] == views.A_REGISTER  # 登録ボタンは必ず残す
    assert "表示しきれない会議" in json.dumps(blocks, ensure_ascii=False)


def test_fixed_modal_fits_in_100_blocks(repo, meeting):
    items = [service.add_fixed(repo, meeting.meeting_id, f"固定{i}", 1) for i in range(150)]
    modal = views.fixed_modal(meeting, items)
    modal["blocks"].insert(0, {"type": "context", "elements": [{"type": "mrkdwn", "text": "x"}]})  # delete_fixed が足す分
    _check_blocks(modal["blocks"], views.MAX_MODAL_BLOCKS)


def test_handlers_respond_within_limits(repo, meeting):
    """show・取り下げ・確定・Canvas（message）の応答が 50 ブロックに収まる."""
    _many(repo, meeting, 70)
    sent: list[dict] = []

    class Client:
        def chat_postMessage(self, **kw):
            sent.append(kw)

    h = Handlers(repo, now=lambda: TODAY)
    noop = lambda *a, **k: None  # noqa: E731
    h.show(noop, {"value": meeting.meeting_id}, {"user": {"id": "UA"}}, lambda **kw: sent.append(kw))
    d = repo.drafts(meeting.meeting_id, "2026-10-05")[0]
    h.remove(noop, {"value": f"{meeting.meeting_id}|2026-10-05|{d.item_id}"}, {"user": {"id": "UA"}}, lambda **kw: sent.append(kw))
    h.canvas(noop, {"user": {"id": "UOWNER"}}, {"value": meeting.meeting_id}, Client(), lambda **kw: sent.append(kw))
    # 確定（過去の開催日にする）
    later = date(2026, 10, 6)
    h2 = Handlers(repo, now=lambda: later)
    h2.finalize(noop, {"value": f"{meeting.meeting_id}|2026-10-05"}, {"user": {"id": "UOWNER"}}, lambda **kw: sent.append(kw), Client())
    with_blocks = [kw["blocks"] for kw in sent if "blocks" in kw]
    assert len(with_blocks) == 4
    for b in with_blocks:
        _check_blocks(b)


# --- 3. メンション判定を U/W 両方に（AGENDA_ADMINS と揃える） ---


def test_w_user_id_is_mentioned_in_blocks_and_canvas(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "Grid の議題", "", 5, "W0ABC123", "W0ABC123")
    v = service.agenda(repo, meeting, TODAY)
    text = views.agenda_blocks(v, "")[2]["text"]["text"]
    assert "<@W0ABC123>" in text
    assert "| <@W0ABC123> |" in v.markdown


def test_non_id_presenter_is_not_mentioned(repo):
    m = Meeting("m", "C", "x", 0, "10:00", "U", 60)
    d = DraftItem("m", "2026-10-05", "i", "t", presenter="Ueda")  # U で始まるが ID ではない（小文字を含む）
    rows, total, deferred = build_agenda(m, date(2026, 10, 5), [], [d])
    assert "<@Ueda>" not in agenda_markdown(m, date(2026, 10, 5), rows, total, deferred)


# --- 4. 扱い済みの議題の後片付け（TTL）と drafts_before の範囲 ---


def test_accepted_drafts_get_ttl_and_others_do_not(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", "UA")
    raw = next(iter(repo.t["drafts"].values()))
    assert TTL_ATTRIBUTE not in raw  # 0 を入れると TTL で即消えるので、属性ごと省く
    accepted, _ = service.finalize(repo, meeting, "2026-10-05", date(2026, 10, 6))
    raw = next(iter(repo.t["drafts"].values()))
    assert raw["status"] == "accepted"
    assert raw[TTL_ATTRIBUTE] == accepted_expiry("2026-10-05") == int(datetime(2027, 4, 3, tzinfo=UTC).timestamp())


def test_drafts_before_does_not_read_settled_history(repo, meeting):
    """確定を重ねても、アジェンダを見るたびに過去の扱い済みの議題を全部読まない."""
    day = date(2026, 10, 5)
    for week in range(10):
        d = date.fromordinal(day.toordinal() + 7 * week)
        before = date.fromordinal(d.toordinal() - 3)
        for i in range(5):
            service.add_draft(repo, meeting, before, f"w{week}-{i}", "", 1, "", "UA")
        meeting = repo.get_meeting(meeting.meeting_id)
        service.finalize(repo, meeting, d.isoformat(), date.fromordinal(d.toordinal() + 1))
    meeting = repo.get_meeting(meeting.meeting_id)
    assert meeting.settled_through == "2026-12-07"
    repo.read_count = 0
    v = service.agenda(repo, meeting, date(2026, 12, 8))
    assert v.unsettled == []
    assert repo.read_count == 0  # 扱い済みの 50 件は読まない


def test_mark_settled_only_moves_forward(repo, meeting):
    repo.mark_settled(meeting.meeting_id, "2026-10-12")
    repo.mark_settled(meeting.meeting_id, "2026-10-05")
    assert repo.get_meeting(meeting.meeting_id).settled_through == "2026-10-12"


def test_dynamo_ttl_range_query_and_settled_with_moto(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    from agenda_bot.repository import DynamoRepository, create_tables

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        res = boto3.resource("dynamodb", region_name="ap-northeast-1")
        create_tables(res, "t_")
        ttl = res.meta.client.describe_time_to_live(TableName="t_drafts")["TimeToLiveDescription"]
        assert ttl["AttributeName"] == TTL_ATTRIBUTE and ttl["TimeToLiveStatus"] in ("ENABLED", "ENABLING")
        create_tables(res, "t_")  # 2 回目も落ちない
        repo = DynamoRepository(prefix="t_", resource=res)
        m = service.register_meeting(repo, "C1", "UO", "定例", 0, "10:00", 10)
        service.add_draft(repo, m, TODAY, "収まる", "", 10, "", "UA")
        service.add_draft(repo, m, TODAY, "収まらない", "", 10, "", "UA")
        item = res.Table("t_drafts").scan()["Items"][0]
        assert TTL_ATTRIBUTE not in item
        service.finalize(repo, m, "2026-10-05", date(2026, 10, 6))
        items = {i["title"]: i for i in res.Table("t_drafts").scan()["Items"]}
        assert int(items["収まる"][TTL_ATTRIBUTE]) == accepted_expiry("2026-10-05")
        assert TTL_ATTRIBUTE not in items["収まらない"]
        m2 = repo.get_meeting(m.meeting_id)
        assert m2.settled_through == "2026-10-05" and m2.name == "定例"
        repo.mark_settled(m.meeting_id, "2026-09-28")  # 古い日付では戻さない
        assert repo.get_meeting(m.meeting_id).settled_through == "2026-10-05"
        # キー条件で、確定済みの開催日（10-05）以前を読まない
        calls = []
        orig = repo.tables["drafts"].query

        def spy(**kw):
            calls.append(kw)
            return orig(**kw)

        monkeypatch.setattr(repo.tables["drafts"], "query", spy)
        repo.drafts_before(m.meeting_id, "2026-10-19", m2.settled_through)
        key_cond = calls[-1]["KeyConditionExpression"]
        assert key_cond.get_expression()["values"][1].get_expression()["values"][1:] == ("2026-10-06", "2026-10-19")
        # 未確定の 10-12 の議題（繰り越した分）は範囲に入る
        assert [d.title for d in repo.drafts_before(m.meeting_id, "2026-10-19", m2.settled_through)] == ["収まらない"]
        v = service.agenda(repo, m2, date(2026, 10, 13))
        assert v.unsettled == ["2026-10-12"]  # 確定済みの 10-05 は出さず、未確定の 10-12 だけ
