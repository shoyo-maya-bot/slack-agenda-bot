"""ユースケース（Slack の画面や保存先に依存しない）."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from . import clock
from .model import CanvasRecord, DraftItem, FixedItem, Meeting, agenda_markdown, build_agenda, new_id
from .repository import Repository


class ValidationError(ValueError):
    """利用者が直せる入力の誤り（モーダルの項目の下に表示する）. field はブロック ID."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


MAX_TITLE = 100  # 議題・会議名の長さの上限（文字）
MAX_DETAIL = 2000  # 補足の長さの上限（文字）
PENDING = ""  # Canvas を作っている途中（canvases に ID の代わりに入れる）
PENDING_TIMEOUT = timedelta(minutes=5)  # 作成中の印がこれより古ければ、作成が途中で止まったとみなして作り直す


class Busy(ValidationError):
    """同じ操作を別の人（または連打）が実行中."""


def _title(value: str, field: str = "title", label: str = "議題") -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationError(field, f"{label}を入れてください")
    if len(value) > MAX_TITLE:
        raise ValidationError(field, f"{label}は {MAX_TITLE} 文字以内で入れてください（今は {len(value)} 文字）")
    return value


def _minutes(value: str | int, field: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(field, "分を数字で入れてください") from None
    if not 1 <= n <= 240:
        raise ValidationError(field, "1〜240 分で入れてください")
    return n


def register_meeting(repo: Repository, channel_id: str, owner: str, name: str, weekday: int, time: str, minutes: str | int) -> Meeting:
    name = _title(name, "name", "会議名")
    if not (len(time) == 5 and time[2] == ":" and time.replace(":", "").isdigit() and int(time[:2]) < 24 and int(time[3:]) < 60):
        raise ValidationError("time", "時刻は 10:00 の形で入れてください")
    m = Meeting(new_id(), channel_id, name, int(weekday), time, owner, _minutes(minutes, "minutes"))
    repo.put_meeting(m)
    return m


def add_fixed(
    repo: Repository, meeting_id: str, title: str, minutes: str | int, presenter: str = "", order: int | None = None
) -> FixedItem:
    title = _title(title)
    existing = repo.fixed_items(meeting_id)
    f = FixedItem(
        meeting_id,
        new_id(),
        title,
        order if order is not None else (max((x.order for x in existing), default=0) + 10),
        _minutes(minutes, "minutes"),
        presenter,
    )
    repo.put_fixed(f)
    return f


def remove_fixed(repo: Repository, meeting_id: str, item_id: str) -> FixedItem:
    f = next((x for x in repo.fixed_items(meeting_id) if x.item_id == item_id), None)
    if f is None:
        raise ValidationError("item", "その固定の議題は見つかりません（削除済みかもしれません）")
    repo.delete_fixed(meeting_id, item_id)
    return f


def add_draft(
    repo: Repository, meeting: Meeting, today: date | datetime, title: str, detail: str, minutes: str | int, presenter: str, user: str
) -> DraftItem:
    """議題を次の開催日に出す. today に日時（clock.now()）を渡すと、今日の会議が終わった後なら翌週に出す."""
    title = _title(title)
    detail = (detail or "").strip()
    if len(detail) > MAX_DETAIL:
        raise ValidationError("detail", f"補足は {MAX_DETAIL} 文字以内で入れてください（今は {len(detail)} 文字）")
    d = DraftItem(
        meeting.meeting_id,
        meeting.next_date(today).isoformat(),
        new_id(),
        title,
        detail,
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


def carry_over(repo: Repository, meeting: Meeting, day: date) -> list[DraftItem]:
    """開催日 day より前の開催日に残っている議題を片付ける. → day に繰り越した議題.

    過去の開催日ごとに（古い順）、その日のアジェンダを今の固定の議題で組み直し、
    - 会議時間に収まった議題は accepted（扱い済み）にしてその日に残す
    - 収まらなかった議題は status=deferred にして次の開催日に移す（移した先のアジェンダでは先頭側に並ぶ）
    移す先に書いてから元を消すので、途中で止まっても、もう一度実行すれば同じ結果になる。
    """
    by_date: dict[str, list[DraftItem]] = {}
    for d in repo.drafts_before(meeting.meeting_id, day.isoformat()):
        by_date.setdefault(d.meeting_date, []).append(d)
    if not by_date:
        return []
    fixed = repo.fixed_items(meeting.meeting_id)
    dates = sorted(by_date)
    carried: list[DraftItem] = []
    for i, past in enumerate(dates):
        rows, _, deferred = build_agenda(meeting, date.fromisoformat(past), fixed, by_date[past] + carried)
        done = {r["item_id"] for r in rows if "item_id" in r}
        for d in by_date[past] + carried:
            if d.item_id in done:
                repo.put_draft(replace(d, status="accepted"))
        target = dates[i + 1] if i + 1 < len(dates) else day.isoformat()
        carried = []
        for d in deferred:
            new = replace(d, meeting_date=target, status="deferred", carried_from=d.carried_from or d.meeting_date)
            repo.put_draft(new)
            repo.delete_draft(meeting.meeting_id, d.meeting_date, d.item_id)
            carried.append(new)
    return carried


def agenda(repo: Repository, meeting: Meeting, today: date | datetime) -> AgendaView:
    """次の開催日のアジェンダ. 先に過去の開催日の未消化の議題を繰り越す（carry_over）."""
    day = meeting.next_date(today)
    carry_over(repo, meeting, day)
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


def _stale(rec: CanvasRecord) -> bool:
    try:
        at = datetime.fromisoformat(rec.updated_at)
    except ValueError:
        return True
    return clock.now() - at > PENDING_TIMEOUT


def publish_canvas(
    repo: Repository, client: CanvasApi, meeting: Meeting, today: date | datetime, mode: str = "standalone"
) -> tuple[str, bool]:
    """アジェンダを Canvas に書く. 同じ開催日の Canvas があれば中身を置き換える. → (Canvas の ID, 新しく作ったか).

    mode=standalone: 開催日ごとに Canvas を作り、チャンネルに編集権限を付ける（有料プラン向け）
    mode=channel: チャンネルの Canvas（1 チャンネルに 1 つ）を毎回置き換える（無料プランでも使える）

    重複を防ぐため、作る前に「作成中」の印を条件付きで書き（先に書けた 1 人だけが作る）、作った直後に ID を保存してから権限を付ける。
    権限の付与に失敗しても ID は残るので、もう一度押すと同じ Canvas を更新し、権限を付け直す。
    """
    view = agenda(repo, meeting, today)
    key_date = view.meeting_date.isoformat() if mode == "standalone" else "channel"
    content = {"type": "markdown", "markdown": view.markdown}
    rec = repo.get_canvas(meeting.meeting_id, key_date)
    if rec is None or rec.canvas_id == PENDING:
        if rec is not None and not _stale(rec):
            raise Busy("canvas", "Canvas を作成中です。少し待ってから、もう一度押してください")
        claim = CanvasRecord(meeting.meeting_id, key_date, PENDING)
        if not repo.put_canvas_if(claim, rec):
            rec = repo.get_canvas(meeting.meeting_id, key_date)
            if rec is None or rec.canvas_id == PENDING:
                raise Busy("canvas", "Canvas を作成中です。少し待ってから、もう一度押してください")
        else:
            try:
                if mode == "channel":
                    res = client.conversations_canvases_create(channel_id=meeting.channel_id, document_content=content)
                else:
                    res = client.canvases_create(title=f"{meeting.name} {view.meeting_date.isoformat()}", document_content=content)
            except Exception:
                repo.delete_canvas(meeting.meeting_id, key_date)  # 作れなかったので印を消し、もう一度押せるようにする
                raise
            canvas_id = res["canvas_id"]
            repo.put_canvas(CanvasRecord(meeting.meeting_id, key_date, canvas_id))  # 権限の付与より先に保存する
            if mode != "channel":
                client.canvases_access_set(canvas_id=canvas_id, access_level="write", channel_ids=[meeting.channel_id])
            return canvas_id, True
    client.canvases_edit(canvas_id=rec.canvas_id, changes=[{"operation": "replace", "document_content": content}])
    if mode != "channel":  # 前回の権限の付与が失敗していても、ここで付け直す（同じ設定なので何度実行してもよい）
        client.canvases_access_set(canvas_id=rec.canvas_id, access_level="write", channel_ids=[meeting.channel_id])
    repo.put_canvas(CanvasRecord(meeting.meeting_id, key_date, rec.canvas_id))
    return rec.canvas_id, False
