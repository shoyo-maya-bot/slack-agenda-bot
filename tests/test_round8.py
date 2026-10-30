"""第 8 回: CANVAS_MODE=channel を同じチャンネルの会議で共有する・お知らせの投稿の失敗で二重登録させない・.env の書き方の案内の一致."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agenda_bot import app, service
from agenda_bot.app import Handlers
from agenda_bot.model import CanvasRecord
from agenda_bot.repository import MemoryRepository

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)  # 金曜. 月曜の会議の次の開催日は 10/5
OWNER = "UOWNER"


class SlackApiError(Exception):
    """slack_sdk.errors.SlackApiError と同じく response["error"] にコードを持つ偽物."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kw):
        self.calls.append(kw)

    @property
    def last(self) -> dict:
        return self.calls[-1]


class Client:
    """Slack の偽物. channels に入っていないチャンネルへの chat_postMessage は not_in_channel にする."""

    def __init__(self, joined: set[str] | None = None, existing_channel_canvas: bool = False):
        self.joined = joined
        self.existing_channel_canvas = existing_channel_canvas
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            if name == "chat_postMessage" and self.joined is not None:
                ch = kw["channel"]
                if not ch.startswith("U") and ch not in self.joined:
                    raise SlackApiError("not_in_channel")
            if name == "conversations_canvases_create":
                if self.existing_channel_canvas:
                    raise SlackApiError("channel_canvas_already_exists")
                self.existing_channel_canvas = True
                return {"ok": True, "canvas_id": "F_CH"}
            return {"ok": True}

        return call

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


@pytest.fixture()
def repo():
    return MemoryRepository()


# --- 1. CANVAS_MODE=channel: 同じチャンネルの 2 つ目の会議も同じ Canvas を使う ---


def test_second_meeting_in_channel_reuses_channel_canvas(repo):
    a = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    b = service.register_meeting(repo, "C1", OWNER, "週次", 2, "15:00", 30)
    c = Client()
    assert service.publish_canvas(repo, c, a, TODAY, "channel") == ("F_CH", True)
    # 前は会議ごとの記録だったので、ここで作ろうとして channel_canvas_already_exists になっていた
    assert service.publish_canvas(repo, c, b, TODAY, "channel") == ("F_CH", False)
    assert c.names() == ["conversations_canvases_create", "canvases_edit"]
    assert repo.get_canvas(service.channel_canvas_key("C1"), "channel").canvas_id == "F_CH"
    # 別のチャンネルの会議は別の Canvas
    other = service.register_meeting(repo, "C2", OWNER, "定例", 0, "10:00", 30)
    c.existing_channel_canvas = False
    assert service.publish_canvas(repo, c, other, TODAY, "channel")[1] is True


def test_legacy_per_meeting_channel_record_is_reused(repo):
    a = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    b = service.register_meeting(repo, "C1", OWNER, "週次", 2, "15:00", 30)
    repo.put_canvas(CanvasRecord(a.meeting_id, "channel", "F_OLD"))  # 以前の版の記録
    c = Client(existing_channel_canvas=True)
    assert service.publish_canvas(repo, c, b, TODAY, "channel") == ("F_OLD", False)
    assert "conversations_canvases_create" not in c.names()


def test_unrecorded_channel_canvas_gives_clear_message(repo):
    a = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    with pytest.raises(service.ValidationError, match="CANVAS_MODE=standalone"):
        service.publish_canvas(repo, Client(existing_channel_canvas=True), a, TODAY, "channel")
    assert repo.get_canvas(service.channel_canvas_key("C1"), "channel") is None  # 作成中の印は残さない（もう一度押せる）
    # ハンドラーでは本人にだけ理由を出す
    h = Handlers(repo, canvas_mode="channel", now=lambda: TODAY)
    respond = Recorder()
    h.canvas(
        ack=Recorder(),
        body={"user": {"id": OWNER}},
        action={"value": a.meeting_id},
        client=Client(existing_channel_canvas=True),
        respond=respond,
    )
    assert "standalone" in respond.last["text"]


# --- 2. 保存の後のお知らせが失敗しても、エラーにしない（やり直しで二重に登録させない） ---


def register_view(name="定例") -> dict:
    return {
        "private_metadata": "C1",
        "state": {
            "values": {
                "name": {"v": {"type": "plain_text_input", "value": name}},
                "weekday": {"v": {"type": "static_select", "selected_option": {"value": "0"}}},
                "time": {"v": {"type": "plain_text_input", "value": "10:00"}},
                "minutes": {"v": {"type": "plain_text_input", "value": "30"}},
            }
        },
    }


def test_register_when_bot_not_in_channel_saves_once_and_dms_user(repo):
    h = Handlers(repo, now=lambda: TODAY)
    client = Client(joined=set())
    ack = Recorder()
    h.register_submit(ack=ack, body={"user": {"id": OWNER}}, view=register_view(), client=client)  # 例外にならない
    assert ack.last == {} and len(repo.meetings_in_channel("C1")) == 1
    dm = [kw for n, kw in client.calls if n == "chat_postMessage" and kw["channel"] == OWNER]
    assert (
        len(dm) == 1
        and "登録しました" in dm[0]["text"]
        and "not_in_channel" in dm[0]["text"]
        and "やり直す必要はありません" in dm[0]["text"]
    )


def test_add_draft_when_bot_not_in_channel_dms_user(repo):
    m = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    h = Handlers(repo, now=lambda: TODAY)
    client = Client(joined=set())
    view = {
        "private_metadata": m.meeting_id,
        "state": {"values": {k: {"v": {"type": "plain_text_input", "value": v}} for k, v in {"title": "議題", "minutes": "5"}.items()}},
    }
    h.add_submit(ack=Recorder(), body={"user": {"id": "UMEMBER"}}, view=view, client=client)
    assert len(repo.drafts(m.meeting_id, "2026-10-05")) == 1
    assert [kw["channel"] for n, kw in client.calls if n == "chat_postMessage"] == ["C1", "UMEMBER"]


def test_announce_when_joined_posts_to_channel_only(repo):
    client = Client(joined={"C1"})
    assert Handlers._announce(client, "C1", OWNER, "text", "done") is True
    assert client.names() == ["chat_postMessage"]


def test_announce_survives_dm_failure_too(repo):
    class Broken:
        def chat_postMessage(self, **kw):
            raise SlackApiError("not_in_channel")

    assert Handlers._announce(Broken(), "C1", OWNER, "text", "done") is False


def test_message_mode_canvas_reports_not_in_channel(repo):
    m = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    h = Handlers(repo, now=lambda: TODAY)
    respond = Recorder()
    h.canvas(ack=Recorder(), body={"user": {"id": OWNER}}, action={"value": m.meeting_id}, client=Client(joined=set()), respond=respond)
    assert "not_in_channel" in respond.last["text"] and "/invite" in respond.last["text"]


# --- 3. ドキュメントと実際の動きのずれを機械で見つける ---


def test_env_comment_guidance_is_the_same_everywhere_and_matches_parser():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setup = (ROOT / "docs/setup.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    guide = "コメントは値の前の行に書く。値の後ろの「 #」（空白と #）から後ろは外して読む"
    assert guide in readme and guide in setup and guide in example.splitlines()[0]
    assert "コメントを書かない" not in example  # 前は .env.example だけ「書かない」と言い、README は「書いても外す」と言っていた
    assert app.parse_env_value("abc # メモ") == "abc"
    assert app.parse_env_value('"a # b"') == "a # b"
    assert app.parse_env_value("a#b") == "a#b"
    # .env.example の値の行には値の後ろのコメントが無い（例として正しい書き方だけを見せる）
    assert all(" #" not in line for line in example.splitlines() if line and not line.startswith("#"))


def test_channel_canvas_sharing_is_documented():
    setup = (ROOT / "docs/setup.md").read_text(encoding="utf-8")
    assert "同じ Canvas を共有" in setup and f"`{service.channel_canvas_key('<チャンネル ID>')}`" in setup


def test_manifest_scopes_are_documented_and_dm_tab_enabled():
    manifest = (ROOT / "manifest.yml").read_text(encoding="utf-8")
    assert "messages_tab_enabled: true" in manifest  # お知らせの失敗を本人に DM で知らせる
    for scope in ("commands", "chat:write", "canvases:write"):
        assert f"- {scope}" in manifest


def test_setup_sh_supports_git_bash_layout():
    sh = (ROOT / "setup.sh").read_text(encoding="utf-8")
    assert "Git Bash" in sh and ".venv/Scripts" in sh and ".venv/bin/python" not in sh
