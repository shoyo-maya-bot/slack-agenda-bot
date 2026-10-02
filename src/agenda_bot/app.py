"""Slack との結合部（Bolt・Socket Mode）. 処理は service、画面は views に置き、ここは受け渡しだけ.

python -m agenda_bot        # .env の SLACK_BOT_TOKEN / SLACK_APP_TOKEN と DynamoDB の設定で起動
"""

from __future__ import annotations

import logging
import os
from datetime import date

from . import service, views
from .repository import DynamoRepository, Repository

log = logging.getLogger("agenda_bot")


def load_env(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):  # noqa: SIM115
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def build_app(repo: Repository, *, token: str, canvas_mode: str = "standalone", today=date.today, **app_kwargs):
    from slack_bolt import App

    app = App(token=token, **app_kwargs)

    def meeting_or_none(meeting_id: str):
        return repo.get_meeting(meeting_id)

    @app.command("/agenda")
    def on_agenda(ack, command, respond):
        ack()
        respond(response_type="ephemeral", blocks=views.menu(repo.meetings_in_channel(command["channel_id"])), text="アジェンダ")

    @app.action(views.A_REGISTER)
    def on_register(ack, body, client):
        ack()
        modal = views.register_modal()
        modal["private_metadata"] = body["channel"]["id"]
        client.views_open(trigger_id=body["trigger_id"], view=modal)

    @app.view(views.V_REGISTER)
    def on_register_submit(ack, body, view, client):
        v = views.values(view)
        try:
            m = service.register_meeting(
                repo, view["private_metadata"], body["user"]["id"], v["name"], int(v["weekday"]), v["time"], v["minutes"]
            )
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()
        client.chat_postMessage(channel=m.channel_id, text=f"会議「{m.name}」を登録しました。`/agenda` から議題を出せます。")

    @app.action(views.A_ADD_DRAFT)
    def on_add(ack, body, client, action):
        ack()
        m = meeting_or_none(action["value"])
        if m:
            client.views_open(trigger_id=body["trigger_id"], view=views.draft_modal(m, m.next_date(today()).isoformat()))

    @app.view(views.V_DRAFT)
    def on_add_submit(ack, body, view, client):
        m = meeting_or_none(view["private_metadata"])
        v = views.values(view)
        try:
            d = service.add_draft(
                repo, m, today(), v["title"], v.get("detail", ""), v["minutes"], v.get("presenter", ""), body["user"]["id"]
            )
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()
        client.chat_postMessage(
            channel=m.channel_id, text=f"<@{d.created_by}> さんが {d.meeting_date} の議題を出しました: *{d.title}*（{d.minutes} 分）"
        )

    @app.action(views.A_SHOW)
    def on_show(ack, action, respond):
        ack()
        m = meeting_or_none(action["value"])
        if m:
            respond(
                response_type="ephemeral",
                replace_original=False,
                blocks=views.agenda_blocks(service.agenda(repo, m, today()), ""),
                text="アジェンダ",
            )

    @app.action(views.A_REMOVE_DRAFT)
    def on_remove(ack, action, body, respond):
        ack()
        meeting_id, day, item_id = action["value"].split("|")
        m = meeting_or_none(meeting_id)
        try:
            service.remove_draft(repo, m, day, item_id, body["user"]["id"])
            respond(
                response_type="ephemeral",
                replace_original=True,
                blocks=views.agenda_blocks(service.agenda(repo, m, today()), ""),
                text="取り下げました",
            )
        except service.ValidationError as e:
            respond(response_type="ephemeral", replace_original=False, text=str(e))

    @app.action(views.A_FIXED)
    def on_fixed(ack, body, client, action):
        ack()
        m = meeting_or_none(action["value"])
        if m:
            client.views_open(trigger_id=body["trigger_id"], view=views.fixed_modal(m, repo.fixed_items(m.meeting_id)))

    @app.view(views.V_FIXED)
    def on_fixed_submit(ack, view):
        v = views.values(view)
        try:
            service.add_fixed(repo, view["private_metadata"], v["title"], v["minutes"])
        except service.ValidationError as e:
            ack(**views.errors(e))
            return
        ack()

    @app.action(views.A_CANVAS)
    def on_canvas(ack, action, client, respond):
        ack()
        m = meeting_or_none(action["value"])
        if not m:
            return
        if canvas_mode == "message":  # Canvas を使えないプラン向け: チャンネルにアジェンダを投稿する
            v = service.agenda(repo, m, today())
            client.chat_postMessage(channel=m.channel_id, blocks=views.agenda_blocks(v, ""), text=f"{m.name} のアジェンダ")
            return
        try:
            canvas_id, created = service.publish_canvas(repo, client, m, today(), canvas_mode)
        except Exception as e:  # noqa: BLE001 — Slack の権限・プランの誤りを利用者に伝える
            log.warning("canvas failed", exc_info=True)
            respond(
                response_type="ephemeral",
                replace_original=False,
                text=f"Canvas に出せませんでした: {e}（docs/setup.md の権限とプランを確認してください）",
            )
            return
        word = "作りました" if created else "更新しました"
        client.chat_postMessage(channel=m.channel_id, text=f"{m.name} のアジェンダを Canvas に{word}（ID: {canvas_id}）。")

    return app


def main() -> None:
    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    repo = DynamoRepository(
        prefix=os.environ.get("TABLE_PREFIX", "agenda_"),
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT") or None,
        region=os.environ.get("AWS_REGION", "ap-northeast-1"),
    )
    app = build_app(repo, token=os.environ["SLACK_BOT_TOKEN"], canvas_mode=os.environ.get("CANVAS_MODE", "standalone"))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
