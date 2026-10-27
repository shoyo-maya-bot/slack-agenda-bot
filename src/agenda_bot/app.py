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
from .model import USER_ID as _USER_ID
from .model import Meeting
from .repository import DynamoRepository, MissingTablesError, Repository

log = logging.getLogger("agenda_bot")

CANVAS_MODES = ("message", "channel", "standalone")
DEFAULT_CANVAS_MODE = "message"  # docs/setup.md と .env.example の既定に合わせる（無料プランでも動く）


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
    """AGENDA_ADMINS（カンマか空白で区切った Slack のユーザー ID）. 空なら空集合.

    空集合のとき、固定の議題・Canvas への出力・確定と中止は、その会議の登録者だけができる（会議の登録は誰でもできる）。
    会議の変更・削除は、空でも空でなくても、会議の登録者と AGENDA_ADMINS の担当者ができる（service.can_manage / can_edit_meeting）。

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


_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{2,}$")


def parse_channel(value: str | None) -> str:
    """ERROR_CHANNEL（失敗を知らせる管理チャンネルの ID. 空なら知らせない）. ID の形でなければ ConfigError."""
    value = (value or "").strip()
    if value and not _CHANNEL_ID.match(value):
        raise ConfigError(f"ERROR_CHANNEL「{value}」はチャンネル ID（C… / G…）ではありません（チャンネル名ではなく ID を書く）")
    return value


def today_in(tz: str | None = None) -> Callable[[], date]:
    """設定したタイムゾーン（tz → 環境変数 TIMEZONE → Asia/Tokyo）の「今日」を返す関数."""
    return lambda: clock.today(tz)


def now_in(tz: str | None = None) -> Callable[[], datetime]:
    """設定したタイムゾーンの「今」を返す関数（会議が終わったかどうかの判断に使う）."""
    return lambda: clock.now(tz)


NOT_FOUND = "その会議は見つかりません（削除されたか、別のワークスペースのものかもしれません）。`/agenda` からやり直してください。"
NOT_ALLOWED = (
    "この操作（固定の議題・Canvas への出力・会議の確定と中止）ができるのは、担当者（AGENDA_ADMINS。空なら会議の登録者）だけです。"
    "登録者が異動・退職したときは、担当者（AGENDA_ADMINS）が「会議の変更」で登録者を引き継げます（docs/setup.md）。"
)
NOT_ALLOWED_EDIT = "会議の変更（登録者の引き継ぎを含む）・削除ができるのは、会議の登録者と担当者（AGENDA_ADMINS）だけです。"
NOT_ALLOWED_REGISTER = "会議を登録できるのは、設定された担当者（AGENDA_ADMINS）だけです。"


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

    def can_manage(self, user: str, meeting: Meeting | None = None) -> bool:
        """固定の議題・Canvas への出力・過去の会議の確定と中止ができるか（meeting を渡す）、会議を登録できるか（meeting なし）.

        AGENDA_ADMINS があればその担当者だけ。空なら、その会議の登録者だけ（会議の登録は誰でもできる）。
        """
        return service.can_manage(meeting, user, self.admins)

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
            self._say(respond, NOT_ALLOWED_REGISTER)
            return
        modal = views.register_modal()
        modal["private_metadata"] = body["channel"]["id"]
        client.views_open(trigger_id=body["trigger_id"], view=modal)

    def register_submit(self, ack, body, view, client):
        if not self.can_manage(body["user"]["id"]):
            ack(**views.errors(service.ValidationError("name", NOT_ALLOWED_REGISTER)))
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

    def show(self, ack, action, body, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        respond(
            response_type="ephemeral",
            replace_original=False,
            blocks=views.agenda_blocks(service.agenda(self.repo, m, self.now()), "", self.can_manage(body["user"]["id"], m)),
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
            service.remove_draft(self.repo, m, parts[1], parts[2], body["user"]["id"], self.admins)
        except service.ValidationError as e:
            self._say(respond, str(e))
            return
        respond(
            response_type="ephemeral",
            replace_original=True,
            blocks=views.agenda_blocks(service.agenda(self.repo, m, self.now()), "", self.can_manage(body["user"]["id"], m)),
            text="取り下げました",
        )

    # --- 過去の会議の確定・中止（担当者だけ） ---

    def _settle(self, ack, action, body, respond, client, op: str) -> None:
        ack()
        meeting_id, _, meeting_date = action["value"].partition("|")
        m = self.repo.get_meeting(meeting_id)
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        if not self.can_manage(body["user"]["id"], m):
            self._say(respond, NOT_ALLOWED)
            return
        try:
            if op == "finalize":
                accepted, moved = service.finalize(self.repo, m, meeting_date, self.now())
                note = f"{meeting_date} の会議を確定しました（扱い済み {len(accepted)} 件・次回へ繰り越し {len(moved)} 件）。"
            else:
                moved = service.cancel(self.repo, m, meeting_date, self.now())
                note = f"{meeting_date} の会議を中止にしました（議題 {len(moved)} 件を次回へ繰り越し）。"
        except service.ValidationError as e:
            self._say(respond, str(e))
            return
        respond(
            response_type="ephemeral",
            replace_original=True,
            blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": views.clip(note)}]}]
            + views.agenda_blocks(service.agenda(self.repo, m, self.now()), "", True, max_blocks=views.MAX_MESSAGE_BLOCKS - 1),
            text=note,
        )
        client.chat_postMessage(channel=m.channel_id, text=f"<@{body['user']['id']}> さん: {views.esc(m.name)} の {note}")

    def finalize(self, ack, action, body, respond, client):
        self._settle(ack, action, body, respond, client, "finalize")

    def cancel(self, ack, action, body, respond, client):
        self._settle(ack, action, body, respond, client, "cancel")

    # --- 固定の議題 ---

    def fixed(self, ack, body, client, action, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        modal = views.fixed_modal(m, self.repo.fixed_items(m.meeting_id), self.can_manage(body["user"]["id"], m))
        client.views_open(trigger_id=body["trigger_id"], view=modal)

    def fixed_submit(self, ack, body, view, client=None):
        m = self.repo.get_meeting(view["private_metadata"])
        if m is None:
            ack(**views.errors(service.ValidationError("title", NOT_FOUND)))
            return
        if not self.can_manage(body["user"]["id"], m):
            ack(**views.errors(service.ValidationError("title", NOT_ALLOWED)))
            return
        v = views.values(view)
        try:
            service.add_fixed(self.repo, m.meeting_id, v.get("title", ""), v.get("minutes", ""))
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()
        # 固定の議題だけで会議時間が埋まる・出ている議題が収まらなくなった: 追加した本人に知らせる（追加は拒否しない）
        warnings = [w for w in (service.fill_warning(self.repo, m), service.unfit_warning(self.repo, m)) if w]
        if warnings and client is not None:
            text = "\n".join(f":warning: {views.esc(m.name)}: {w}" for w in warnings)
            client.chat_postEphemeral(channel=m.channel_id, user=body["user"]["id"], text=views.clip(text))

    def delete_fixed(self, ack, body, action, client):
        """固定の議題のモーダルの「削除」. 消したら同じモーダルを描き直す（結果はモーダルの上に出す）."""
        ack()
        meeting_id, _, item_id = action["value"].partition("|")
        m = self.repo.get_meeting(meeting_id)
        if m is None:
            note = NOT_FOUND
        elif not self.can_manage(body["user"]["id"], m):
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
            modal = views.fixed_modal(m, self.repo.fixed_items(m.meeting_id), self.can_manage(body["user"]["id"], m))
        modal["blocks"].insert(0, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
        client.views_update(view_id=body["view"]["id"], hash=body["view"].get("hash"), view=modal)

    # --- 会議の変更・削除（登録者と AGENDA_ADMINS の担当者） ---

    def edit_meeting(self, ack, body, client, action, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        if not service.can_edit_meeting(m, body["user"]["id"], self.admins):
            self._say(respond, NOT_ALLOWED_EDIT)
            return
        client.views_open(trigger_id=body["trigger_id"], view=views.edit_meeting_modal(m))

    def edit_meeting_submit(self, ack, body, view, client):
        m = self.repo.get_meeting(view["private_metadata"])
        if m is None:
            ack(**views.errors(service.ValidationError("minutes", NOT_FOUND)))
            return
        v = views.values(view)
        user = body["user"]["id"]
        new_owner = v.get("owner", "").strip()
        transfer = bool(new_owner) and new_owner != m.owner
        # 登録者の引き継ぎだけ（曜日・時刻・長さはそのまま）なら予定は書き換えない（確定していない過去の会議があっても引き継げる）
        entered = (v.get("weekday", ""), v.get("time", "").strip(), v.get("minutes", "").strip())
        same = entered == (str(m.weekday), m.time, str(m.default_minutes))
        moved = []
        try:
            if transfer and same:
                if not service.can_edit_meeting(m, user, self.admins):
                    raise service.ValidationError("owner", NOT_ALLOWED_EDIT)
                new = m
            else:
                new, moved = service.update_meeting(
                    self.repo, m, user, v.get("weekday", ""), v.get("time", ""), v.get("minutes", ""), self.now(), self.admins
                )
            if transfer:
                new = service.transfer_meeting(self.repo, new, user, new_owner, self.admins)
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()
        if transfer and same:
            note = f"<@{user}> さんが会議「{views.esc(new.name)}」の登録者を <@{new.owner}> さんに引き継ぎました。"
        else:
            note = (
                f"<@{user}> さんが会議「{views.esc(new.name)}」を変更しました"
                f"（毎週{views.WEEKDAYS[new.weekday]}曜 {new.time}〜、{new.default_minutes} 分）。"
            )
            if transfer:
                note += f" 登録者を <@{new.owner}> さんに引き継ぎました。"
        if moved:
            note += f" 議題 {len(moved)} 件の開催日を付け替えました。"
        for warning in (service.fill_warning(self.repo, new), service.unfit_warning(self.repo, new)):
            if warning:  # 会議を短くして収まらなくなった議題は、出した人にメンションで知らせる（変更は拒否しない）
                note += f"\n:warning: {warning}"
        client.chat_postMessage(channel=new.channel_id, text=views.clip(note))

    def delete_meeting(self, ack, body, client, action, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        try:
            service.delete_meeting(self.repo, m, body["user"]["id"], self.admins)
        except service.ValidationError:
            self._say(respond, NOT_ALLOWED_EDIT)
            return
        respond(
            response_type="ephemeral",
            replace_original=True,
            # GSI は結果整合なので、消した直後の一覧に残っていることがある. 消した会議は除いて出す
            blocks=views.menu([x for x in self.repo.meetings_in_channel(m.channel_id) if x.meeting_id != m.meeting_id]),
            text="会議を削除しました",
        )
        client.chat_postMessage(channel=m.channel_id, text=f"<@{body['user']['id']}> さんが会議「{views.esc(m.name)}」を削除しました。")

    # --- Canvas ---

    def canvas(self, ack, body, action, client, respond):
        ack()
        m = self.repo.get_meeting(action["value"])
        if m is None:
            self._say(respond, NOT_FOUND)
            return
        if not self.can_manage(body["user"]["id"], m):
            self._say(respond, NOT_ALLOWED)
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


ERROR_TEXT = "処理中にエラーが起きました。少し待ってから、もう一度試してください（続くときはボットの管理者に連絡してください）。"


class ErrorReporter:
    """Bolt の app.error に登録する、ハンドラーの想定外の例外の受け口.

    - ログに例外を残す（スタックトレース付き）
    - 押した本人には、本人にだけ見えるメッセージで失敗を知らせる（respond が使えるとき）
    - 管理チャンネル（ERROR_CHANNEL）が設定されていれば、そこにも短く知らせる（例外の種類と操作の名前だけ。入力の中身は送らない）
    知らせる処理そのものが失敗しても、ログに残して握りつぶす（エラーの処理でさらに落ちないように）。
    """

    def __init__(self, error_channel: str = ""):
        self.error_channel = error_channel

    @staticmethod
    def _what(body: dict | None) -> str:
        body = body or {}
        if body.get("command"):
            return str(body["command"])
        actions = body.get("actions") or []
        if actions:
            return f"action:{actions[0].get('action_id', '?')}"
        if body.get("view"):
            return f"view:{body['view'].get('callback_id', '?')}"
        return str(body.get("type", "?"))

    def __call__(self, error: Exception, body: dict | None = None, respond=None, client=None, logger=None) -> None:
        what = self._what(body)
        user = ((body or {}).get("user") or {}).get("id") or (body or {}).get("user_id", "")
        (logger or log).error("ハンドラーで想定外のエラー（%s）: %s", what, type(error).__name__, exc_info=error)
        if respond is not None:
            try:
                respond(response_type="ephemeral", replace_original=False, text=ERROR_TEXT)
            except Exception:  # noqa: BLE001
                log.warning("エラーを本人に知らせられませんでした", exc_info=True)
        if self.error_channel and client is not None:
            try:
                who = f"<@{user}> さんの" if user else ""
                client.chat_postMessage(
                    channel=self.error_channel,
                    text=f":warning: アジェンダ係: {who}操作 `{views.esc(what)}` が失敗しました（{type(error).__name__}）。ログを確認してください。",
                )
            except Exception:  # noqa: BLE001
                log.warning("管理チャンネル %s にエラーを知らせられませんでした", self.error_channel, exc_info=True)


def build_app(
    repo: Repository,
    *,
    token: str,
    canvas_mode: str = DEFAULT_CANVAS_MODE,
    admins: Iterable[str] = (),
    today: Callable[[], date | datetime] | None = None,
    tz: str | None = None,
    error_channel: str = "",
    **app_kwargs,
):
    """Bolt の App を作り、Handlers とエラーの受け口（ErrorReporter）を登録する.

    today は「今」を返す関数（テストで時刻を止める. 日付だけでもよい）。error_channel は失敗を知らせる管理チャンネルの ID（空なら知らせない）。
    """
    from slack_bolt import App

    h = Handlers(repo, canvas_mode=canvas_mode, admins=admins, now=today, tz=tz)
    app = App(token=token, **app_kwargs)
    app.error(ErrorReporter(error_channel))
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
    app.action(views.A_FINALIZE)(h.finalize)
    app.action(views.A_CANCEL)(h.cancel)
    app.action(views.A_EDIT_MEETING)(h.edit_meeting)
    app.view(views.V_EDIT_MEETING)(h.edit_meeting_submit)
    app.action(views.A_DELETE_MEETING)(h.delete_meeting)
    return app


LOCAL_DUMMY_CREDENTIAL = "local"  # DynamoDB Local は認証情報を検証しないが、boto3 は値を必要とする


def use_local_dynamodb() -> bool:
    """DYNAMODB_ENDPOINT（DynamoDB Local）があり、AWS の認証情報が無ければ、ダミーの値を使う. → ダミーを入れたか.

    .env に AWS のアクセスキーを書かずに済むようにする（実際のキーを .env に置かない）。AWS では DYNAMODB_ENDPOINT を空にし、
    IAM ロールかプロファイルを使う。
    """
    if not os.environ.get("DYNAMODB_ENDPOINT"):
        return False
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return False
    os.environ["AWS_ACCESS_KEY_ID"] = LOCAL_DUMMY_CREDENTIAL
    os.environ["AWS_SECRET_ACCESS_KEY"] = LOCAL_DUMMY_CREDENTIAL
    return True


def check_storage(repo: DynamoRepository) -> None:
    """起動時にテーブルがあるか確かめる. 無ければ（作り忘れ・TABLE_PREFIX の誤り）、最初の /agenda で落ちる前にここで止める.

    DynamoDB に届かない（DynamoDB Local が起動していない・認証情報が無い）ときも、理由を出して止める。
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        warnings = repo.check_tables()
    except MissingTablesError as e:
        sys.exit(f"設定の誤り: {e}")
    except (BotoCoreError, ClientError) as e:
        sys.exit(f"DynamoDB に接続できません: {e}（DYNAMODB_ENDPOINT・AWS_REGION・AWS の認証情報を確かめてください）")
    for w in warnings:
        log.warning(w)


def main() -> None:
    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        tz = clock.get_tz()  # 名前の誤りは起動時に止める
        bot_token = require_env("SLACK_BOT_TOKEN")
        app_token = require_env("SLACK_APP_TOKEN")
        mode = canvas_mode_from_env()
        admins = parse_admins(os.environ.get("AGENDA_ADMINS"))
        error_channel = parse_channel(os.environ.get("ERROR_CHANNEL"))
    except (ConfigError, ValueError) as e:
        sys.exit(f"設定の誤り: {e}")
    log.info(
        "timezone: %s / canvas mode: %s / admins: %s / error channel: %s",
        tz.key,
        mode,
        len(admins) or "なし（確定・中止などは各会議の登録者）",
        "あり" if error_channel else "なし",
    )
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    use_local_dynamodb()
    repo = DynamoRepository(
        prefix=os.environ.get("TABLE_PREFIX", "agenda_"),
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT") or None,
        region=os.environ.get("AWS_REGION", "ap-northeast-1"),
    )
    check_storage(repo)
    app = build_app(repo, token=bot_token, canvas_mode=mode, admins=admins, tz=tz.key, error_channel=error_channel)
    SocketModeHandler(app, app_token).start()
