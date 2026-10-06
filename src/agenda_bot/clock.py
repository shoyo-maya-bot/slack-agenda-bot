"""日付と時刻. サーバーの時刻設定（UTC など）ではなく、設定したタイムゾーンで「今日」と「今」を決める.

タイムゾーンは環境変数 TIMEZONE（IANA の名前。例: Asia/Tokyo）。未設定なら Asia/Tokyo。
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Tokyo"


def _utcnow() -> datetime:
    """現在時刻（UTC）. テストではここを差し替えて時刻を止める."""
    return datetime.now(UTC)


def get_tz(name: str | None = None) -> ZoneInfo:
    """タイムゾーン. 引数 → 環境変数 TIMEZONE → Asia/Tokyo の順で決める."""
    name = name or os.environ.get("TIMEZONE") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"TIMEZONE「{name}」は使えません（例: Asia/Tokyo）") from e


def now(tz: str | None = None) -> datetime:
    """設定したタイムゾーンの現在時刻（タイムゾーン付き）."""
    return _utcnow().astimezone(get_tz(tz))


def today(tz: str | None = None) -> date:
    """設定したタイムゾーンの今日の日付."""
    return now(tz).date()
