"""ユースケース（Slack の画面や保存先に依存しない）."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from .model import CanvasRecord, DraftItem, FixedItem, Meeting, agenda_markdown, build_agenda, new_id
from .repository import Repository


class ValidationError(ValueError):
    """利用者が直せる入力の誤り（モーダルの項目の下に表示する）. field はブロック ID."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _minutes(value: str | int, field: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(field, "分を数字で入れてください") from None
    if not 1 <= n <= 240:
        raise ValidationError(field, "1〜240 分で入れてください")
    return n


def register_meeting(repo: Repository, channel_id: str, owner: str, name: str, weekday: int, time: str, minutes: str | int) -> Meeting:
    if not name.strip():
        raise ValidationError("name", "会議名を入れてください")
    if not (len(time) == 5 and time[2] == ":" and time.replace(":", "").isdigit() and int(time[:2]) < 24 and int(time[3:]) < 60):
        raise ValidationError("time", "時刻は 10:00 の形で入れてください")
    m = Meeting(new_id(), channel_id, name.strip(), int(weekday), time, owner, _minutes(minutes, "minutes"))
    repo.put_meeting(m)
    return m


def add_fixed(
    repo: Repository, meeting_id: str, title: str, minutes: str | int, presenter: str = "", order: int | None = None
) -> FixedItem:
    if not title.strip():
        raise ValidationError("title", "議題を入れてください")
    existing = repo.fixed_items(meeting_id)
    f = FixedItem(
        meeting_id,
        new_id(),
        title.strip(),
        order if order is not None else (max((x.order for x in existing), default=0) + 10),
        _minutes(minutes, "minutes"),
        presenter,
    )
    repo.put_fixed(f)
    return f


def add_draft(
    repo: Repository, meeting: Meeting, today: date, title: str, detail: str, minutes: str | int, presenter: str, user: str
) -> DraftItem:
    if not title.strip():
        raise ValidationError("title", "議題を入れてください")
    d = DraftItem(
        meeting.meeting_id,
        meeting.next_date(today).isoformat(),
        new_id(),
        title.strip(),
        detail.strip(),
        _minutes(minutes, "minutes"),
        presenter or user,
        user,
    )
    repo.put_draft(d)
    return d


def remove_draft(repo: Repository, meeting: Meeting, meeting_date: str, item_id: str, user: str) -> None:
    """取り下げられるのは、出した本人か会議の担当者だけ."""
    d = next((x for x in repo.drafts(meeting.meeting_id, meeting_date) if x.item_id == item_id), None)
    if d is None:
        raise ValidationError("item", "その議題は見つかりません（取り下げ済みかもしれません）")
    if user not in (d.created_by, meeting.owner):
        raise ValidationError("item", "議題を取り下げられるのは、出した人と会議の担当者だけです")
    repo.delete_draft(meeting.meeting_id, meeting_date, item_id)


@dataclass
class AgendaView:
    meeting: Meeting
    meeting_date: date
    rows: list[dict]
    total: int
    deferred: list[DraftItem]
    markdown: str


def agenda(repo: Repository, meeting: Meeting, today: date) -> AgendaView:
    day = meeting.next_date(today)
    rows, total, deferred = build_agenda(
        meeting, day, repo.fixed_items(meeting.meeting_id), repo.drafts(meeting.meeting_id, day.isoformat())
    )
    return AgendaView(meeting, day, rows, total, deferred, agenda_markdown(meeting, day, rows, total, deferred))


class CanvasApi(Protocol):
    """slack_sdk の WebClient のうち、使うメソッドだけ."""

    def canvases_create(self, **kw: Any) -> Any: ...
    def canvases_edit(self, **kw: Any) -> Any: ...
    def canvases_access_set(self, **kw: Any) -> Any: ...
    def conversations_canvases_create(self, **kw: Any) -> Any: ...


def publish_canvas(repo: Repository, client: CanvasApi, meeting: Meeting, today: date, mode: str = "standalone") -> tuple[str, bool]:
    """アジェンダを Canvas に書く. 同じ開催日の Canvas があれば中身を置き換える. → (Canvas の ID, 新しく作ったか).

    mode=standalone: 開催日ごとに Canvas を作り、チャンネルに編集権限を付ける（有料プラン向け）
    mode=channel: チャンネルの Canvas（1 チャンネルに 1 つ）を毎回置き換える（無料プランでも使える）
    """
    view = agenda(repo, meeting, today)
    key_date = view.meeting_date.isoformat() if mode == "standalone" else "channel"
    content = {"type": "markdown", "markdown": view.markdown}
    rec = repo.get_canvas(meeting.meeting_id, key_date)
    if rec:
        client.canvases_edit(canvas_id=rec.canvas_id, changes=[{"operation": "replace", "document_content": content}])
        repo.put_canvas(CanvasRecord(meeting.meeting_id, key_date, rec.canvas_id))
        return rec.canvas_id, False
    if mode == "channel":
        res = client.conversations_canvases_create(channel_id=meeting.channel_id, document_content=content)
    else:
        res = client.canvases_create(title=f"{meeting.name} {view.meeting_date.isoformat()}", document_content=content)
        client.canvases_access_set(canvas_id=res["canvas_id"], access_level="write", channel_ids=[meeting.channel_id])
    canvas_id = res["canvas_id"]
    repo.put_canvas(CanvasRecord(meeting.meeting_id, key_date, canvas_id))
    return canvas_id, True
