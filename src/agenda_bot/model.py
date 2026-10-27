"""会議・固定議題・議題の下書き・Canvas の記録（ドメイン）."""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as Time  # Meeting.time（"10:00" の文字列）と名前がぶつからないように別名で使う

from . import clock

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

# Slack のユーザー ID（U…、Enterprise Grid では W…）. AGENDA_ADMINS の検査とメンションの判定で同じ規則を使う
USER_ID = re.compile(r"^[UW][A-Z0-9]+$")


def is_user_id(value: str) -> bool:
    """Slack のユーザー ID（U… / W…）か. そうなら <@…> でメンションにする."""
    return bool(USER_ID.match(value or ""))


def new_id() -> str:
    return uuid.uuid4().hex[:12]


_created_lock = threading.Lock()
_last_created: list[datetime] = []


def created_at() -> str:
    """議題を出した時刻（ISO 形式）. 出した順に並べるのに使う.

    Windows の Python 3.12 などは時計の刻みが粗く（約 15 ms）、続けて出した議題が同じ時刻になることがある。
    直前（1 秒以内）に出した時刻以下なら 1 マイクロ秒ずらして、同じプロセスの中では必ず後の時刻にする。
    """
    t = clock.now()
    with _created_lock:
        if _last_created:
            last = _last_created[0]
            if last - timedelta(seconds=1) < t <= last:
                t = last + timedelta(microseconds=1)
            _last_created[0] = t
        else:
            _last_created.append(t)
    return t.isoformat()


@dataclass
class Meeting:
    meeting_id: str
    channel_id: str
    name: str
    weekday: int  # 0=月 … 6=日
    time: str  # "10:00"
    owner: str  # Slack のユーザー ID
    default_minutes: int = 60
    # 確定か中止を済ませた最後の開催日（YYYY-MM-DD）. これ以前の議題は扱い済みなので、繰り越しを探すときに読まない
    settled_through: str = ""
    # 曜日・時刻・長さを変えるたびに 1 増える. 議題を出すときに、読んだ会議の予定のままか（途中で変わっていないか）を確かめる
    schedule_version: int = 0

    def start_time(self) -> Time:
        return Time(int(self.time[:2]), int(self.time[3:5]))

    def next_date(self, now: date | datetime) -> date:
        """次の開催日.

        now が日時（clock.now()）のとき: 今日の会議が終わった後（開始時刻＋会議時間を過ぎた後）なら翌週にする。
        now が日付（clock.today()）のとき: 今日を含めて次の開催日（時刻は見ない）。
        """
        today = now.date() if isinstance(now, datetime) else now
        day = today + timedelta(days=(self.weekday - today.weekday()) % 7)
        if isinstance(now, datetime) and day == today:
            end = datetime.combine(day, self.start_time(), tzinfo=now.tzinfo) + timedelta(minutes=self.default_minutes)
            if now >= end:
                day += timedelta(days=7)
        return day


@dataclass
class FixedItem:
    """毎回の議題（例: 先週の振り返り、課題の確認）."""

    meeting_id: str
    item_id: str
    title: str
    order: int = 100
    minutes: int = 5
    presenter: str = ""


@dataclass
class DraftItem:
    """次回に向けて出された議題."""

    meeting_id: str
    meeting_date: str  # YYYY-MM-DD
    item_id: str
    title: str
    detail: str = ""
    minutes: int = 10
    presenter: str = ""  # Slack のユーザー ID
    created_by: str = ""
    # 出した順に並べるため、マイクロ秒まで持つ（同じ秒に出た議題の順が保存先の都合で入れ替わらないように）
    created_at: str = field(default_factory=created_at)
    # proposed（出された）/ accepted（開催日に扱った。担当者が「確定」したとき）/ deferred（前の会議から繰り越した）
    status: str = "proposed"
    carried_from: str = ""  # 繰り越した議題の、最初の開催日（YYYY-MM-DD）
    # 扱い済み（accepted）にした議題を DynamoDB の TTL で消す時刻（UNIX 秒）. 0 は消さない（保存するときに属性ごと省く）
    expires_at: int = 0


ACCEPTED_RETENTION_DAYS = 180  # 扱い済みの議題を開催日から何日残すか（DynamoDB の TTL で消す）


def accepted_expiry(meeting_date: str, days: int = ACCEPTED_RETENTION_DAYS) -> int:
    """扱い済みの議題を消してよい時刻（開催日の days 日後の 0 時 UTC、UNIX 秒）."""
    d = date.fromisoformat(meeting_date) + timedelta(days=days)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


@dataclass
class CanvasRecord:
    meeting_id: str
    meeting_date: str
    canvas_id: str
    updated_at: str = field(default_factory=lambda: clock.now().isoformat(timespec="seconds"))


def to_dict(obj) -> dict:
    return asdict(obj)


def build_agenda(
    meeting: Meeting, meeting_date: date, fixed: list[FixedItem], drafts: list[DraftItem]
) -> tuple[list[dict], int, list[DraftItem]]:
    """アジェンダを組む（README の「アジェンダの組み方」と同じ決まり）.

    1. 固定の議題を order（同じなら議題名）の順にすべて並べる（会議時間を超えても並べる）
    2. 繰り越した議題（status=deferred）を、最初の開催日（carried_from）の古い順 → 出した順（created_at）に
    3. 新しく出た議題を、出した順（created_at）に
    2・3 は前から順に、合計が会議時間を超えない議題だけを並べ、超える議題は「次回へ」に回す。回した後も後ろの議題は続けて試すので、
    後に出た短い議題が、先に出た長い議題より先に入ることがある（先頭から詰める first-fit）。
    扱い済み（accepted）の議題は並べない。
    戻り値: (並べた議題, 合計の分, 次回へ回した議題)
    """
    rows: list[dict] = []
    total = 0
    for f in sorted(fixed, key=lambda x: (x.order, x.title)):
        rows.append({"kind": "固定", "title": f.title, "minutes": f.minutes, "presenter": f.presenter, "detail": ""})
        total += f.minutes
    deferred: list[DraftItem] = []
    pending = (d for d in drafts if d.status != "accepted")
    for d in sorted(pending, key=lambda x: (x.status != "deferred", x.carried_from or x.meeting_date, x.created_at)):
        if total + d.minutes > meeting.default_minutes:
            deferred.append(d)
            continue
        rows.append(
            {
                "kind": "繰越" if d.status == "deferred" else "議題",
                "title": d.title,
                "minutes": d.minutes,
                "presenter": d.presenter,
                "detail": d.detail,
                "item_id": d.item_id,
                "carried_from": d.carried_from if d.status == "deferred" else "",
            }
        )
        total += d.minutes
    return rows, total, deferred


def canvas_text(text: str, *, cell: bool = True) -> str:
    """Canvas の Markdown に入れる利用者の文字列を無害化する.

    < と > は全角にして、<!channel> や <@U…> などの Slack の特殊な書式（一斉メンション）にならないようにする。
    表のセル（cell=True）では | をエスケープし、改行を空白にする。
    """
    text = text.replace("<", "＜").replace(">", "＞")
    if cell:
        text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text


def agenda_markdown(meeting: Meeting, meeting_date: date, rows: list[dict], total: int, deferred: list[DraftItem]) -> str:
    """Canvas 用の Markdown. ユーザーは <@U…> で書くと Canvas でメンションになる."""
    lines = [
        f"# {canvas_text(meeting.name)} アジェンダ（{meeting_date.isoformat()}（{WEEKDAYS[meeting_date.weekday()]}）{meeting.time}〜）",
        "",
        f"合計 {total} 分 ／ 会議時間 {meeting.default_minutes} 分",
        "",
        "| # | 種類 | 議題 | 時間 | 担当 |",
        "|---|---|---|---|---|",
    ]
    for n, r in enumerate(rows, start=1):
        who = f"<@{r['presenter']}>" if is_user_id(r["presenter"]) else canvas_text(r["presenter"])
        lines.append(f"| {n} | {r['kind']} | {canvas_text(r['title'])} | {r['minutes']} 分 | {who} |")
    details = [r for r in rows if r.get("detail")]
    if details:
        lines += ["", "## 議題の補足"]
        for r in details:
            lines += ["", f"### {canvas_text(r['title'])}", "", canvas_text(r["detail"], cell=False)]
    if deferred:
        lines += ["", "## 次回へ（時間に収まらなかった議題）", ""] + [f"- {canvas_text(d.title)}（{d.minutes} 分）" for d in deferred]
    lines += ["", "## メモ", "", "- ", ""]
    return "\n".join(lines)
