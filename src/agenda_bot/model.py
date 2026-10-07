"""会議・固定議題・議題の下書き・Canvas の記録（ドメイン）."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

from . import clock

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Meeting:
    meeting_id: str
    channel_id: str
    name: str
    weekday: int  # 0=月 … 6=日
    time: str  # "10:00"
    owner: str  # Slack のユーザー ID
    default_minutes: int = 60

    def start_time(self) -> time:
        return time(int(self.time[:2]), int(self.time[3:5]))

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
    created_at: str = field(default_factory=lambda: clock.now().isoformat())
    status: str = "proposed"  # proposed（出された）/ accepted（開催日に扱った）/ deferred（前の会議から繰り越した）
    carried_from: str = ""  # 繰り越した議題の、最初の開催日（YYYY-MM-DD）


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
    """固定議題 → 前の会議から繰り越した議題 → 出された議題（受付順）の順に並べ、会議時間に収まらない議題は「次回へ」に回す.

    取り下げ済み（accepted）の議題は並べない。
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
        who = f"<@{r['presenter']}>" if r["presenter"].startswith("U") else canvas_text(r["presenter"])
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
