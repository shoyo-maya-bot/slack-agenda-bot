"""ユースケース（Slack の画面や保存先に依存しない）."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from . import clock
from .model import CanvasRecord, DraftItem, FixedItem, Meeting, accepted_expiry, agenda_markdown, build_agenda, new_id
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
SAVE_BACKOFF = 0.5  # Canvas の ID の保存をやり直すまでの秒（回数に比例して延ばす）

log = logging.getLogger("agenda_bot")


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
    """取り下げられるのは、出した本人か会議の担当者だけ.

    アジェンダに「繰越」として見えている議題は、確定するまで元の開催日に保存されているので、そちらも探す。
    """
    d = next((x for x in repo.drafts(meeting.meeting_id, meeting_date) if x.item_id == item_id), None)
    if d is None:
        d = next((x for x in repo.drafts_before(meeting.meeting_id, meeting_date, meeting.settled_through) if x.item_id == item_id), None)
    if d is None:
        raise ValidationError("item", "その議題は見つかりません（取り下げ済みかもしれません）")
    if user not in (d.created_by, meeting.owner):
        raise ValidationError("item", "議題を取り下げられるのは、出した人と会議の担当者だけです")
    repo.delete_draft(meeting.meeting_id, d.meeting_date, item_id)


@dataclass
class AgendaView:
    meeting: Meeting
    meeting_date: date
    rows: list[dict]
    total: int
    deferred: list[DraftItem]
    markdown: str
    # 確定も中止もされていない過去の開催日（古い順）. 担当者が「確定」か「中止」を選ぶまで、保存した状態は変えない
    unsettled: list[str] = field(default_factory=list)


def _project(repo: Repository, meeting: Meeting, day: date) -> tuple[list[DraftItem], list[str]]:
    """開催日 day のアジェンダに繰り越して見せる議題と、確定も中止もされていない過去の開催日. 保存した状態は変えない（読み取りだけ）.

    過去の開催日に残っている議題（accepted 以外）を古い順に、その日のアジェンダを今の固定の議題で組み直して、
    会議時間に収まった議題は「扱われた」とみなして見せず、収まらなかった議題を次の開催日へ繰り越したものとして見せる。
    実際に状態を変えるのは、担当者の「確定」（finalize）と「中止」（cancel）だけ。
    """
    by_date: dict[str, list[DraftItem]] = {}
    for d in repo.drafts_before(meeting.meeting_id, day.isoformat(), meeting.settled_through):
        by_date.setdefault(d.meeting_date, []).append(d)
    if not by_date:
        return [], []
    fixed = repo.fixed_items(meeting.meeting_id)
    dates = sorted(by_date)
    carried: list[DraftItem] = []
    for past in dates:
        _, _, deferred = build_agenda(meeting, date.fromisoformat(past), fixed, by_date[past] + carried)
        carried = [replace(d, status="deferred", carried_from=d.carried_from or d.meeting_date) for d in deferred]
    return carried, dates


def _settle_target(repo: Repository, meeting: Meeting, meeting_date: str, now: date | datetime) -> tuple[list[DraftItem], date]:
    """確定・中止できる開催日か確かめる. → (その日に残っている議題, 繰り越す先の開催日).

    繰り越す先は、議題の残っている次の過去の開催日か、無ければ次の開催日（議題の無い週は飛ばす. 以前の繰り越しと同じ）。
    """
    day = meeting.next_date(now)
    try:
        target = date.fromisoformat(meeting_date)
    except ValueError:
        raise ValidationError("meeting_date", f"開催日の形が違います: {meeting_date}") from None
    if target >= day:
        raise ValidationError("meeting_date", f"{meeting_date} の会議はまだ終わっていません（確定・中止は会議の後にできます）")
    pending = repo.drafts_before(meeting.meeting_id, day.isoformat(), meeting.settled_through)
    older = sorted({d.meeting_date for d in pending if d.meeting_date < meeting_date})
    if older:
        raise ValidationError("meeting_date", f"先に {older[0]} の会議を確定か中止にしてください（古い順に決めます）")
    items = [d for d in pending if d.meeting_date == meeting_date]
    if not items:
        raise ValidationError("meeting_date", f"{meeting_date} の会議は確定か中止が済んでいます")
    later = sorted({d.meeting_date for d in pending if d.meeting_date > meeting_date})
    return items, (date.fromisoformat(later[0]) if later else day)


def _move_to_next(repo: Repository, meeting: Meeting, items: list[DraftItem], next_day: date) -> list[DraftItem]:
    """議題を次の開催日へ繰り越す. 移す先に書いてから元を消すので、途中で止まっても、もう一度実行すれば同じ結果になる."""
    moved = []
    for d in items:
        new = replace(d, meeting_date=next_day.isoformat(), status="deferred", carried_from=d.carried_from or d.meeting_date)
        repo.put_draft(new)
        repo.delete_draft(meeting.meeting_id, d.meeting_date, d.item_id)
        moved.append(new)
    return moved


def finalize(repo: Repository, meeting: Meeting, meeting_date: str, now: date | datetime) -> tuple[list[DraftItem], list[DraftItem]]:
    """「確定」: 開催日 meeting_date の会議を、アジェンダのとおりに行ったとする. → (扱い済みにした議題, 次の開催日へ繰り越した議題).

    会議時間に収まった議題は accepted（扱い済み）にしてその日に残し、収まらなかった議題は次の開催日へ繰り越す。
    確定も中止もしていない古い開催日があれば、先にそちらを決めるよう ValidationError にする。
    """
    items, target = _settle_target(repo, meeting, meeting_date, now)
    day = date.fromisoformat(meeting_date)
    rows, _, deferred = build_agenda(meeting, day, repo.fixed_items(meeting.meeting_id), items)
    done_ids = {r["item_id"] for r in rows if "item_id" in r}
    accepted = []
    for d in items:
        if d.item_id in done_ids:
            a = replace(d, status="accepted", expires_at=accepted_expiry(d.meeting_date))  # 一定期間の後に TTL で消える
            repo.put_draft(a)
            accepted.append(a)
    moved = _move_to_next(repo, meeting, deferred, target)
    repo.mark_settled(meeting.meeting_id, meeting_date)  # これ以前の議題は、次から読まない
    return accepted, moved


def cancel(repo: Repository, meeting: Meeting, meeting_date: str, now: date | datetime) -> list[DraftItem]:
    """「中止」: 開催日 meeting_date の会議は開かれなかったとし、残っている議題をすべて次の開催日へ繰り越す. → 繰り越した議題."""
    items, target = _settle_target(repo, meeting, meeting_date, now)
    moved = _move_to_next(repo, meeting, items, target)
    repo.mark_settled(meeting.meeting_id, meeting_date)
    return moved


def agenda(repo: Repository, meeting: Meeting, today: date | datetime) -> AgendaView:
    """次の開催日のアジェンダ（読み取りだけで、保存した状態は変えない）.

    確定も中止もされていない過去の開催日があれば、収まらなかった議題を繰り越したものとして並べ（_project）、
    その開催日を unsettled に入れる（画面で担当者に「確定」「中止」を選んでもらう）。
    """
    day = meeting.next_date(today)
    carried, unsettled = _project(repo, meeting, day)
    rows, total, deferred = build_agenda(
        meeting, day, repo.fixed_items(meeting.meeting_id), repo.drafts(meeting.meeting_id, day.isoformat()) + carried
    )
    return AgendaView(meeting, day, rows, total, deferred, agenda_markdown(meeting, day, rows, total, deferred), unsettled)


class CanvasApi(Protocol):
    """slack_sdk の WebClient のうち、使うメソッドだけ."""

    def canvases_create(self, **kw: Any) -> Any: ...
    def canvases_edit(self, **kw: Any) -> Any: ...
    def canvases_access_set(self, **kw: Any) -> Any: ...
    def canvases_delete(self, **kw: Any) -> Any: ...
    def conversations_canvases_create(self, **kw: Any) -> Any: ...


def _stale(rec: CanvasRecord) -> bool:
    try:
        at = datetime.fromisoformat(rec.updated_at)
    except ValueError:
        return True
    return clock.now() - at > PENDING_TIMEOUT


SAVE_ATTEMPTS = 3  # 作った Canvas の ID を保存する回数（一時的な保存先のエラーに備える）


class CanvasSaveError(RuntimeError):
    """作った Canvas の ID を保存できなかった. 重複を避けるため、作った Canvas は消してある（消せなかったときは ID を知らせる）."""


def _save_created(repo: Repository, client: CanvasApi, rec: CanvasRecord, mode: str) -> None:
    """作った直後の Canvas の ID を保存する. 保存できないまま「作成中」の印が古くなると、次に押したときにもう 1 つ作ってしまうので:

    1. 何度か保存し直す
    2. それでも保存できなければ、作った Canvas を消してから印を消す（次に押せば 1 つだけ作り直す）
    3. 消せなければ印を残したまま、Canvas の ID を例外のメッセージに入れて知らせる（手で消すか記録してもらう）
    """
    last: Exception | None = None
    for attempt in range(SAVE_ATTEMPTS):
        try:
            repo.put_canvas(rec)
            return
        except Exception as e:  # noqa: BLE001 — 保存先（DynamoDB など）の一時的な失敗
            last = e
            log.warning("Canvas の ID の保存に失敗しました（%d/%d 回目）", attempt + 1, SAVE_ATTEMPTS, exc_info=True)
            time.sleep(SAVE_BACKOFF * (attempt + 1))
    try:
        if mode == "channel":
            raise RuntimeError("チャンネルの Canvas は API で消せません")
        client.canvases_delete(canvas_id=rec.canvas_id)
    except Exception as e:  # noqa: BLE001
        log.error("Canvas %s の ID を保存できず、消すこともできませんでした", rec.canvas_id, exc_info=True)
        raise CanvasSaveError(
            f"Canvas（ID: {rec.canvas_id}）は作りましたが、ID を保存できませんでした（{last}）。"
            f"重複して作らないよう、この Canvas を手で消してから、もう一度押してください（消せなかった理由: {e}）"
        ) from last
    try:
        repo.delete_canvas(rec.meeting_id, rec.meeting_date)
    except Exception:  # noqa: BLE001 — 印は 5 分で古いとみなされ、作り直せる
        log.warning("作成中の印を消せませんでした", exc_info=True)
    raise CanvasSaveError(
        f"Canvas の ID を保存できなかったため、作った Canvas を消しました（{last}）。少し待ってから、もう一度押してください"
    ) from last


def _slack_error(e: Exception) -> str:
    """slack_sdk の SlackApiError の error コード（canvas_not_found など）. 取れなければ空."""
    res = getattr(e, "response", None)
    try:
        return str(res["error"]) if res is not None else ""
    except (KeyError, TypeError):
        return ""


def _create_canvas(repo: Repository, client: CanvasApi, meeting: Meeting, view: AgendaView, key_date: str, mode: str) -> str:
    """「作成中」の印を書けた人だけが呼ぶ. Canvas を作り、ID を保存してから権限を付ける. → Canvas の ID."""
    content = {"type": "markdown", "markdown": view.markdown}
    try:
        if mode == "channel":
            res = client.conversations_canvases_create(channel_id=meeting.channel_id, document_content=content)
        else:
            res = client.canvases_create(title=f"{meeting.name} {view.meeting_date.isoformat()}", document_content=content)
    except Exception:
        repo.delete_canvas(meeting.meeting_id, key_date)  # 作れなかったので印を消し、もう一度押せるようにする
        raise
    canvas_id = res["canvas_id"]
    _save_created(repo, client, CanvasRecord(meeting.meeting_id, key_date, canvas_id), mode)  # 権限の付与より先に保存する
    if mode != "channel":
        client.canvases_access_set(canvas_id=canvas_id, access_level="write", channel_ids=[meeting.channel_id])
    return canvas_id


def publish_canvas(
    repo: Repository, client: CanvasApi, meeting: Meeting, today: date | datetime, mode: str = "standalone"
) -> tuple[str, bool]:
    """アジェンダを Canvas に書く. 同じ開催日の Canvas があれば中身を置き換える. → (Canvas の ID, 新しく作ったか).

    mode=standalone: 開催日ごとに Canvas を作り、チャンネルに編集権限を付ける（有料プラン向け）
    mode=channel: チャンネルの Canvas（1 チャンネルに 1 つ）を毎回置き換える（無料プランでも使える）

    重複を防ぐため、作る前に「作成中」の印を条件付きで書き（先に書けた 1 人だけが作る）、作った直後に ID を保存してから権限を付ける。
    権限の付与に失敗しても ID は残るので、もう一度押すと同じ Canvas を更新し、権限を付け直す。
    記録した Canvas が Slack 側で消されていた（canvas_not_found）ときは、記録を「作成中」の印に置き換えてから作り直す。
    """
    view = agenda(repo, meeting, today)
    key_date = view.meeting_date.isoformat() if mode == "standalone" else "channel"
    content = {"type": "markdown", "markdown": view.markdown}
    rec = repo.get_canvas(meeting.meeting_id, key_date)
    if rec is None or rec.canvas_id == PENDING:
        if rec is not None and not _stale(rec):
            raise Busy("canvas", "Canvas を作成中です。少し待ってから、もう一度押してください")
        claim = CanvasRecord(meeting.meeting_id, key_date, PENDING)
        if repo.put_canvas_if(claim, rec):
            return _create_canvas(repo, client, meeting, view, key_date, mode), True
        rec = repo.get_canvas(meeting.meeting_id, key_date)
        if rec is None or rec.canvas_id == PENDING:
            raise Busy("canvas", "Canvas を作成中です。少し待ってから、もう一度押してください")
    try:
        client.canvases_edit(canvas_id=rec.canvas_id, changes=[{"operation": "replace", "document_content": content}])
        if mode != "channel":  # 前回の権限の付与が失敗していても、ここで付け直す（同じ設定なので何度実行してもよい）
            client.canvases_access_set(canvas_id=rec.canvas_id, access_level="write", channel_ids=[meeting.channel_id])
    except Exception as e:
        if _slack_error(e) != "canvas_not_found":
            raise
        log.warning("記録した Canvas %s が見つかりません（Slack で削除された）. 記録を消して作り直します", rec.canvas_id)
        # 記録が読んだときのままなら「作成中」の印に置き換える（同時に押した人のうち 1 人だけが作り直す）
        if not repo.put_canvas_if(CanvasRecord(meeting.meeting_id, key_date, PENDING), rec):
            raise Busy("canvas", "Canvas を作り直しています。少し待ってから、もう一度押してください") from e
        return _create_canvas(repo, client, meeting, view, key_date, mode), True
    repo.put_canvas(CanvasRecord(meeting.meeting_id, key_date, rec.canvas_id))
    return rec.canvas_id, False
