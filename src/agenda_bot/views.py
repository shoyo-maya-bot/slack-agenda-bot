"""Slack の画面（Block Kit）. 純粋な関数で組み立てる（テストしやすくするため）."""

from __future__ import annotations

from typing import Any

from .model import WEEKDAYS, Meeting, is_user_id
from .service import AgendaView

A_ADD_DRAFT = "agenda_add_draft"
A_SHOW = "agenda_show"
A_FIXED = "agenda_fixed"
A_CANVAS = "agenda_canvas"
A_REGISTER = "agenda_register"
A_REMOVE_DRAFT = "agenda_remove_draft"
A_DELETE_FIXED = "agenda_delete_fixed"
A_FINALIZE = "agenda_finalize"
A_CANCEL = "agenda_cancel"
A_EDIT_MEETING = "agenda_edit_meeting"
A_DELETE_MEETING = "agenda_delete_meeting"
V_REGISTER = "agenda_view_register"
V_DRAFT = "agenda_view_draft"
V_FIXED = "agenda_view_fixed"
V_EDIT_MEETING = "agenda_view_edit_meeting"

# Slack の上限（Block Kit）. 超えると invalid_blocks で投稿・表示そのものが失敗する
MAX_MESSAGE_BLOCKS = 50  # メッセージ（chat.postMessage・respond）のブロック数
MAX_MODAL_BLOCKS = 100  # モーダルのブロック数
MAX_TEXT = 3000  # section・context の mrkdwn の文字数


def clip(text: str, limit: int = MAX_TEXT) -> str:
    """文字数の上限に収める（超えたら末尾を「…」にする）."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def join_within(items: list[str], prefix: str = "", sep: str = "、", limit: int = MAX_TEXT) -> str:
    """prefix の後ろに items を sep でつなぐ. limit 文字に収まらない分は「ほか N 件」にまとめる."""
    out = prefix
    for i, item in enumerate(items):
        piece = (sep if i else "") + item
        after = len(items) - i - 1
        reserve = len(f"{sep}ほか {after} 件") if after else 0  # 次が入らなかったときの「ほか N 件」の分を残す
        if len(out) + len(piece) + reserve > limit:
            return clip(out + (sep if i else "") + f"ほか {len(items) - i} 件", limit)
        out += piece
    return out


def esc(text: str) -> str:
    """利用者の文字列を Slack の mrkdwn（メッセージ・セクション）に入れる前に無害化する.

    & < > をエスケープし、<!channel>・<!here>・<@U…>・<#C…> などの特殊な書式（一斉メンションなど）にならないようにする。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _btn(text: str, action: str, value: str, style: str = "") -> dict[str, Any]:
    b: dict[str, Any] = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action, "value": value}
    if style:
        b["style"] = style
    return b


def _confirm(button: dict[str, Any], title: str, text: str) -> dict[str, Any]:
    """押した後に確認のダイアログを出す（削除など元に戻せない操作）."""
    button["confirm"] = {
        "title": {"type": "plain_text", "text": clip(title, 100)},
        "text": {"type": "plain_text", "text": clip(text, 300)},
        "confirm": {"type": "plain_text", "text": "削除する"},
        "deny": {"type": "plain_text", "text": "やめる"},
        "style": "danger",
    }
    return button


def _input(block_id: str, label: str, element: dict[str, Any], optional: bool = False, hint: str = "") -> dict[str, Any]:
    b: dict[str, Any] = {
        "type": "input",
        "block_id": block_id,
        "label": {"type": "plain_text", "text": label},
        "element": element,
        "optional": optional,
    }
    if hint:
        b["hint"] = {"type": "plain_text", "text": hint}
    return b


def _text(action: str, initial: str = "", multiline: bool = False, placeholder: str = "") -> dict[str, Any]:
    e: dict[str, Any] = {"type": "plain_text_input", "action_id": action, "multiline": multiline}
    if initial:
        e["initial_value"] = initial
    if placeholder:
        e["placeholder"] = {"type": "plain_text", "text": placeholder}
    return e


def menu(meetings: list[Meeting]) -> list[dict[str, Any]]:
    """/agenda の応答（本人にだけ見える）."""
    if not meetings:
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": "このチャンネルには会議が登録されていません。"}},
            {"type": "actions", "elements": [_btn("会議を登録する", A_REGISTER, "new", "primary")]},
        ]
    blocks: list[dict[str, Any]] = []
    room = (MAX_MESSAGE_BLOCKS - 2) // 3  # 1 会議 3 ブロック. 末尾の「別の会議を登録する」と「ほか N 件」の分を残す
    shown = meetings if len(meetings) * 3 + 1 <= MAX_MESSAGE_BLOCKS else meetings[:room]
    for m in shown:
        blocks += [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{esc(m.name)}*（毎週{WEEKDAYS[m.weekday]}曜 {m.time}〜、{m.default_minutes} 分）"},
            },
            {
                "type": "actions",
                "elements": [
                    _btn("議題を出す", A_ADD_DRAFT, m.meeting_id, "primary"),
                    _btn("次回のアジェンダ", A_SHOW, m.meeting_id),
                    _btn("固定の議題", A_FIXED, m.meeting_id),
                    _btn("Canvas に出す", A_CANVAS, m.meeting_id),
                    _btn("会議の変更", A_EDIT_MEETING, m.meeting_id),
                    _confirm(
                        _btn("会議の削除", A_DELETE_MEETING, m.meeting_id, "danger"),
                        "会議の削除",
                        "この会議と、その固定の議題・出ている議題を消します。元に戻せません。",
                    ),
                ],
            },
            {"type": "divider"},
        ]
    if len(shown) < len(meetings):
        names = [esc(m.name) for m in meetings[len(shown) :]]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": join_within(names, "表示しきれない会議: ")}]})
    blocks.append({"type": "actions", "elements": [_btn("別の会議を登録する", A_REGISTER, "new")]})
    return blocks


def register_modal() -> dict[str, Any]:
    days = [{"text": {"type": "plain_text", "text": f"{w}曜"}, "value": str(i)} for i, w in enumerate(WEEKDAYS)]
    return {
        "type": "modal",
        "callback_id": V_REGISTER,
        "title": {"type": "plain_text", "text": "会議の登録"},
        "submit": {"type": "plain_text", "text": "登録"},
        "blocks": [
            _input("name", "会議名", _text("v", placeholder="例: 開発チーム定例")),
            _input("weekday", "曜日", {"type": "static_select", "action_id": "v", "options": days, "initial_option": days[0]}),
            _input("time", "開始時刻", _text("v", "10:00")),
            _input("minutes", "会議時間（分）", _text("v", "60"), hint="議題の合計がこれを超えると、後から出た議題は「次回へ」に回ります"),
        ],
    }


def edit_meeting_modal(meeting: Meeting) -> dict[str, Any]:
    """会議の変更（曜日・時刻・長さ）. 今の値を初期値に入れる."""
    days = [{"text": {"type": "plain_text", "text": f"{w}曜"}, "value": str(i)} for i, w in enumerate(WEEKDAYS)]
    return {
        "type": "modal",
        "callback_id": V_EDIT_MEETING,
        "private_metadata": meeting.meeting_id,
        "title": {"type": "plain_text", "text": "会議の変更"},
        "submit": {"type": "plain_text", "text": "変更"},
        "blocks": [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{esc(meeting.name)}*"}]},
            _input(
                "weekday",
                "曜日",
                {"type": "static_select", "action_id": "v", "options": days, "initial_option": days[meeting.weekday]},
                hint="次の開催日とその後に出ている議題は、新しい曜日の開催日へ移ります",
            ),
            _input("time", "開始時刻", _text("v", meeting.time)),
            _input("minutes", "会議時間（分）", _text("v", str(meeting.default_minutes))),
        ],
    }


def draft_modal(meeting: Meeting, meeting_date: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": V_DRAFT,
        "private_metadata": meeting.meeting_id,
        "title": {"type": "plain_text", "text": "議題を出す"},
        "submit": {"type": "plain_text", "text": "出す"},
        "blocks": [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{esc(meeting.name)}（{meeting_date}）に向けた議題"}]},
            _input("title", "議題", _text("v", placeholder="例: リリース日の判断")),
            _input("detail", "補足（決めたいこと・資料のリンク）", _text("v", multiline=True), optional=True),
            _input("minutes", "時間（分）", _text("v", "10")),
            _input("presenter", "説明する人", {"type": "users_select", "action_id": "v"}, optional=True, hint="空なら自分"),
        ],
    }


def fixed_modal(meeting: Meeting, items: list, can_manage: bool = True) -> dict[str, Any]:
    """固定の議題の一覧（削除ボタン付き）と追加の入力欄. can_manage=False なら一覧だけ（追加・削除できない）."""
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{esc(meeting.name)} の毎回の議題*"}}]
    ordered = sorted(items, key=lambda x: x.order)
    # 見出し・追加の入力欄 2 つ・結果の表示（delete_fixed が先頭に足す）・「ほか N 件」の分を残す
    room = MAX_MODAL_BLOCKS - 5
    shown, hidden = (ordered, []) if len(ordered) <= room + 1 else (ordered[:room], ordered[room:])
    for f in shown:
        sec: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": f"• {esc(f.title)}（{f.minutes} 分）"}}
        if can_manage:
            sec["accessory"] = _btn("削除", A_DELETE_FIXED, f"{meeting.meeting_id}|{f.item_id}", "danger")
        blocks.append(sec)
    if hidden:
        text = join_within([esc(f.title) for f in hidden], f"表示しきれない固定の議題（{len(hidden)} 件）: ")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": text}]})
    if not items:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "（まだありません）"}})
    modal: dict[str, Any] = {
        "type": "modal",
        "callback_id": V_FIXED,
        "private_metadata": meeting.meeting_id,
        "title": {"type": "plain_text", "text": "固定の議題"},
        "close": {"type": "plain_text", "text": "閉じる"},
        "blocks": blocks,
    }
    if can_manage:
        modal["submit"] = {"type": "plain_text", "text": "追加"}
        blocks += [
            _input("title", "追加する議題", _text("v", placeholder="例: 先週の振り返り")),
            _input("minutes", "時間（分）", _text("v", "5")),
        ]
    else:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "固定の議題を変えられるのは、設定された担当者だけです。"}]}
        )
    return modal


def settle_blocks(view: AgendaView, can_manage: bool) -> list[dict[str, Any]]:
    """確定も中止もされていない過去の開催日（いちばん古い日）について、担当者に「確定」「中止」を選んでもらう.

    確定: アジェンダのとおりに行った（収まった議題は扱い済み、収まらなかった議題は次の開催日へ）
    中止: 会議は開かれなかった（議題をすべて次の開催日へ）
    """
    if not view.unsettled:
        return []
    day = view.unsettled[0]
    more = f"（ほかに {len(view.unsettled) - 1} 回分）" if len(view.unsettled) > 1 else ""
    text = f"*{day} の会議* の結果がまだ決まっていません{more}。下のアジェンダは、収まった議題を扱ったものとして見込みで並べています。"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    value = f"{view.meeting.meeting_id}|{day}"
    if can_manage:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _btn("確定（アジェンダのとおり行った）", A_FINALIZE, value, "primary"),
                    _btn("中止（議題をすべて次回へ）", A_CANCEL, value),
                ],
            }
        )
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "確定・中止は、設定された担当者が選びます。"}]})
    return blocks


def agenda_blocks(
    view: AgendaView, user: str, can_manage: bool | None = None, max_blocks: int = MAX_MESSAGE_BLOCKS
) -> list[dict[str, Any]]:
    """アジェンダの表示.

    can_manage が None（チャンネルへの投稿など）なら確定・中止の案内を出さない。True ならボタンも、False なら案内だけ出す。
    ブロック数は max_blocks（既定はメッセージの上限 50）に収める. 議題が多すぎるときは、収まらない分を「ほか N 件」にまとめる。
    """
    m = view.meeting
    head: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": clip(f"{m.name} {view.meeting_date.isoformat()}（{WEEKDAYS[view.meeting_date.weekday()]}）", 150),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"合計 {view.total} 分 ／ 会議時間 {m.default_minutes} 分"}]},
    ]
    tail: list[dict[str, Any]] = []
    if view.deferred:
        text = join_within([esc(d.title) for d in view.deferred], "次回へ: ")
        tail.append({"type": "context", "elements": [{"type": "mrkdwn", "text": text}]})
    if not view.rows:
        tail.append({"type": "section", "text": {"type": "mrkdwn", "text": "まだ議題がありません。"}})
    if can_manage is not None:
        tail += settle_blocks(view, can_manage)
    room = max_blocks - len(head) - len(tail)
    rows = view.rows if len(view.rows) <= room else view.rows[: max(room - 1, 0)]
    body: list[dict[str, Any]] = []
    for n, r in enumerate(rows, start=1):
        who = f" <@{r['presenter']}>" if is_user_id(r["presenter"]) else (f" {esc(r['presenter'])}" if r["presenter"] else "")
        mark = "（繰越）" if r["kind"] == "繰越" else ""
        sec: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": clip(f"{n}. *{esc(r['title'])}*{mark}（{r['minutes']} 分）{who}")},
        }
        if "item_id" in r:
            sec["accessory"] = _btn("取り下げ", A_REMOVE_DRAFT, f"{m.meeting_id}|{view.meeting_date.isoformat()}|{r['item_id']}")
        body.append(sec)
    if len(rows) < len(view.rows):
        rest = view.rows[len(rows) :]
        text = join_within(
            [f"{esc(r['title'])}（{r['minutes']} 分）" for r in rest],
            f"ほか {len(rest)} 件（1 つのメッセージに出せる数を超えたため省略）: ",
        )
        body.append({"type": "context", "elements": [{"type": "mrkdwn", "text": text}]})
    return head + body + tail


def errors(e) -> dict[str, Any]:
    """モーダルの入力エラー（response_action: errors）."""
    return {"response_action": "errors", "errors": {e.field: str(e)}}


def values(view: dict[str, Any]) -> dict[str, str]:
    """モーダルの送信値を {block_id: 値} にする."""
    out: dict[str, str] = {}
    for block_id, actions in view["state"]["values"].items():
        v = actions.get("v", {})
        if v.get("type") == "static_select":
            out[block_id] = (v.get("selected_option") or {}).get("value", "")
        elif v.get("type") == "users_select":
            out[block_id] = v.get("selected_user") or ""
        else:
            out[block_id] = v.get("value") or ""
    return out
