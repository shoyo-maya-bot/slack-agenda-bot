"""Slack との結合部（Bolt・Socket Mode）. 処理は service、画面は views に置き、ここは受け渡しだけ.

python -m agenda_bot        # .env の SLACK_BOT_TOKEN / SLACK_APP_TOKEN・TIMEZONE と DynamoDB の設定で起動

ハンドラーは Handlers のメソッドにまとめ、Bolt が無くても偽の ack・respond・client でテストできるようにしている。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path

from . import clock, service, views
from .repository import DynamoRepository, Repository

log = logging.getLogger("agenda_bot")

CANVAS_MODES = ("message", "channel", "standalone")
DEFAULT_CANVAS_MODE = "message"  # docs/setup.md と .env.example の既定に合わせる（無料プランでも動く）
_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")


class ConfigError(Exception):
    """設定（環境変数・.env）の誤り. 起動時に分かるメッセージを出して止める."""


def parse_env_value(raw: str) -> str:
    """.env の値を読む. devtools-common の load_dotenv と同じ規則に、行末のコメントを外す規則を足したもの.

    - 引用符（" か '）で囲まれた値は、その中身をそのまま使う（中の # はコメントにしない）
    - 引用符で囲まれていない値は、空白の後ろの # から行末までをコメントとして外す（a#b の # は値の一部）
    """
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end > 0:
            return value[1:end]
    m = re.search(r"\s#", value)
    if m:
        value = value[: m.start()]
    return value.strip()


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """KEY=VALUE 形式の .env を読み、まだ無い環境変数だけを設定する. 読んだ値を返す."""
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.is_file():
        return loaded
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        loaded[key] = parse_env_value(value)
        os.environ.setdefault(key, loaded[key])
    return loaded


def require_env(name: str) -> str:
    """必須の設定. 空や .env.example のまま（<your-…>）なら ConfigError."""
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("<your-"):
        raise ConfigError(f"環境変数 {name} が設定されていません（.env.example を見て .env に書いてください）")
    return value


def canvas_mode_from_env() -> str:
    mode = os.environ.get("CANVAS_MODE", "").strip() or DEFAULT_CANVAS_MODE
    if mode not in CANVAS_MODES:
        raise ConfigError(f"CANVAS_MODE「{mode}」は使えません（{' / '.join(CANVAS_MODES)} のどれか）")
    return mode


def parse_admins(value: str | None) -> frozenset[str]:
    """AGENDA_ADMINS（カンマか空白で区切った Slack のユーザー ID）. 空なら空集合（誰でも操作できる）.

    ユーザー ID の形でないものは警告を出して使わない。指定はあるのに 1 つも使えなければ ConfigError
    （誰も操作できなくなる・誰でも操作できてしまう、のどちらにもしない）。
    """
    if not value or not value.strip():
        return frozenset()
    ids, bad = set(), []
    for token in re.split(r"[\s,]+", value.strip()):
        if not token:
            continue
        if _USER_ID.match(token):
            ids.add(token)
        else:
            bad.append(token)
    for token in bad:
        log.warning("AGENDA_ADMINS の「%s」は Slack のユーザー ID（U… / W…）ではないため使いません", token)
    if not ids:
        raise ConfigError("AGENDA_ADMINS に使えるユーザー ID がありません（例: U0123ABCD,U0456EFGH）")
    return frozenset(ids)


def today_in(tz: str | None = None) -> Callable[[], date]:
    """設定したタイムゾーン（tz → 環境変数 TIMEZONE → Asia/Tokyo）の「今日」を返す関数."""
    return lambda: clock.today(tz)


def now_in(tz: str | None = None) -> Callable[[], datetime]:
    """設定したタイムゾーンの「今」を返す関数（会議が終わったかどうかの判断に使う）."""
    return lambda: clock.now(tz)


NOT_FOUND = "その会議は見つかりません（削除されたか、別のワークスペースのものかもしれません）。`/agenda` からやり直してください。"
NOT_ALLOWED = "この操作（会議の登録・固定の議題・Canvas への出力）ができるのは、設定された担当者だけです。"


class Handlers:
    """Bolt のリスナー. 引数の名前（ack・body・view・action・client・respond）で Bolt が値を渡す."""

    def __init__(
        self,
        repo: Repository,
        *,
        canvas_mode: str = DEFAULT_CANVAS_MODE,
        admins: Iterable[str] = (),
        now: Callable[[], date | datetime] | None = None,
        tz: str | None = None,
    ):
        if canvas_mode not in CANVAS_MODES:
            raise ConfigError(f"CANVAS_MODE「{canvas_mode}」は使えません（{' / '.join(CANVAS_MODES)} のどれか）")
        self.repo = repo
        self.canvas_mode = canvas_mode
        self.admins = frozenset(admins)
        self.now = now or now_in(tz)

    # --- 共通 ---

    def can_manage(self, user: str) -> bool:
        """会議の登録・固定の議題・Canvas への出力ができるか. AGENDA_ADMINS が空なら誰でもできる."""
        return not self.admins or user in self.admins

    @staticmethod
    def _say(respond, text: str) -> None:
        respond(response_type="ephemeral", replace_original=False, text=text)

    # --- /agenda ---

    def agenda_command(self, ack, command, respond):
        ack()
        respond(response_type="ephemeral", blocks=views.menu(self.repo.meetings_in_channel(command["channel_id"])), text="アジェンダ")

    # --- 会議の登録 ---

    def register(self, ack, body, client, respond):
        ack()
        if not self.can_manage(body["user"]["id"]):
            self._say(respond, NOT_ALLOWED)
            return
        modal = views.register_modal()
        modal["private_metadata"] = body["channel"]["id"]
        client.views_open(trigger_id=body["trigger_id"], view=modal)

    def register_submit(self, ack, body, view, client):
        if not self.can_manage(body["user"]["id"]):
            ack(**views.errors(service.ValidationError("name", NOT_ALLOWED)))
            return
        v = views.values(view)
        try:
            m = service.register_meeting(
                self.repo, view["private_metadata"], body["user"]["id"], v["name"], int(v["weekday"] or 0), v["time"], v["minutes"]
            )
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()
        client.chat_postMessage(channel=m.channel_id, text=f"会議「{views.esc(m.name)}」を登録しました。`/agenda` から議題を出せます。")

    # --- 議題 ---

    def add(self, ack, body, client, action, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        client.views_open(trigger_id=body["trigger_id"], view=views.draft_modal(m, m.next_date(self.now()).isoformat()))

    def add_submit(self, ack, body, view, client):
        m = self.repo.get_meeting(view["private_metadata"])
        if m is None:
            ack(**views.errors(service.ValidationError("title", NOT_FOUND)))
            return
        v = views.values(view)
        try:
            d = service.add_draft(
                self.repo,
                m,
                self.now(),
                v.get("title", ""),
                v.get("detail", ""),
                v.get("minutes", ""),
                v.get("presenter", ""),
                body["user"]["id"],
            )
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()
        client.chat_postMessage(
            channel=m.channel_id,
            text=f"<@{d.created_by}> さんが {d.meeting_date} の議題を出しました: *{views.esc(d.title)}*（{d.minutes} 分）",
        )

    def show(self, ack, action, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        respond(
            response_type="ephemeral",
            replace_original=False,
            blocks=views.agenda_blocks(service.agenda(self.repo, m, self.now()), ""),
            text="アジェンダ",
        )

    def remove(self, ack, action, body, respond):
        ack()
        parts = action["value"].split("|")
        m = self.repo.get_meeting(parts[0]) if len(parts) == 3 else None
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        try:
            service.remove_draft(self.repo, m, parts[1], parts[2], body["user"]["id"])
        except service.ValidationError as e:
            self._say(respond, str(e))
            return
        respond(
            response_type="ephemeral",
            replace_original=True,
            blocks=views.agenda_blocks(service.agenda(self.repo, m, self.now()), ""),
            text="取り下げました",
        )

    # --- 固定の議題 ---

    def fixed(self, ack, body, client, action, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        modal = views.fixed_modal(m, self.repo.fixed_items(m.meeting_id), self.can_manage(body["user"]["id"]))
        client.views_open(trigger_id=body["trigger_id"], view=modal)

    def fixed_submit(self, ack, body, view):
        if not self.can_manage(body["user"]["id"]):
            ack(**views.errors(service.ValidationError("title", NOT_ALLOWED)))
            return
        if self.repo.get_meeting(view["private_metadata"]) is None:
            ack(**views.errors(service.ValidationError("title", NOT_FOUND)))
            return
        v = views.values(view)
        try:
            service.add_fixed(self.repo, view["private_metadata"], v.get("title", ""), v.get("minutes", ""))
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()

    def delete_fixed(self, ack, body, action, client):
        """固定の議題のモーダルの「削除」. 消したら同じモーダルを描き直す（結果はモーダルの上に出す）."""
        ack()
        meeting_id, _, item_id = action["value"].partition("|")
        m = self.repo.get_meeting(meeting_id)
        if m is None:
            note = NOT_FOUND
        elif not self.can_manage(body["user"]["id"]):
            note = NOT_ALLOWED
        else:
            try:
                f = service.remove_fixed(self.repo, meeting_id, item_id)
                note = f"「{views.esc(f.title)}」を削除しました。"
            except service.ValidationError as e:
                note = str(e)
        if m is None:
            modal = {
                "type": "modal",
                "callback_id": views.V_FIXED,
                "title": {"type": "plain_text", "text": "固定の議題"},
                "close": {"type": "plain_text", "text": "閉じる"},
                "blocks": [],
            }
        else:
            modal = views.fixed_modal(m, self.repo.fixed_items(m.meeting_id), self.can_manage(body["user"]["id"]))
        modal["blocks"].insert(0, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
        client.views_update(view_id=body["view"]["id"], hash=body["view"].get("hash"), view=modal)

    # --- Canvas ---

    def canvas(self, ack, body, action, client, respond):
        ack()
        if not self.can_manage(body["user"]["id"]):
            self._say(respond, NOT_ALLOWED)
            return
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        if self.canvas_mode == "message":  # Canvas を使えないプラン向け: チャンネルにアジェンダを投稿する
            v = service.agenda(self.repo, m, self.now())
            client.chat_postMessage(channel=m.channel_id, blocks=views.agenda_blocks(v, ""), text=f"{views.esc(m.name)} のアジェンダ")
            return
        try:
            canvas_id, created = service.publish_canvas(self.repo, client, m, self.now(), self.canvas_mode)
        except service.ValidationError as e:  # 作成中（連打・同時の押下）など
            self._say(respond, str(e))
            return
        except Exception as e:  # noqa: BLE001 — Slack の権限・プランの誤りを利用者に伝える
            log.warning("canvas failed", exc_info=True)
            self._say(respond, f"Canvas に出せませんでした: {e}（docs/setup.md の権限とプランを確認してください）")
            return
        word = "作りました" if created else "更新しました"
        client.chat_postMessage(channel=m.channel_id, text=f"{views.esc(m.name)} のアジェンダを Canvas に{word}（ID: {canvas_id}）。")


def build_app(
    repo: Repository,
    *,
    token: str,
    canvas_mode: str = DEFAULT_CANVAS_MODE,
    admins: Iterable[str] = (),
    today: Callable[[], date | datetime] | None = None,
    tz: str | None = None,
    **app_kwargs,
):
    """Bolt の App を作り、Handlers を登録する. today は「今」を返す関数（テストで時刻を止める. 日付だけでもよい）."""
    from slack_bolt import App

    h = Handlers(repo, canvas_mode=canvas_mode, admins=admins, now=today, tz=tz)
    app = App(token=token, **app_kwargs)
    app.command("/agenda")(h.agenda_command)
    app.action(views.A_REGISTER)(h.register)
    app.view(views.V_REGISTER)(h.register_submit)
    app.action(views.A_ADD_DRAFT)(h.add)
    app.view(views.V_DRAFT)(h.add_submit)
    app.action(views.A_SHOW)(h.show)
    app.action(views.A_REMOVE_DRAFT)(h.remove)
    app.action(views.A_FIXED)(h.fixed)
    app.view(views.V_FIXED)(h.fixed_submit)
    app.action(views.A_DELETE_FIXED)(h.delete_fixed)
    app.action(views.A_CANVAS)(h.canvas)
    return app


def main() -> None:
    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        tz = clock.get_tz()  # 名前の誤りは起動時に止める
        bot_token = require_env("SLACK_BOT_TOKEN")
        app_token = require_env("SLACK_APP_TOKEN")
        mode = canvas_mode_from_env()
        admins = parse_admins(os.environ.get("AGENDA_ADMINS"))
    except (ConfigError, ValueError) as e:
        sys.exit(f"設定の誤り: {e}")
    log.info("timezone: %s / canvas mode: %s / admins: %s", tz.key, mode, len(admins) or "全員")
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    repo = DynamoRepository(
        prefix=os.environ.get("TABLE_PREFIX", "agenda_"),
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT") or None,
        region=os.environ.get("AWS_REGION", "ap-northeast-1"),
    )
    app = build_app(repo, token=bot_token, canvas_mode=mode, admins=admins, tz=tz.key)
    SocketModeHandler(app, app_token).start()
