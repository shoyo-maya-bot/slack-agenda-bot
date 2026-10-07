"""保存先. DynamoDB（本番・DynamoDB Local）と、メモリ（テスト）の 2 つを同じ形で持つ.

テーブル（scripts/create_tables.py で作る。名前の前に TABLE_PREFIX が付く）:
| テーブル | パーティションキー | ソートキー | 中身 |
|---|---|---|---|
| meetings | meeting_id | - | 会議（チャンネル・曜日・時刻・時間） |
| fixed_items | meeting_id | item_id | 固定議題 |
| drafts | meeting_id | sk = 開催日#item_id | 次回に向けて出された議題 |
| canvases | meeting_id | meeting_date | 開催日ごとの Canvas の ID |
"""

from __future__ import annotations

import threading
from dataclasses import fields
from typing import Any, Protocol

from .model import CanvasRecord, DraftItem, FixedItem, Meeting, to_dict

TABLES: dict[str, dict[str, str]] = {
    "meetings": {"pk": "meeting_id"},
    "fixed_items": {"pk": "meeting_id", "sk": "item_id"},
    "drafts": {"pk": "meeting_id", "sk": "sk"},
    "canvases": {"pk": "meeting_id", "sk": "meeting_date"},
}


class Repository(Protocol):
    def put_meeting(self, m: Meeting) -> None: ...
    def get_meeting(self, meeting_id: str) -> Meeting | None: ...
    def meetings_in_channel(self, channel_id: str) -> list[Meeting]: ...
    def put_fixed(self, f: FixedItem) -> None: ...
    def delete_fixed(self, meeting_id: str, item_id: str) -> None: ...
    def fixed_items(self, meeting_id: str) -> list[FixedItem]: ...
    def put_draft(self, d: DraftItem) -> None: ...
    def delete_draft(self, meeting_id: str, meeting_date: str, item_id: str) -> None: ...
    def drafts(self, meeting_id: str, meeting_date: str) -> list[DraftItem]: ...
    def drafts_before(self, meeting_id: str, meeting_date: str) -> list[DraftItem]: ...
    def put_canvas(self, c: CanvasRecord) -> None: ...
    def put_canvas_if(self, c: CanvasRecord, expected: CanvasRecord | None) -> bool: ...
    def delete_canvas(self, meeting_id: str, meeting_date: str) -> None: ...
    def get_canvas(self, meeting_id: str, meeting_date: str) -> CanvasRecord | None: ...


def _build(cls, item: dict[str, Any]):
    names = {f.name for f in fields(cls)}
    return cls(**{k: (int(v) if isinstance(v, int | float) or type(v).__name__ == "Decimal" else v) for k, v in item.items() if k in names})


class MemoryRepository:
    def __init__(self) -> None:
        self.t: dict[str, dict[tuple, dict]] = {k: {} for k in TABLES}
        self._lock = threading.Lock()  # 条件付きの書き込み（put_canvas_if）を DynamoDB と同じく 1 回の操作にする

    def _put(self, table: str, item: dict) -> None:
        spec = TABLES[table]
        self.t[table][(item[spec["pk"]], item.get(spec.get("sk", ""), ""))] = dict(item)

    def _query(self, table: str, pk: str, prefix: str = "") -> list[dict]:
        sk = TABLES[table].get("sk", "")
        return [v for (p, s), v in sorted(self.t[table].items()) if p == pk and str(s).startswith(prefix)] if sk else []

    def put_meeting(self, m: Meeting) -> None:
        self._put("meetings", to_dict(m))

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        v = self.t["meetings"].get((meeting_id, ""))
        return _build(Meeting, v) if v else None

    def meetings_in_channel(self, channel_id: str) -> list[Meeting]:
        return [_build(Meeting, v) for v in self.t["meetings"].values() if v["channel_id"] == channel_id]

    def put_fixed(self, f: FixedItem) -> None:
        self._put("fixed_items", to_dict(f))

    def delete_fixed(self, meeting_id: str, item_id: str) -> None:
        self.t["fixed_items"].pop((meeting_id, item_id), None)

    def fixed_items(self, meeting_id: str) -> list[FixedItem]:
        return [_build(FixedItem, v) for v in self._query("fixed_items", meeting_id)]

    def put_draft(self, d: DraftItem) -> None:
        self._put("drafts", {**to_dict(d), "sk": f"{d.meeting_date}#{d.item_id}"})

    def delete_draft(self, meeting_id: str, meeting_date: str, item_id: str) -> None:
        self.t["drafts"].pop((meeting_id, f"{meeting_date}#{item_id}"), None)

    def drafts(self, meeting_id: str, meeting_date: str) -> list[DraftItem]:
        return [_build(DraftItem, v) for v in self._query("drafts", meeting_id, f"{meeting_date}#")]

    def drafts_before(self, meeting_id: str, meeting_date: str) -> list[DraftItem]:
        """meeting_date より前の開催日の議題（扱い済みの accepted を除く）."""
        return [_build(DraftItem, v) for v in self._query("drafts", meeting_id) if v["sk"] < meeting_date and v.get("status") != "accepted"]

    def put_canvas(self, c: CanvasRecord) -> None:
        self._put("canvases", to_dict(c))

    def put_canvas_if(self, c: CanvasRecord, expected: CanvasRecord | None) -> bool:
        """今の記録が expected と同じ（None なら記録が無い）ときだけ書く. 書けたら True."""
        with self._lock:
            if self.get_canvas(c.meeting_id, c.meeting_date) != expected:
                return False
            self._put("canvases", to_dict(c))
            return True

    def delete_canvas(self, meeting_id: str, meeting_date: str) -> None:
        self.t["canvases"].pop((meeting_id, meeting_date), None)

    def get_canvas(self, meeting_id: str, meeting_date: str) -> CanvasRecord | None:
        v = self.t["canvases"].get((meeting_id, meeting_date))
        return _build(CanvasRecord, v) if v else None


class DynamoRepository:
    """boto3 の DynamoDB. endpoint_url を渡すと DynamoDB Local を使う."""

    def __init__(self, prefix: str = "agenda_", endpoint_url: str | None = None, region: str = "ap-northeast-1", resource: Any = None):
        if resource is None:
            import boto3

            resource = boto3.resource("dynamodb", endpoint_url=endpoint_url or None, region_name=region)
        self.db = resource
        self.tables = {k: resource.Table(prefix + k) for k in TABLES}

    def _query(self, table: str, pk: str, prefix: str = "") -> list[dict]:
        from boto3.dynamodb.conditions import Key

        spec = TABLES[table]
        cond = Key(spec["pk"]).eq(pk)
        if prefix:
            cond = cond & Key(spec["sk"]).begins_with(prefix)
        items, kw = [], {"KeyConditionExpression": cond}
        while True:
            res = self.tables[table].query(**kw)
            items += res.get("Items", [])
            if "LastEvaluatedKey" not in res:
                return items
            kw["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def put_meeting(self, m: Meeting) -> None:
        self.tables["meetings"].put_item(Item=to_dict(m))

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        v = self.tables["meetings"].get_item(Key={"meeting_id": meeting_id}).get("Item")
        return _build(Meeting, v) if v else None

    def meetings_in_channel(self, channel_id: str) -> list[Meeting]:
        # 会議の数は少ない前提で scan（多くなったら channel_id の GSI を足す）
        from boto3.dynamodb.conditions import Attr

        items, kw = [], {"FilterExpression": Attr("channel_id").eq(channel_id)}
        while True:
            res = self.tables["meetings"].scan(**kw)
            items += res.get("Items", [])
            if "LastEvaluatedKey" not in res:
                return [_build(Meeting, v) for v in items]
            kw["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def put_fixed(self, f: FixedItem) -> None:
        self.tables["fixed_items"].put_item(Item=to_dict(f))

    def delete_fixed(self, meeting_id: str, item_id: str) -> None:
        self.tables["fixed_items"].delete_item(Key={"meeting_id": meeting_id, "item_id": item_id})

    def fixed_items(self, meeting_id: str) -> list[FixedItem]:
        return [_build(FixedItem, v) for v in self._query("fixed_items", meeting_id)]

    def put_draft(self, d: DraftItem) -> None:
        self.tables["drafts"].put_item(Item={**to_dict(d), "sk": f"{d.meeting_date}#{d.item_id}"})

    def delete_draft(self, meeting_id: str, meeting_date: str, item_id: str) -> None:
        self.tables["drafts"].delete_item(Key={"meeting_id": meeting_id, "sk": f"{meeting_date}#{item_id}"})

    def drafts(self, meeting_id: str, meeting_date: str) -> list[DraftItem]:
        return [_build(DraftItem, v) for v in self._query("drafts", meeting_id, f"{meeting_date}#")]

    def drafts_before(self, meeting_id: str, meeting_date: str) -> list[DraftItem]:
        """meeting_date より前の開催日の議題（扱い済みの accepted を除く）."""
        from boto3.dynamodb.conditions import Attr, Key

        kw: dict[str, Any] = {
            "KeyConditionExpression": Key("meeting_id").eq(meeting_id) & Key("sk").lt(meeting_date),
            "FilterExpression": Attr("status").ne("accepted"),
        }
        items: list[dict] = []
        while True:
            res = self.tables["drafts"].query(**kw)
            items += res.get("Items", [])
            if "LastEvaluatedKey" not in res:
                return [_build(DraftItem, v) for v in items]
            kw["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def put_canvas(self, c: CanvasRecord) -> None:
        self.tables["canvases"].put_item(Item=to_dict(c))

    def put_canvas_if(self, c: CanvasRecord, expected: CanvasRecord | None) -> bool:
        """今の記録が expected と同じ（None なら記録が無い）ときだけ書く（条件付き put）. 書けたら True."""
        from botocore.exceptions import ClientError

        if expected is None:
            cond: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(meeting_id)"}
        else:
            cond = {
                "ConditionExpression": "canvas_id = :c AND updated_at = :u",
                "ExpressionAttributeValues": {":c": expected.canvas_id, ":u": expected.updated_at},
            }
        try:
            self.tables["canvases"].put_item(Item=to_dict(c), **cond)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def delete_canvas(self, meeting_id: str, meeting_date: str) -> None:
        self.tables["canvases"].delete_item(Key={"meeting_id": meeting_id, "meeting_date": meeting_date})

    def get_canvas(self, meeting_id: str, meeting_date: str) -> CanvasRecord | None:
        v = self.tables["canvases"].get_item(Key={"meeting_id": meeting_id, "meeting_date": meeting_date}).get("Item")
        return _build(CanvasRecord, v) if v else None


def create_tables(resource: Any, prefix: str = "agenda_") -> list[str]:
    """テーブルが無ければ作る（オンデマンド課金）. 作ったテーブル名を返す."""
    existing = {t.name for t in resource.tables.all()}
    made = []
    for name, spec in TABLES.items():
        full = prefix + name
        if full in existing:
            continue
        keys = [{"AttributeName": spec["pk"], "KeyType": "HASH"}]
        attrs = [{"AttributeName": spec["pk"], "AttributeType": "S"}]
        if "sk" in spec:
            keys.append({"AttributeName": spec["sk"], "KeyType": "RANGE"})
            attrs.append({"AttributeName": spec["sk"], "AttributeType": "S"})
        resource.create_table(TableName=full, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST").wait_until_exists()
        made.append(full)
    return made
