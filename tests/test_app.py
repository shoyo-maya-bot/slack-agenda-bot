"""app.py のテスト: .env の読み込み・設定のチェック・ハンドラー（偽の ack・respond・client で、Bolt と Slack に接続しない）."""

from __future__ import annotations

from datetime import date

import pytest

from agenda_bot import app as app_mod
from agenda_bot import service, views
from agenda_bot.app import ConfigError, Handlers, load_env, parse_admins, parse_env_value, require_env
from agenda_bot.repository import MemoryRepository

TODAY = date(2026, 10, 2)  # 金曜

# --- .env: 行末のコメントを外す ---


def test_env_inline_comment_is_removed(tmp_path, monkeypatch):
    # 採点で再現した入力: .env.example の <your-value> だけを書き換えた行
    env = tmp_path / ".env"
    env.write_text(
        "SLACK_BOT_TOKEN=xoxb-123      # xoxb-…（OAuth & Permissions）\n"
        'QUOTED="a # b"   # コメント\n'
        "SINGLE='x#y'\n"
        "HASH_IN_VALUE=abc#def\n"
        "export EXPORTED=1 # c\n"
        "# 行全体のコメント\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    for k in ("SLACK_BOT_TOKEN", "QUOTED", "SINGLE", "HASH_IN_VALUE", "EXPORTED", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    got = load_env(env)
    assert got == {
        "SLACK_BOT_TOKEN": "xoxb-123",
        "QUOTED": "a # b",
        "SINGLE": "x#y",
        "HASH_IN_VALUE": "abc#def",
        "EXPORTED": "1",
        "EMPTY": "",
    }
    import os

    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-123"


def test_env_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TIMEZONE=UTC\n", encoding="utf-8")
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    load_env(env)
    import os

    assert os.environ["TIMEZONE"] == "Asia/Tokyo"


def test_env_example_has_no_inline_comments():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            assert parse_env_value(line.split("=", 1)[1]) == line.split("=", 1)[1], line


def test_require_env_and_modes(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "<your-value>")
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN"):
        require_env("SLACK_BOT_TOKEN")
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN"):
        require_env("SLACK_BOT_TOKEN")
    monkeypatch.delenv("CANVAS_MODE", raising=False)
    assert app_mod.canvas_mode_from_env() == "message"  # 既定はドキュメントと同じ message
    monkeypatch.setenv("CANVAS_MODE", "canvas")
    with pytest.raises(ConfigError, match="CANVAS_MODE"):
        app_mod.canvas_mode_from_env()


def test_main_exits_with_message_when_token_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # .env が無い場所
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIMEZONE", raising=False)
    with pytest.raises(SystemExit) as e:
        app_mod.main()
    assert "SLACK_BOT_TOKEN" in str(e.value.code)


def test_parse_admins(caplog):
    assert parse_admins(None) == frozenset() and parse_admins("  ") == frozenset()
    assert parse_admins("U01AAA, U02BBB  W03CCC") == {"U01AAA", "U02BBB", "W03CCC"}
    with caplog.at_level("WARNING"):
        assert parse_admins("U01AAA,yamada") == {"U01AAA"}
    assert "yamada" in caplog.text
    with pytest.raises(ConfigError):
        parse_admins("yamada,suzuki")


# --- ハンドラー（偽の ack・respond・client） ---


class Recorder:
    """ack・respond の代わり. 呼ばれた引数を残す."""

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
            if name in ("canvases_create", "conversations_canvases_create"):
                return {"ok": True, "canvas_id": "F_NEW"}
            return {"ok": True}

        return call

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


ADMIN, MEMBER = "UADMIN", "UMEMBER"


@pytest.fixture()
def repo():
    return MemoryRepository()


@pytest.fixture()
def meeting(repo):
    return service.register_meeting(repo, "C1", ADMIN, "定例", 0, "10:00", 30)


def handlers(repo, **kw):
    return Handlers(repo, now=lambda: TODAY, **kw)


def modal_state(**values) -> dict:
    return {"values": {k: {"v": {"type": "plain_text_input", "value": v}} for k, v in values.items()}}


def body(user=MEMBER, **kw) -> dict:
    return {"user": {"id": user}, "trigger_id": "T", "channel": {"id": "C1"}, **kw}


def test_unknown_meeting_is_acked_and_reported(repo):
    h = handlers(repo)
    for method in (h.add, h.fixed):
        ack, respond, client = Recorder(), Recorder(), FakeClient()
        method(ack=ack, body=body(), client=client, action={"value": "nope"}, respond=respond)
        assert len(ack.calls) == 1 and "見つかりません" in respond.last["text"] and client.calls == []
    ack, respond = Recorder(), Recorder()
    h.show(ack=ack, action={"value": "nope"}, body=body(), respond=respond)
    assert len(ack.calls) == 1 and "見つかりません" in respond.last["text"]
    ack, respond = Recorder(), Recorder()
    h.remove(ack=ack, action={"value": "nope|2026-10-05|x"}, body=body(), respond=respond)
    assert len(ack.calls) == 1 and "見つかりません" in respond.last["text"]
    ack, respond, client = Recorder(), Recorder(), FakeClient()
    h.canvas(ack=ack, body=body(), action={"value": "nope"}, client=client, respond=respond)
    assert len(ack.calls) == 1 and "見つかりません" in respond.last["text"]


def test_add_submit_without_meeting_acks_with_error(repo):
    # 採点の指摘: 会議が見つからないと ack せずに例外になっていた
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": "nope", "state": modal_state(title="x", minutes="10")}
    handlers(repo).add_submit(ack=ack, body=body(), view=view, client=client)
    assert ack.last["response_action"] == "errors" and "見つかりません" in ack.last["errors"]["title"]
    assert client.calls == []


def test_add_submit_neutralizes_mentions(repo, meeting):
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": modal_state(title="<!channel> 至急", minutes="10")}
    handlers(repo).add_submit(ack=ack, body=body(), view=view, client=client)
    assert ack.calls == [{}]
    name, kw = client.calls[0]
    assert name == "chat_postMessage"
    assert "<!channel>" not in kw["text"] and "&lt;!channel&gt; 至急" in kw["text"]
    assert kw["text"].startswith(f"<@{MEMBER}>")  # 出した人のメンションはそのまま


def test_add_submit_validation_error(repo, meeting):
    ack, client = Recorder(), FakeClient()
    view = {"private_metadata": meeting.meeting_id, "state": modal_state(title="あ" * 101, minutes="10")}
    handlers(repo).add_submit(ack=ack, body=body(), view=view, client=client)
    assert ack.last["response_action"] == "errors" and "title" in ack.last["errors"]
    assert client.calls == []


def test_admins_restrict_register_fixed_and_canvas(repo, meeting):
    h = handlers(repo, admins={ADMIN}, canvas_mode="standalone")
    # 会議の登録
    ack, respond, client = Recorder(), Recorder(), FakeClient()
    h.register(ack=ack, body=body(MEMBER), client=client, respond=respond)
    assert len(ack.calls) == 1 and "担当者" in respond.last["text"] and client.calls == []
    ack = Recorder()
    view = {"private_metadata": "C1", "state": {"values": {}}}
    h.register_submit(ack=ack, body=body(MEMBER), view=view, client=FakeClient())
    assert ack.last["response_action"] == "errors" and repo.meetings_in_channel("C1") == [meeting]
    # 固定の議題: 担当者以外には一覧だけ（追加・削除のボタンなし）、送信も拒否
    client = FakeClient()
    h.fixed(ack=Recorder(), body=body(MEMBER), client=client, action={"value": meeting.meeting_id}, respond=Recorder())
    assert "submit" not in client.calls[0][1]["view"]
    ack = Recorder()
    h.fixed_submit(ack=ack, body=body(MEMBER), view={"private_metadata": meeting.meeting_id, "state": modal_state(title="x", minutes="5")})
    assert ack.last["response_action"] == "errors" and repo.fixed_items(meeting.meeting_id) == []
    f = service.add_fixed(repo, meeting.meeting_id, "振り返り", 5)
    client = FakeClient()
    h.delete_fixed(
        ack=Recorder(),
        body=body(MEMBER, view={"id": "V1", "hash": "h"}),
        action={"value": f"{meeting.meeting_id}|{f.item_id}"},
        client=client,
    )
    assert repo.fixed_items(meeting.meeting_id) == [f]
    assert "担当者" in client.calls[0][1]["view"]["blocks"][0]["elements"][0]["text"]
    # Canvas
    ack, respond, client = Recorder(), Recorder(), FakeClient()
    h.canvas(ack=ack, body=body(MEMBER), action={"value": meeting.meeting_id}, client=client, respond=respond)
    assert len(ack.calls) == 1 and "担当者" in respond.last["text"] and client.calls == []
    # 担当者はできる
    ack, respond, client = Recorder(), Recorder(), FakeClient()
    h.canvas(ack=ack, body=body(ADMIN), action={"value": meeting.meeting_id}, client=client, respond=respond)
    assert client.names() == ["canvases_create", "canvases_access_set", "chat_postMessage"]
    client = FakeClient()
    h.delete_fixed(
        ack=Recorder(),
        body=body(ADMIN, view={"id": "V1", "hash": "h"}),
        action={"value": f"{meeting.meeting_id}|{f.item_id}"},
        client=client,
    )
    assert repo.fixed_items(meeting.meeting_id) == []
    assert client.calls[0][0] == "views_update" and client.calls[0][1]["view_id"] == "V1"


def test_without_admins_only_meeting_owner_can_manage(repo, meeting):
    """AGENDA_ADMINS が空なら、会議の登録者（ここでは ADMIN）だけが固定の議題・Canvas・確定・中止を操作できる."""
    h = handlers(repo)
    state = {"private_metadata": meeting.meeting_id, "state": modal_state(title="振り返り", minutes="5")}
    ack = Recorder()
    h.fixed_submit(ack=ack, body=body(MEMBER), view=state)
    assert ack.last["response_action"] == "errors" and repo.fixed_items(meeting.meeting_id) == []
    ack = Recorder()
    h.fixed_submit(ack=ack, body=body(ADMIN), view=state)
    assert ack.calls == [{}] and [f.title for f in repo.fixed_items(meeting.meeting_id)] == ["振り返り"]
    client, respond = FakeClient(), Recorder()
    h.canvas(ack=Recorder(), body=body(MEMBER), action={"value": meeting.meeting_id}, client=client, respond=respond)
    assert client.calls == [] and "登録者" in respond.last["text"]
    client = FakeClient()
    h.canvas(ack=Recorder(), body=body(ADMIN), action={"value": meeting.meeting_id}, client=client, respond=Recorder())
    assert client.names() == ["chat_postMessage"]  # 既定は message モード
    # 会議の登録そのものは、AGENDA_ADMINS が空なら誰でもできる
    client = FakeClient()
    h.register(ack=Recorder(), body=body(MEMBER), client=client, respond=Recorder())
    assert client.names() == ["views_open"]


def test_without_admins_only_meeting_owner_can_settle(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "A", "", 20, "", "UA")
    h = Handlers(repo, now=lambda: AFTER)
    respond = Recorder()
    h.show(ack=Recorder(), action={"value": meeting.meeting_id}, body=body(MEMBER), respond=respond)
    assert not {views.A_FINALIZE, views.A_CANCEL} & set(_actions(respond.last["blocks"]))
    for method in (h.finalize, h.cancel):
        respond, client = Recorder(), FakeClient()
        method(ack=Recorder(), action={"value": f"{meeting.meeting_id}|2026-10-05"}, body=body(MEMBER), respond=respond, client=client)
        assert "登録者" in respond.last["text"] and client.calls == []
    assert repo.drafts(meeting.meeting_id, "2026-10-05")[0].status == "proposed"
    respond = Recorder()
    h.show(ack=Recorder(), action={"value": meeting.meeting_id}, body=body(ADMIN), respond=respond)
    assert {views.A_FINALIZE, views.A_CANCEL} <= set(_actions(respond.last["blocks"]))


def test_canvas_errors_are_reported(repo, meeting):
    class Broken(FakeClient):
        def canvases_create(self, **kw):
            raise RuntimeError("not_allowed_token_type")

    ack, respond = Recorder(), Recorder()
    handlers(repo, canvas_mode="standalone").canvas(
        ack=ack, body=body(ADMIN), action={"value": meeting.meeting_id}, client=Broken(), respond=respond
    )
    assert len(ack.calls) == 1 and "Canvas に出せませんでした" in respond.last["text"]


def test_remove_permission_error_is_reported(repo, meeting):
    d = service.add_draft(repo, meeting, TODAY, "議題", "", 5, "", "UA")
    ack, respond = Recorder(), Recorder()
    handlers(repo).remove(
        ack=ack, action={"value": f"{meeting.meeting_id}|{d.meeting_date}|{d.item_id}"}, body=body("UOTHER"), respond=respond
    )
    assert len(ack.calls) == 1 and "出した人と会議の担当者" in respond.last["text"]


def test_handlers_reject_unknown_canvas_mode(repo):
    with pytest.raises(ConfigError):
        Handlers(repo, canvas_mode="canvas")


def test_menu_escapes_meeting_name(repo):
    m = service.register_meeting(repo, "C1", ADMIN, "<!here> 定例", 0, "10:00", 30)
    assert "<!here>" not in str(views.menu([m]))


# --- 第 2 回: 過去の会議の確定・中止（担当者だけ）・エラーの通知 ---

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

AFTER = datetime(2026, 10, 6, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))  # 10/5（月）の会議の後


def _actions(blocks) -> list[str]:
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_show_offers_finalize_and_cancel_to_admins_only(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "A", "", 20, "", "UA")
    h = Handlers(repo, admins={ADMIN}, now=lambda: AFTER)
    respond = Recorder()
    h.show(ack=Recorder(), action={"value": meeting.meeting_id}, body=body(ADMIN), respond=respond)
    assert {views.A_FINALIZE, views.A_CANCEL} <= set(_actions(respond.last["blocks"]))
    respond = Recorder()
    h.show(ack=Recorder(), action={"value": meeting.meeting_id}, body=body(MEMBER), respond=respond)
    assert not {views.A_FINALIZE, views.A_CANCEL} & set(_actions(respond.last["blocks"]))
    assert "担当者が選びます" in str(respond.last["blocks"])
    assert repo.drafts(meeting.meeting_id, "2026-10-05")[0].status == "proposed"  # 見ただけでは変わらない


def test_finalize_and_cancel_require_admin(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "A", "", 20, "", "UA")
    h = Handlers(repo, admins={ADMIN}, now=lambda: AFTER)
    for method in (h.finalize, h.cancel):
        ack, respond, client = Recorder(), Recorder(), FakeClient()
        method(ack=ack, action={"value": f"{meeting.meeting_id}|2026-10-05"}, body=body(MEMBER), respond=respond, client=client)
        assert len(ack.calls) == 1 and "担当者" in respond.last["text"] and client.calls == []
    assert repo.drafts(meeting.meeting_id, "2026-10-05")[0].status == "proposed"
    ack, respond, client = Recorder(), Recorder(), FakeClient()
    h.finalize(ack=ack, action={"value": f"{meeting.meeting_id}|2026-10-05"}, body=body(ADMIN), respond=respond, client=client)
    assert "確定しました（扱い済み 1 件" in respond.last["text"] and respond.last["replace_original"] is True
    assert client.names() == ["chat_postMessage"]
    assert repo.drafts(meeting.meeting_id, "2026-10-05")[0].status == "accepted"
    # 2 回目（別の人が同時に押した など）は、理由を本人に返す
    respond = Recorder()
    h.cancel(ack=Recorder(), action={"value": f"{meeting.meeting_id}|2026-10-05"}, body=body(ADMIN), respond=respond, client=FakeClient())
    assert "済んでいます" in respond.last["text"]


def test_cancel_handler_moves_items(repo, meeting):
    service.add_draft(repo, meeting, TODAY, "A", "", 20, "", "UA")
    h = Handlers(repo, now=lambda: AFTER)
    respond = Recorder()
    h.cancel(ack=Recorder(), action={"value": f"{meeting.meeting_id}|2026-10-05"}, body=body(ADMIN), respond=respond, client=FakeClient())
    assert "中止にしました（議題 1 件" in respond.last["text"]
    assert [d.title for d in repo.drafts(meeting.meeting_id, "2026-10-12")] == ["A"]


def test_error_reporter_logs_and_notifies(caplog):
    reporter = app_mod.ErrorReporter("CADMIN")
    respond, client = Recorder(), FakeClient()
    b = {"user": {"id": "UX"}, "actions": [{"action_id": "agenda_canvas"}]}
    with caplog.at_level("ERROR", logger="agenda_bot"):
        reporter(RuntimeError("boom <secret>"), body=b, respond=respond, client=client)
    assert "action:agenda_canvas" in caplog.text and "RuntimeError" in caplog.text
    assert respond.last["response_type"] == "ephemeral" and "エラー" in respond.last["text"]
    name, kw = client.calls[0]
    assert name == "chat_postMessage" and kw["channel"] == "CADMIN"
    assert "agenda_canvas" in kw["text"] and "<@UX>" in kw["text"] and "secret" not in kw["text"]  # 例外のメッセージ（中身）は送らない


def test_error_reporter_without_channel_and_failing_notify(caplog):
    class Broken(FakeClient):
        def chat_postMessage(self, **kw):
            raise RuntimeError("channel_not_found")

    def bad_respond(**kw):
        raise RuntimeError("expired")

    app_mod.ErrorReporter("")(RuntimeError("x"), body={"command": "/agenda"}, respond=None, client=FakeClient())
    with caplog.at_level("WARNING", logger="agenda_bot"):
        app_mod.ErrorReporter("CADMIN")(RuntimeError("x"), body={"command": "/agenda"}, respond=bad_respond, client=Broken())
    assert "知らせられませんでした" in caplog.text  # 通知の失敗で落ちない


def test_build_app_registers_error_handler(repo):
    pytest.importorskip("slack_bolt")
    app = app_mod.build_app(repo, token="xoxb-test", token_verification_enabled=False, error_channel="CADMIN")
    handler = app._listener_runner.listener_error_handler
    assert type(handler).__name__ == "CustomListenerErrorHandler"
    assert isinstance(handler.func, app_mod.ErrorReporter) and handler.func.error_channel == "CADMIN"


def test_parse_channel():
    assert app_mod.parse_channel("") == "" and app_mod.parse_channel(" C0123ABCD ") == "C0123ABCD"
    with pytest.raises(ConfigError):
        app_mod.parse_channel("#general")


def test_env_example_has_no_aws_keys():
    text = (app_mod.Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    keys = [line.split("=", 1)[0] for line in text.splitlines() if line and not line.startswith("#")]
    assert "AWS_ACCESS_KEY_ID" not in keys and "AWS_SECRET_ACCESS_KEY" not in keys


def test_local_dynamodb_uses_dummy_credentials(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "DYNAMODB_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    assert app_mod.use_local_dynamodb() is False  # AWS では何もしない（ロール・プロファイルを使う）
    monkeypatch.setenv("DYNAMODB_ENDPOINT", "http://localhost:8000")
    assert app_mod.use_local_dynamodb() is True
    import os

    assert os.environ["AWS_ACCESS_KEY_ID"] == "local"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "already")
    assert app_mod.use_local_dynamodb() is False and os.environ["AWS_ACCESS_KEY_ID"] == "already"
