"""ユースケース（Slack の画面や保存先に依存しない）."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from . import clock
from .model import (
    CanvasRecord,
    DraftItem,
    FixedItem,
    Meeting,
    accepted_expiry,
    agenda_markdown,
    build_agenda,
    is_user_id,
    new_id,
)
from .repository import Repository


class ValidationError(ValueError):
    """利用者が直せる入力の誤り（モーダルの項目の下に表示する）. field はブロック ID."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


MAX_TITLE = 100  # 議題・会議名の長さの上限（文字）
MAX_DETAIL = 2000  # 補足の長さの上限（文字）
MIN_MINUTES, MAX_MINUTES = 1, 240  # 議題・固定の議題・会議の時間（分）の範囲
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
    if not MIN_MINUTES <= n <= MAX_MINUTES:
        raise ValidationError(field, f"{MIN_MINUTES}〜{MAX_MINUTES} 分で入れてください")
    return n


def _time(time: str) -> str:
    time = (time or "").strip()
    if not (len(time) == 5 and time[2] == ":" and time.replace(":", "").isdigit() and int(time[:2]) < 24 and int(time[3:]) < 60):
        raise ValidationError("time", "時刻は 10:00 の形で入れてください")
    return time


def _weekday(value: str | int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError("weekday", "曜日を選んでください") from None
    if not 0 <= n <= 6:
        raise ValidationError("weekday", "曜日を選んでください")
    return n


def register_meeting(repo: Repository, channel_id: str, owner: str, name: str, weekday: int, time: str, minutes: str | int) -> Meeting:
    name = _title(name, "name", "会議名")
    m = Meeting(new_id(), channel_id, name, _weekday(weekday), _time(time), owner, _minutes(minutes, "minutes"))
    repo.put_meeting(m)
    return m


# --- 権限 ---


def can_manage(meeting: Meeting | None, user: str, admins: Iterable[str] = ()) -> bool:
    """確定・中止・Canvas への出力・固定の議題の変更ができるか.

    AGENDA_ADMINS（admins）があれば、その担当者だけ。空なら、その会議の登録者（owner）だけ
    （会議を指定しない操作 = 会議の登録は、空なら誰でもできる）。
    """
    admins = frozenset(admins)
    if admins:
        return user in admins
    return meeting is None or user == meeting.owner


def can_edit_meeting(meeting: Meeting, user: str, admins: Iterable[str] = ()) -> bool:
    """会議の変更（曜日・時刻・長さ）・登録者の引き継ぎ・削除ができるか. 会議の登録者と AGENDA_ADMINS の担当者."""
    return user == meeting.owner or user in frozenset(admins)


# --- 会議時間と議題の時間 ---


def fixed_minutes(repo: Repository, meeting_id: str) -> int:
    """固定の議題の合計（分）."""
    return sum(f.minutes for f in repo.fixed_items(meeting_id))


def available_minutes(repo: Repository, meeting: Meeting) -> int:
    """議題に使える時間（会議の長さ − 固定の議題の合計. 0 未満は 0）."""
    return max(meeting.default_minutes - fixed_minutes(repo, meeting.meeting_id), 0)


def fill_warning(repo: Repository, meeting: Meeting) -> str:
    """固定の議題だけで会議時間が埋まる（または超える）なら、その警告文. 埋まらなければ空."""
    fixed = fixed_minutes(repo, meeting.meeting_id)
    if fixed < meeting.default_minutes:
        return ""
    over = "を超えています" if fixed > meeting.default_minutes else "と同じです"
    return (
        f"固定の議題の合計（{fixed} 分）が会議時間（{meeting.default_minutes} 分）{over}。"
        "このままでは議題を出せません（固定の議題を減らすか、会議時間を延ばしてください）。"
    )


def unfit_drafts(repo: Repository, meeting: Meeting) -> list[DraftItem]:
    """会議時間に収まらない（固定の議題を除いた使える時間より長い）まま残っている議題. 出した順.

    議題を出すときは長さを確かめているが、後から会議を短くしたり固定の議題を足したりすると収まらなくなり、
    確定のたびに次の開催日へ繰り越され続ける（拒否はせず、警告とアジェンダの表示で知らせる）。
    確定・中止を済ませた開催日より後の議題（今後の開催日のものも含む）を見る。
    """
    room = available_minutes(repo, meeting)
    pending = repo.drafts_before(meeting.meeting_id, FAR_FUTURE, meeting.settled_through)
    return sorted((d for d in pending if d.minutes > room), key=lambda d: (d.carried_from or d.meeting_date, d.created_at))


MAX_UNFIT_LISTED = 10  # 警告に名前を並べる議題の数（多ければ「ほか N 件」）


def unfit_warning(repo: Repository, meeting: Meeting) -> str:
    """会議時間に収まらない議題があれば、その警告文（出した人へのメンション付き）. 無ければ空."""
    items = unfit_drafts(repo, meeting)
    if not items:
        return ""
    room = available_minutes(repo, meeting)
    names = []
    for d in items[:MAX_UNFIT_LISTED]:
        who = f" <@{d.created_by}>" if is_user_id(d.created_by) else ""
        names.append(f"「{_plain(d.title)}」（{d.minutes} 分{who}）")
    more = f" ほか {len(items) - MAX_UNFIT_LISTED} 件" if len(items) > MAX_UNFIT_LISTED else ""
    return (
        f"議題に使える時間（{room} 分 = 会議 {meeting.default_minutes} 分 − 固定の議題 {meeting.default_minutes - room} 分）に"
        f"収まりません: {'、'.join(names)}{more}。このままでは毎回次回へ繰り越されます。"
        "時間を短くして出し直すか取り下げてもらうか、会議時間・固定の議題を見直してください。"
    )


def _plain(text: str) -> str:
    """Slack の mrkdwn に入れる利用者の文字列（views.esc と同じ規則. service は views に依存しないのでここに置く）."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    """議題を次の開催日に出す. today に日時（clock.now()）を渡すと、今日の会議が終わった後なら翌週に出す.

    会議の予定（曜日・時刻・長さ）が読んだときのままのときだけ書く（put_draft_if_schedule）。同時に会議が変更されたら、
    新しい予定で 1 回だけ出し直す（古い開催日に議題が残って、付け替えから漏れないように）。会議が削除されていたら出さない。
    """
    title = _title(title)
    detail = (detail or "").strip()
    if len(detail) > MAX_DETAIL:
        raise ValidationError("detail", f"補足は {MAX_DETAIL} 文字以内で入れてください（今は {len(detail)} 文字）")
    n = _minutes(minutes, "minutes")
    for _ in range(2):
        d = _new_draft(repo, meeting, today, title, detail, n, presenter, user)
        if repo.put_draft_if_schedule(d, meeting.schedule_version):
            return d
        fresh = repo.get_meeting(meeting.meeting_id)
        if fresh is None:
            raise ValidationError("title", "その会議は見つかりません（削除されたかもしれません）")
        meeting = fresh
    raise ValidationError("minutes", "ちょうど会議の予定が変更（または会議が削除）されたところです。もう一度出してください")


def _new_draft(
    repo: Repository, meeting: Meeting, today: date | datetime, title: str, detail: str, n: int, presenter: str, user: str
) -> DraftItem:
    room = available_minutes(repo, meeting)
    if room <= 0:
        raise ValidationError(
            "minutes",
            "固定の議題だけで会議時間が埋まっているため、議題を出せません（担当者に固定の議題か会議時間の見直しを頼んでください）",
        )
    if n > room:
        # 会議時間に入らない議題は、何度繰り越しても扱えないので、出すときに弾く
        raise ValidationError(
            "minutes",
            f"議題に使える時間は {room} 分までです（会議 {meeting.default_minutes} 分 − 固定の議題 {meeting.default_minutes - room} 分）",
        )
    return DraftItem(
        meeting.meeting_id,
        meeting.next_date(today).isoformat(),
        new_id(),
        title,
        detail,
        n,
        presenter or user,
        user,
    )


def remove_draft(repo: Repository, meeting: Meeting, meeting_date: str, item_id: str, user: str, admins: Iterable[str] = ()) -> None:
    """取り下げられるのは、出した本人・会議の登録者・AGENDA_ADMINS の担当者（admins）だけ.

    アジェンダに「繰越」として見えている議題は、確定するまで元の開催日に保存されているので、そちらも探す。
    """
    d = next((x for x in repo.drafts(meeting.meeting_id, meeting_date) if x.item_id == item_id), None)
    if d is None:
        d = next((x for x in repo.drafts_before(meeting.meeting_id, meeting_date, meeting.settled_through) if x.item_id == item_id), None)
    if d is None:
        raise ValidationError("item", "その議題は見つかりません（取り下げ済みかもしれません）")
    if user not in (d.created_by, meeting.owner) and user not in frozenset(admins):
        raise ValidationError("item", "議題を取り下げられるのは、出した人と会議の担当者（会議の登録者・AGENDA_ADMINS）だけです")
    repo.delete_draft(meeting.meeting_id, d.meeting_date, item_id)


FAR_FUTURE = "9999-12-31"


def update_meeting(
    repo: Repository,
    meeting: Meeting,
    user: str,
    weekday: str | int,
    time: str,
    minutes: str | int,
    now: date | datetime,
    admins: Iterable[str] = (),
) -> tuple[Meeting, list[DraftItem]]:
    """会議の曜日・時刻・長さを変える（登録者と AGENDA_ADMINS の担当者だけ）. → (変えた会議, 開催日を付け替えた議題).

    次の開催日やその後に出ている議題は、新しい曜日・時刻の開催日へ付け替える（同じ週数だけ先の開催日へ）。
    確定も中止もされていない過去の会議があるときは、先にそちらを決めてもらう（どの曜日の会議として扱うか曖昧になるため）。

    同時に出される議題（add_draft）と競合しないよう、先に予定を書き換えて schedule_version を上げ（条件付き）、その後で
    議題を強い整合性で読み直して付け替える。版が上がった後に古い予定で出そうとした議題は、add_draft が新しい予定で出し直す。
    同時にほかの人が会議を変更していたら（版が違えば）書かずに断る。
    """
    if not can_edit_meeting(meeting, user, admins):
        raise ValidationError("minutes", "会議を変更できるのは、会議の登録者と担当者（AGENDA_ADMINS）だけです")
    current = repo.get_meeting(meeting.meeting_id)
    if current is None:
        raise ValidationError("minutes", "その会議は見つかりません（削除されたかもしれません）")
    new = replace(current, weekday=_weekday(weekday), time=_time(time), default_minutes=_minutes(minutes, "minutes"))
    old_next = current.next_date(now)
    pending = repo.drafts_before(current.meeting_id, FAR_FUTURE, current.settled_through)
    past = sorted({d.meeting_date for d in pending if d.meeting_date < old_next.isoformat()})
    if past:
        raise ValidationError("minutes", f"先に {past[0]} の会議を確定か中止にしてください（「次回のアジェンダ」から選べます）")
    new_next = new.next_date(now)
    # 曜日・時刻・長さだけを書き換える（全体を上書きすると、画面を開いた後に別の人が確定した settled_through を戻してしまう）
    if not repo.update_schedule(current.meeting_id, new.weekday, new.time, new.default_minutes, expected_version=current.schedule_version):
        if repo.get_meeting(current.meeting_id) is None:
            raise ValidationError("minutes", "その会議は見つかりません（削除されたかもしれません）")
        raise ValidationError("minutes", "同時にほかの人が会議を変更しました。開き直してからもう一度変更してください")
    moved: list[DraftItem] = []
    if new_next != old_next:
        for d in repo.drafts_before(current.meeting_id, FAR_FUTURE, current.settled_through, consistent=True):
            if d.meeting_date < old_next.isoformat():
                continue  # 確認の後に過去の開催日へ書かれることはないが、念のため付け替えない
            shift = date.fromisoformat(d.meeting_date) - old_next
            target = (new_next + shift).isoformat()
            if target == d.meeting_date:
                continue
            nd = replace(d, meeting_date=target)
            repo.put_draft(nd)  # 先に書いてから元を消す（途中で止まっても、議題は消えない）
            repo.delete_draft(current.meeting_id, d.meeting_date, d.item_id)
            moved.append(nd)
    return repo.get_meeting(current.meeting_id) or new, moved


def transfer_meeting(repo: Repository, meeting: Meeting, user: str, new_owner: str, admins: Iterable[str] = ()) -> Meeting:
    """会議の登録者（owner）を new_owner に引き継ぐ（登録者と AGENDA_ADMINS の担当者だけ）. → 引き継いだ後の会議.

    AGENDA_ADMINS が空のとき、確定・中止・Canvas への出力・固定の議題は登録者だけができるので、登録者が異動・退職すると
    誰も確定できず、会議の変更も「先に確定」で止まる。登録者が抜ける前に引き継ぐか、AGENDA_ADMINS の担当者が代わりに引き継ぐ。
    今の登録者が読んだときのままのときだけ書く（同時に別の人が引き継いだら断る）。
    """
    current = repo.get_meeting(meeting.meeting_id)
    if current is None:
        raise ValidationError("owner", "その会議は見つかりません（削除されたかもしれません）")
    if not can_edit_meeting(current, user, admins):
        raise ValidationError("owner", "登録者を引き継げるのは、会議の登録者と担当者（AGENDA_ADMINS）だけです")
    new_owner = (new_owner or "").strip()
    if not is_user_id(new_owner):
        raise ValidationError("owner", "引き継ぐ人を選んでください")
    if new_owner == current.owner:
        return current
    if not repo.update_owner(current.meeting_id, new_owner, expected_owner=current.owner):
        if repo.get_meeting(current.meeting_id) is None:
            raise ValidationError("owner", "その会議は見つかりません（削除されたかもしれません）")
        raise ValidationError("owner", "同時にほかの人が登録者を変更しました。開き直してからもう一度変更してください")
    return repo.get_meeting(current.meeting_id) or replace(current, owner=new_owner)


def delete_meeting(repo: Repository, meeting: Meeting, user: str, admins: Iterable[str] = ()) -> None:
    """会議を削除する（登録者と AGENDA_ADMINS の担当者だけ）. 固定の議題・議題・Canvas の記録も消す（Slack 側の Canvas は残る）."""
    if not can_edit_meeting(meeting, user, admins):
        raise ValidationError("meeting", "会議を削除できるのは、会議の登録者と担当者（AGENDA_ADMINS）だけです")
    repo.delete_meeting(meeting.meeting_id)


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
    # 次回へ回した議題のうち、会議時間（固定の議題を除く）より長く、何度繰り越しても収まらない議題
    unfit: list[DraftItem] = field(default_factory=list)


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
    fixed = repo.fixed_items(meeting.meeting_id)
    rows, total, deferred = build_agenda(meeting, day, fixed, repo.drafts(meeting.meeting_id, day.isoformat()) + carried)
    room = max(meeting.default_minutes - sum(f.minutes for f in fixed), 0)
    unfit = [d for d in deferred if d.minutes > room]
    return AgendaView(meeting, day, rows, total, deferred, agenda_markdown(meeting, day, rows, total, deferred), unsettled, unfit)


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


CHANNEL_CANVAS_DATE = "channel"  # CANVAS_MODE=channel の記録の meeting_date（開催日によらず 1 つ）


def channel_canvas_key(channel_id: str) -> str:
    """CANVAS_MODE=channel の記録の meeting_id. チャンネルの Canvas は 1 チャンネルに 1 つなので、会議ではなくチャンネルで記録する

    （同じチャンネルの 2 つ目の会議が新しく作ろうとして channel_canvas_already_exists にならないように）。
    """
    return f"channel#{channel_id}"


def _canvas_owner(meeting: Meeting, mode: str) -> str:
    return channel_canvas_key(meeting.channel_id) if mode == "channel" else meeting.meeting_id


def _create_canvas(repo: Repository, client: CanvasApi, meeting: Meeting, view: AgendaView, key_date: str, mode: str) -> str:
    """「作成中」の印を書けた人だけが呼ぶ. Canvas を作り、ID を保存してから権限を付ける. → Canvas の ID."""
    content = {"type": "markdown", "markdown": view.markdown}
    owner = _canvas_owner(meeting, mode)
    try:
        if mode == "channel":
            res = client.conversations_canvases_create(channel_id=meeting.channel_id, document_content=content)
        else:
            res = client.canvases_create(title=f"{meeting.name} {view.meeting_date.isoformat()}", document_content=content)
    except Exception as e:
        repo.delete_canvas(owner, key_date)  # 作れなかったので印を消し、もう一度押せるようにする
        if _slack_error(e) == "channel_canvas_already_exists":
            raise ValidationError(
                "canvas",
                "このチャンネルには、ボットが記録していない Canvas が既にあります（手で作った・以前の版のボットが別の会議で作った、など）。"
                "チャンネルの Canvas を消すか、CANVAS_MODE=standalone にしてください（docs/setup.md の「Canvas とプラン」）",
            ) from e
        raise
    canvas_id = res["canvas_id"]
    _save_created(repo, client, CanvasRecord(owner, key_date, canvas_id), mode)  # 権限の付与より先に保存する
    if mode != "channel":
        client.canvases_access_set(canvas_id=canvas_id, access_level="write", channel_ids=[meeting.channel_id])
    return canvas_id


def publish_canvas(
    repo: Repository, client: CanvasApi, meeting: Meeting, today: date | datetime, mode: str = "standalone"
) -> tuple[str, bool]:
    """アジェンダを Canvas に書く. 同じ開催日の Canvas があれば中身を置き換える. → (Canvas の ID, 新しく作ったか).

    mode=standalone: 開催日ごとに Canvas を作り、チャンネルに編集権限を付ける（有料プラン向け）
    mode=channel: チャンネルの Canvas（1 チャンネルに 1 つ）を毎回置き換える（API で作るので有料プランが必要. docs/setup.md）。
      記録はチャンネル単位（channel_canvas_key）なので、同じチャンネルの会議は同じ Canvas を使い、最後に出した会議の内容になる

    重複を防ぐため、作る前に「作成中」の印を条件付きで書き（先に書けた 1 人だけが作る）、作った直後に ID を保存してから権限を付ける。
    権限の付与に失敗しても ID は残るので、もう一度押すと同じ Canvas を更新し、権限を付け直す。
    記録した Canvas が Slack 側で消されていた（canvas_not_found）ときは、記録を「作成中」の印に置き換えてから作り直す。
    """
    view = agenda(repo, meeting, today)
    key_date = view.meeting_date.isoformat() if mode == "standalone" else CHANNEL_CANVAS_DATE
    owner = _canvas_owner(meeting, mode)
    content = {"type": "markdown", "markdown": view.markdown}
    rec = repo.get_canvas(owner, key_date)
    if rec is None and mode == "channel":
        # 以前の版は会議ごとに記録していた（meeting_id + "channel"）. このチャンネルのどれかの会議の記録があれば、チャンネルの記録に写して使う
        ids = [meeting.meeting_id] + [
            x.meeting_id for x in repo.meetings_in_channel(meeting.channel_id) if x.meeting_id != meeting.meeting_id
        ]
        for mid in ids:
            legacy = repo.get_canvas(mid, key_date)
            if legacy is not None and legacy.canvas_id != PENDING:
                repo.put_canvas_if(CanvasRecord(owner, key_date, legacy.canvas_id), None)
                rec = repo.get_canvas(owner, key_date)
                break
    if rec is None or rec.canvas_id == PENDING:
        if rec is not None and not _stale(rec):
            raise Busy("canvas", "Canvas を作成中です。少し待ってから、もう一度押してください")
        claim = CanvasRecord(owner, key_date, PENDING)
        if repo.put_canvas_if(claim, rec):
            return _create_canvas(repo, client, meeting, view, key_date, mode), True
        rec = repo.get_canvas(owner, key_date)
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
        if not repo.put_canvas_if(CanvasRecord(owner, key_date, PENDING), rec):
            raise Busy("canvas", "Canvas を作り直しています。少し待ってから、もう一度押してください") from e
        return _create_canvas(repo, client, meeting, view, key_date, mode), True
    repo.put_canvas(CanvasRecord(owner, key_date, rec.canvas_id))
    return rec.canvas_id, False
