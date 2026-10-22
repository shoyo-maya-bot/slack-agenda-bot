"""第 7 回: 登録者の引き継ぎ（登録者がいなくなっても確定できる）・会議の削除と議題の追加の競合・アジェンダの組み方と README の一致."""

from __future__ import annotations

import re
import tomllib
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agenda_bot import app, clock, model, repository, service, views
from agenda_bot.app import Handlers
from agenda_bot.model import DraftItem, FixedItem, Meeting
from agenda_bot.repository import MemoryRepository

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SETUP = (ROOT / "docs/setup.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

TODAY = date(2026, 10, 2)  # 金曜. 月曜の会議の次の開催日は 10/5
AFTER = datetime(2026, 10, 6, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))  # 10/5（月）の会議の後
OWNER, NEXT, ADMIN, MEMBER = "UOWNER", "UNEXT", "UADMIN", "UMEMBER"


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


def state(meeting: Meeting, owner: str = "", **changes) -> dict:
    v = {"weekday": str(meeting.weekday), "time": meeting.time, "minutes": str(meeting.default_minutes), **changes}
    out: dict = {
        "weekday": {"v": {"type": "static_select", "selected_option": {"value": v["weekday"]}}},
        "time": {"v": {"type": "plain_text_input", "value": v["time"]}},
        "minutes": {"v": {"type": "plain_text_input", "value": v["minutes"]}},
        "owner": {"v": {"type": "users_select", "selected_user": owner or None}},
    }
    return {"values": out}


@pytest.fixture()
def repo():
    return MemoryRepository()


@pytest.fixture()
def meeting(repo):
    return service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)


# --- 1. 登録者の引き継ぎ ---


def test_owner_or_admin_can_transfer_and_others_cannot(repo, meeting):
    with pytest.raises(service.ValidationError, match="登録者と担当者"):
        service.transfer_meeting(repo, meeting, MEMBER, NEXT)
    with pytest.raises(service.ValidationError, match="引き継ぐ人を選んで"):
        service.transfer_meeting(repo, meeting, OWNER, "")
    m = service.transfer_meeting(repo, meeting, OWNER, NEXT)
    assert m.owner == NEXT and repo.get_meeting(meeting.meeting_id).owner == NEXT
    m = service.transfer_meeting(repo, m, ADMIN, MEMBER, admins={ADMIN})  # 担当者の代行
    assert repo.get_meeting(meeting.meeting_id).owner == MEMBER
    with pytest.raises(service.ValidationError, match="登録者と担当者"):
        service.transfer_meeting(repo, meeting, OWNER, NEXT)  # 引き継いだ後の元の登録者はもう変えられない


def test_transfer_rejects_concurrent_change(repo, meeting):
    assert repo.update_owner(meeting.meeting_id, NEXT, expected_owner=OWNER)
    assert not repo.update_owner(meeting.meeting_id, MEMBER, expected_owner=OWNER)  # 読んだときの登録者でなければ書かない
    assert not repo.update_owner("missing", MEMBER, expected_owner=OWNER)


def test_admin_unblocks_meeting_whose_owner_left(repo, meeting):
    """AGENDA_ADMINS が空で登録者がいなくなった会議: 担当者を置いて引き継げば、新しい登録者が確定できる（予定の変更は要らない）."""
    service.add_draft(repo, meeting, TODAY, "議題", "", 10, "", MEMBER)
    nobody = Handlers(repo, now=lambda: AFTER)
    respond = Recorder()
    nobody.finalize(
        ack=Recorder(),
        action={"value": f"{meeting.meeting_id}|2026-10-05"},
        body={"user": {"id": NEXT}},
        respond=respond,
        client=FakeClient(),
    )
    assert "引き継げます" in respond.last["text"]  # 権限の無い人に、引き継ぎ方を案内する
    # 予定を変えようとすると「先に確定」で止まるが、登録者だけの変更は通る
    with pytest.raises(service.ValidationError, match="先に 2026-10-05"):
        service.update_meeting(repo, meeting, OWNER, 0, "10:00", 30, AFTER)
    h = Handlers(repo, admins={ADMIN}, now=lambda: AFTER)
    ack, client = Recorder(), FakeClient()
    h.edit_meeting_submit(
        ack=ack, body={"user": {"id": ADMIN}}, view={"private_metadata": meeting.meeting_id, "state": state(meeting, NEXT)}, client=client
    )
    assert ack.calls == [{}]
    assert "登録者を <@UNEXT> さんに引き継ぎました" in client.calls[0][1]["text"]
    # 担当者を置かない運用に戻しても、新しい登録者が確定できる
    respond, client = Recorder(), FakeClient()
    nobody.finalize(
        ack=Recorder(), action={"value": f"{meeting.meeting_id}|2026-10-05"}, body={"user": {"id": NEXT}}, respond=respond, client=client
    )
    assert "確定しました" in respond.last["text"]


def test_edit_submit_with_schedule_and_owner_change(repo, meeting):
    h = Handlers(repo, now=lambda: TODAY)
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": state(meeting, NEXT, minutes="45")}
    h.edit_meeting_submit(ack=ack, body={"user": {"id": OWNER}}, view=view, client=client)
    m = repo.get_meeting(meeting.meeting_id)
    assert (m.default_minutes, m.owner) == (45, NEXT)
    assert "45 分" in client.calls[0][1]["text"] and "引き継ぎました" in client.calls[0][1]["text"]
    # 権限の無い人の引き継ぎだけの送信は、モーダルのエラー
    ack = Recorder()
    h.edit_meeting_submit(
        ack=ack, body={"user": {"id": MEMBER}}, view={"private_metadata": m.meeting_id, "state": state(m, MEMBER)}, client=FakeClient()
    )
    assert ack.last["response_action"] == "errors" and repo.get_meeting(m.meeting_id).owner == NEXT


def test_edit_modal_has_owner_select(meeting):
    owner = next(b for b in views.edit_meeting_modal(meeting)["blocks"] if b.get("block_id") == "owner")
    assert owner["element"] == {"type": "users_select", "action_id": "v", "initial_user": OWNER}


def test_dynamo_update_owner(aws):
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    r = DynamoRepository(resource=aws)
    m = service.register_meeting(r, "C1", OWNER, "定例", 0, "10:00", 30)
    assert service.transfer_meeting(r, m, OWNER, NEXT).owner == NEXT
    assert not r.update_owner(m.meeting_id, MEMBER, expected_owner=OWNER)
    assert not r.update_owner("missing", MEMBER, expected_owner=OWNER)


# --- 2. 会議の削除と議題の追加の競合 ---


@pytest.fixture()
def aws(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        yield boto3.resource("dynamodb", region_name="ap-northeast-1")


def _all_items(r, meeting_id: str) -> list:
    return [*r.fixed_items(meeting_id), *r.drafts_before(meeting_id, "9999-12-31"), r.get_canvas(meeting_id, "2026-10-05")]


def test_draft_added_during_delete_is_not_orphaned(aws, monkeypatch):
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    r = DynamoRepository(resource=aws)
    m = service.register_meeting(r, "C1", OWNER, "定例", 0, "10:00", 30)
    service.add_draft(r, m, TODAY, "前からある議題", "", 10, "", MEMBER)
    original = r._delete_children
    results: list = []

    def racing(meeting_id):
        if not results:  # 子の記録を消している最中に、別の人が議題を出す・固定の議題を足す（条件なしの書き込み）
            try:
                results.append(service.add_draft(r, m, TODAY, "削除と同時の議題", "", 10, "", MEMBER))
            except service.ValidationError as e:
                results.append(e)
            r.put_fixed(FixedItem(meeting_id, "f1", "削除と同時の固定の議題"))
        original(meeting_id)

    monkeypatch.setattr(r, "_delete_children", racing)
    service.delete_meeting(r, m, OWNER)
    assert isinstance(results[0], service.ValidationError)  # 削除中の会議には議題を書かない
    assert r.get_meeting(m.meeting_id) is None
    assert _all_items(r, m.meeting_id) == [None]  # 孤児の議題・固定の議題が残らない


def test_delete_resumes_after_meeting_is_gone(aws):
    """前回の削除が会議を消した後で止まっても、もう一度消せば残った子の記録を消す."""
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    r = DynamoRepository(resource=aws)
    r.put_draft(DraftItem("gone", "2026-10-05", "d1", "残った議題"))
    r.delete_meeting("gone")
    assert r.drafts("gone", "2026-10-05") == []


def test_put_draft_fallback_respects_deleting_mark(aws, monkeypatch):
    from botocore.exceptions import ClientError

    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    r = DynamoRepository(resource=aws)
    m = service.register_meeting(r, "C1", OWNER, "定例", 0, "10:00", 30)
    r.tables["meetings"].update_item(
        Key={"meeting_id": m.meeting_id},
        UpdateExpression=f"SET {repository.DELETING_ATTRIBUTE} = :t",
        ExpressionAttributeValues={":t": True},
    )

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "TransactWriteItems")

    monkeypatch.setattr(r.db.meta.client, "transact_write_items", denied)
    assert r.put_draft_if_schedule(DraftItem(m.meeting_id, "2026-10-05", "x", "t"), 0) is False


# --- 3. アジェンダの組み方（README の例をそのまま確かめる） ---


def test_readme_packing_example_matches_build_agenda():
    m = Meeting("m", "C1", "定例", 0, "10:00", OWNER, 60)
    fixed = [FixedItem("m", "f", "振り返り", minutes=15)]
    a = DraftItem("m", "2026-10-12", "a", "A", minutes=30, status="deferred", carried_from="2026-10-05", created_at="2026-10-09T00:00:00")
    b = DraftItem("m", "2026-10-12", "b", "B", minutes=20, created_at="2026-10-01T00:00:00")
    c = DraftItem("m", "2026-10-12", "c", "C", minutes=10, created_at="2026-10-02T00:00:00")
    rows, total, deferred = model.build_agenda(m, date(2026, 10, 12), fixed, [c, b, a])
    drafts = [r["title"] for r in rows if r["kind"] != "固定"]
    text = f"並ぶのは {' → '.join(drafts)} の {total} 分で、{''.join(d.title for d in deferred)} は次回へ"
    assert text in README
    assert "会議 60 分・固定の議題 15 分で、繰越 A 30 分、B 20 分、C 10 分の順" in README


# --- 4. README・docs と実際の値のずれを機械で見つける ---


def test_python_version_matches_everywhere():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    need = re.fullmatch(r">=(\d+\.\d+)", meta["project"]["requires-python"]).group(1)
    assert f"Python {need} 以上" in README
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert set(re.findall(r'python-version: "([\d.]+)"', ci)) == {need}
    assert meta["tool"]["mypy"]["python_version"] == need
    assert meta["tool"]["ruff"]["target-version"] == "py" + need.replace(".", "")


def test_input_limits_in_readme_match_constants():
    rows = dict(re.findall(r"^\| ([^|]+?) \| ([^|]+?) \|$", README, re.M))
    assert rows["議題・会議名"] == f"{service.MAX_TITLE} 文字まで"
    assert rows["補足"] == f"{service.MAX_DETAIL} 文字まで"
    assert rows["時間（議題・固定の議題・会議時間）"] == f"{service.MIN_MINUTES}〜{service.MAX_MINUTES} 分"
    with pytest.raises(service.ValidationError, match=f"{service.MIN_MINUTES}〜{service.MAX_MINUTES} 分"):
        service._minutes(service.MAX_MINUTES + 1, "minutes")
    service._minutes(service.MAX_MINUTES, "minutes")


def test_coverage_floor_in_readme_and_agents_matches_pyproject():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert f"`fail_under`（{meta['tool']['coverage']['report']['fail_under']}%）" in README


def test_tables_in_docs_match_repository():
    assert f"DynamoDB（{len(repository.TABLES)} テーブル）" in README
    for name in repository.TABLES:
        assert f"| `agenda_{name}` |" in SETUP, name
    assert f"`{repository.TTL_ATTRIBUTE}`" in SETUP and f"`{repository.CHANNEL_INDEX}`" in SETUP
    assert f"`{repository.DELETING_ATTRIBUTE}`" in SETUP
    assert f"開催日の {model.ACCEPTED_RETENTION_DAYS} 日後" in SETUP
    assert f"{int(service.PENDING_TIMEOUT.total_seconds() // 60)} 分たつと作り直せます" in SETUP


def test_canvas_modes_and_defaults_in_docs_match_code():
    documented = re.findall(r"^\| `(\w+)`(（既定）)? \|", SETUP.split("| CANVAS_MODE |")[1].split("\n\n")[0], re.M)
    assert [m for m, _ in documented] == list(app.CANVAS_MODES)
    assert [m for m, d in documented if d] == [app.DEFAULT_CANVAS_MODE]
    assert f"CANVAS_MODE={app.DEFAULT_CANVAS_MODE}" in ENV_EXAMPLE
    assert f"TIMEZONE={clock.DEFAULT_TIMEZONE}" in ENV_EXAMPLE and f"`TIMEZONE`（既定は `{clock.DEFAULT_TIMEZONE}`）" in README


def test_every_env_var_read_by_the_bot_is_in_env_example():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src/agenda_bot").glob("*.py"))
    names = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', src)) | set(re.findall(r'require_env\("([A-Z_]+)"\)', src))
    # AWS の認証情報は .env に書かない（ロール・プロファイル・DynamoDB Local のダミー）
    names -= {"AWS_ACCESS_KEY_ID", "AWS_PROFILE"}
    missing = sorted(n for n in names if not re.search(rf"^#? ?{n}=", ENV_EXAMPLE, re.M))
    assert missing == []


def test_log_line_for_empty_admins_says_owner_not_everyone():
    src = (ROOT / "src/agenda_bot/app.py").read_text(encoding="utf-8")
    assert '"全員"' not in src  # AGENDA_ADMINS が空でも「全員」ができるわけではない（登録者だけ）
    assert "なし（確定・中止などは各会議の登録者）" in src and "なし（確定・中止などは各会議の登録者）" in SETUP
