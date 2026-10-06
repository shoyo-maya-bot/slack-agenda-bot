"""会議・固定議題・議題の下書き・Canvas の記録（ドメイン）."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

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

    def next_date(self, today: date) -> date:
        """今日（設定したタイムゾーンの日付. clock.today()）を含めて次の開催日."""
        return today + timedelta(days=(self.weekday - today.weekday()) % 7)


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
    created_at: str = field(default_factory=lambda: clock.now().isoformat(timespec="seconds"))
    status: str = "proposed"  # proposed / accepted / deferred


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
    """固定議題 → 出された議題（受付順）の順に並べ、会議時間に収まらない議題は「次回へ」に回す.

    戻り値: (並べた議題, 合計の分, 次回へ回した議題)
    """
    rows: list[dict] = []
    total = 0
    for f in sorted(fixed, key=lambda x: (x.order, x.title)):
        rows.append({"kind": "固定", "title": f.title, "minutes": f.minutes, "presenter": f.presenter, "detail": ""})
        total += f.minutes
    deferred: list[DraftItem] = []
    for d in sorted((d for d in drafts if d.status != "deferred"), key=lambda x: x.created_at):
        if total + d.minutes > meeting.default_minutes:
            deferred.append(d)
            continue
        rows.append(
            {"kind": "議題", "title": d.title, "minutes": d.minutes, "presenter": d.presenter, "detail": d.detail, "item_id": d.item_id}
        )
        total += d.minutes
    return rows, total, deferred


def agenda_markdown(meeting: Meeting, meeting_date: date, rows: list[dict], total: int, deferred: list[DraftItem]) -> str:
    """Canvas 用の Markdown. ユーザーは <@U…> で書くと Canvas でメンションになる."""
    lines = [
        f"# {meeting.name} アジェンダ（{meeting_date.isoformat()}（{WEEKDAYS[meeting_date.weekday()]}）{meeting.time}〜）",
        "",
        f"合計 {total} 分 ／ 会議時間 {meeting.default_minutes} 分",
        "",
        "| # | 種類 | 議題 | 時間 | 担当 |",
        "|---|---|---|---|---|",
    ]
    for n, r in enumerate(rows, start=1):
        who = f"<@{r['presenter']}>" if r["presenter"].startswith("U") else r["presenter"]
        lines.append(f"| {n} | {r['kind']} | {r['title']} | {r['minutes']} 分 | {who} |")
    details = [r for r in rows if r.get("detail")]
    if details:
        lines += ["", "## 議題の補足"]
        for r in details:
            lines += ["", f"### {r['title']}", "", r["detail"]]
    if deferred:
        lines += ["", "## 次回へ（時間に収まらなかった議題）", ""] + [f"- {d.title}（{d.minutes} 分）" for d in deferred]
    lines += ["", "## メモ", "", "- ", ""]
    return "\n".join(lines)
