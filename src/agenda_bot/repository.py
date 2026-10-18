"""保存先. DynamoDB（本番・DynamoDB Local）と、メモリ（テスト）の 2 つを同じ形で持つ.

テーブル（scripts/create_tables.py で作る。名前の前に TABLE_PREFIX が付く）:
| テーブル | パーティションキー | ソートキー | 中身 |
|---|---|---|---|
| meetings | meeting_id | - | 会議（チャンネル・曜日・時刻・時間）. GSI channel_id-index（channel_id）で /agenda のチャンネルの会議を引く |
| fixed_items | meeting_id | item_id | 固定議題 |
| drafts | meeting_id | sk = 開催日#item_id | 次回に向けて出された議題（扱い済みは TTL 属性 expires_at で消える） |
| canvases | meeting_id | meeting_date | 開催日ごとの Canvas の ID |
"""

from __future__ import annotations

import logging
import threading
from dataclasses import fields
from datetime import date, timedelta
from typing import Any, Protocol

from .model import CanvasRecord, DraftItem, FixedItem, Meeting, to_dict

log = logging.getLogger("agenda_bot")

TTL_ATTRIBUTE = "expires_at"  # drafts の TTL 属性（扱い済みの議題だけに入れる）
CHANNEL_INDEX = "channel_id-index"  # meetings の GSI（パーティションキー channel_id、全項目を射影）


class MissingTablesError(RuntimeError):
    """起動時に、必要なテーブルが無い（scripts/create_tables.py を実行していない・TABLE_PREFIX の誤り）."""


TABLES: dict[str, dict[str, str]] = {
    "meetings": {"pk": "meeting_id"},
    "fixed_items": {"pk": "meeting_id", "sk": "item_id"},
    "drafts": {"pk": "meeting_id", "sk": "sk"},
    "canvases": {"pk": "meeting_id", "sk": "meeting_date"},
}


class Repository(Protocol):
    def put_meeting(self, m: Meeting) -> None: ...
    def get_meeting(self, meeting_id: str) -> Meeting | None: ...
    def update_schedule(
        self, meeting_id: str, weekday: int, time: str, default_minutes: int, expected_version: int | None = None
    ) -> bool: ...
    def delete_meeting(self, meeting_id: str) -> None: ...
    def meetings_in_channel(self, channel_id: str) -> list[Meeting]: ...
    def put_fixed(self, f: FixedItem) -> None: ...
    def delete_fixed(self, meeting_id: str, item_id: str) -> None: ...
    def fixed_items(self, meeting_id: str) -> list[FixedItem]: ...
    def put_draft(self, d: DraftItem) -> None: ...
    def put_draft_if_schedule(self, d: DraftItem, schedule_version: int) -> bool: ...
    def delete_draft(self, meeting_id: str, meeting_date: str, item_id: str) -> None: ...
    def drafts(self, meeting_id: str, meeting_date: str) -> list[DraftItem]: ...
    def drafts_before(self, meeting_id: str, meeting_date: str, since: str = "", consistent: bool = False) -> list[DraftItem]: ...
    def mark_settled(self, meeting_id: str, meeting_date: str) -> None: ...
    def put_canvas(self, c: CanvasRecord) -> None: ...
    def put_canvas_if(self, c: CanvasRecord, expected: CanvasRecord | None) -> bool: ...
    def delete_canvas(self, meeting_id: str, meeting_date: str) -> None: ...
    def get_canvas(self, meeting_id: str, meeting_date: str) -> CanvasRecord | None: ...


def _draft_item(d: DraftItem) -> dict[str, Any]:
    item = {**to_dict(d), "sk": f"{d.meeting_date}#{d.item_id}"}
    if not item.get(TTL_ATTRIBUTE):
        item.pop(TTL_ATTRIBUTE, None)  # 0 を入れると TTL が「1970 年に期限切れ」とみなして消してしまうので、属性ごと省く
    return item


def sk_range(meeting_date: str, since: str = "") -> tuple[str, str]:
    """drafts_before のソートキーの範囲（両端を含む）. since（確定・中止を済ませた最後の開催日）の翌日から、meeting_date の前まで.

    sk は「開催日#item_id」なので、上端を meeting_date（# の無い文字列）にすると、その日の議題は範囲に入らない。
    """
    lo = (date.fromisoformat(since) + timedelta(days=1)).isoformat() if since else "0"
    return lo, meeting_date


def _build(cls, item: dict[str, Any]):
    names = {f.name for f in fields(cls)}
    return cls(**{k: (int(v) if isinstance(v, int | float) or type(v).__name__ == "Decimal" else v) for k, v in item.items() if k in names})


class MemoryRepository:
    def __init__(self) -> None:
        self.t: dict[str, dict[tuple, dict]] = {k: {} for k in TABLES}
        self._lock = threading.Lock()  # 条件付きの書き込み（put_canvas_if）を DynamoDB と同じく 1 回の操作にする
        self.read_count = 0  # drafts_before が読んだ（フィルター前の）件数. 全件読みしていないことをテストで確かめる

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

    def update_schedule(self, meeting_id: str, weekday: int, time: str, default_minutes: int, expected_version: int | None = None) -> bool:
        """会議の曜日・時刻・長さだけを書き換え、schedule_version を 1 増やす（settled_through など他の項目はそのまま）.

        会議が無い・expected_version を渡したのに今の版と違うときは書かずに False.
        """
        with self._lock:
            v = self.t["meetings"].get((meeting_id, ""))
            if v is None or (expected_version is not None and int(v.get("schedule_version", 0)) != expected_version):
                return False
            v.update(weekday=weekday, time=time, default_minutes=default_minutes, schedule_version=int(v.get("schedule_version", 0)) + 1)
            return True

    def delete_meeting(self, meeting_id: str) -> None:
        """会議と、その固定の議題・議題・Canvas の記録を消す."""
        with self._lock:
            for table in TABLES:
                for key in [k for k in self.t[table] if k[0] == meeting_id]:
                    del self.t[table][key]

    def meetings_in_channel(self, channel_id: str) -> list[Meeting]:
        return [_build(Meeting, v) for v in self.t["meetings"].values() if v["channel_id"] == channel_id]

    def check_tables(self) -> list[str]:
        return []

    def put_fixed(self, f: FixedItem) -> None:
        self._put("fixed_items", to_dict(f))

    def delete_fixed(self, meeting_id: str, item_id: str) -> None:
        self.t["fixed_items"].pop((meeting_id, item_id), None)

    def fixed_items(self, meeting_id: str) -> list[FixedItem]:
        return [_build(FixedItem, v) for v in self._query("fixed_items", meeting_id)]

    def put_draft(self, d: DraftItem) -> None:
        self._put("drafts", _draft_item(d))

    def put_draft_if_schedule(self, d: DraftItem, schedule_version: int) -> bool:
        """会議があり、その schedule_version が渡した版のままのときだけ議題を書く（1 回の操作）. 書けたら True."""
        with self._lock:
            v = self.t["meetings"].get((d.meeting_id, ""))
            if v is None or int(v.get("schedule_version", 0)) != schedule_version:
                return False
            self._put("drafts", _draft_item(d))
            return True

    def delete_draft(self, meeting_id: str, meeting_date: str, item_id: str) -> None:
        self.t["drafts"].pop((meeting_id, f"{meeting_date}#{item_id}"), None)

    def drafts(self, meeting_id: str, meeting_date: str) -> list[DraftItem]:
        return [_build(DraftItem, v) for v in self._query("drafts", meeting_id, f"{meeting_date}#")]

    def drafts_before(self, meeting_id: str, meeting_date: str, since: str = "", consistent: bool = False) -> list[DraftItem]:
        """meeting_date より前（since より後）の開催日の議題（扱い済みの accepted を除く）. 読んだ件数を read_count に足す."""
        lo, hi = sk_range(meeting_date, since)
        rows = [v for v in self._query("drafts", meeting_id) if lo <= v["sk"] <= hi]
        self.read_count += len(rows)
        return [_build(DraftItem, v) for v in rows if v.get("status") != "accepted"]

    def mark_settled(self, meeting_id: str, meeting_date: str) -> None:
        with self._lock:
            v = self.t["meetings"].get((meeting_id, ""))
            if v is not None and v.get("settled_through", "") < meeting_date:
                v["settled_through"] = meeting_date

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
        self.prefix = prefix
        self.tables = {k: resource.Table(prefix + k) for k in TABLES}
        self._channel_index: bool | None = None  # GSI channel_id-index があるか（None はまだ確かめていない）

    def check_tables(self) -> list[str]:
        """起動時の確認. テーブルが無ければ MissingTablesError. → 警告（GSI・TTL が無いなど. 動くが直したほうがよいもの）.

        DescribeTable の権限が無い（AccessDenied）ときは確かめずに続ける（読み書きの権限だけを付けた環境のため）。
        """
        from botocore.exceptions import ClientError

        missing, warnings = [], []
        for name, table in self.tables.items():
            try:
                desc = self.db.meta.client.describe_table(TableName=table.name)["Table"]
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                if code == "ResourceNotFoundException":
                    missing.append(table.name)
                    continue
                if code in ("AccessDeniedException", "UnrecognizedClientException"):
                    return [f"テーブルの存在を確かめられませんでした（{code}。dynamodb:DescribeTable の権限があれば起動時に確かめます）"]
                raise
            if name == "meetings":
                index = next((g for g in desc.get("GlobalSecondaryIndexes") or [] if g.get("IndexName") == CHANNEL_INDEX), None)
                self._channel_index = index is not None and index.get("IndexStatus", "ACTIVE") == "ACTIVE"
                if index is None:
                    warnings.append(
                        f"{table.name} に GSI {CHANNEL_INDEX} がありません。/agenda のたびに会議の全件を読みます"
                        "（scripts/create_tables.py をもう一度実行すると足します）"
                    )
                elif not self._channel_index:
                    warnings.append(
                        f"{table.name} の GSI {CHANNEL_INDEX} は作成中（{index.get('IndexStatus')}）です。"
                        "それまで会議の全件を読みます（作成が終わったらボットを起動し直してください）"
                    )
        if missing:
            raise MissingTablesError(
                f"DynamoDB のテーブルがありません: {', '.join(missing)}（scripts/create_tables.py を実行するか、TABLE_PREFIX・AWS_REGION・"
                "DYNAMODB_ENDPOINT を確かめてください）"
            )
        return warnings

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

    def update_schedule(self, meeting_id: str, weekday: int, time: str, default_minutes: int, expected_version: int | None = None) -> bool:
        """会議の曜日・時刻・長さだけを書き換え、schedule_version を 1 増やす（UpdateItem. settled_through など他の項目は書き換えない）.

        会議が無ければ（削除された後なら）書かずに False（put_item のように消えた会議を作り直さない）。
        expected_version を渡すと、今の schedule_version がその版のときだけ書く（違えば False）。
        """
        from botocore.exceptions import ClientError

        cond = "attribute_exists(meeting_id)"
        values: dict[str, Any] = {":w": weekday, ":t": time, ":m": default_minutes, ":zero": 0, ":one": 1}
        if expected_version is not None:
            cond += " AND " + _version_condition(expected_version)
            values[":v"] = expected_version
        try:
            self.tables["meetings"].update_item(
                Key={"meeting_id": meeting_id},
                UpdateExpression="SET weekday = :w, #t = :t, default_minutes = :m, schedule_version = if_not_exists(schedule_version, :zero) + :one",
                ConditionExpression=cond,
                ExpressionAttributeNames={"#t": "time"},  # time は DynamoDB の予約語
                ExpressionAttributeValues=values,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def delete_meeting(self, meeting_id: str) -> None:
        """会議と、その固定の議題・議題・Canvas の記録を消す. 子の記録を先に消し、会議は最後に消す（途中で止まっても、もう一度消せる）."""
        for table in ("fixed_items", "drafts", "canvases"):
            spec = TABLES[table]
            with self.tables[table].batch_writer() as batch:
                for item in self._query(table, meeting_id):
                    batch.delete_item(Key={spec["pk"]: item[spec["pk"]], spec["sk"]: item[spec["sk"]]})
        self.tables["meetings"].delete_item(Key={"meeting_id": meeting_id})

    def meetings_in_channel(self, channel_id: str) -> list[Meeting]:
        """チャンネルの会議. GSI channel_id-index を Query する（そのチャンネルの会議だけを読む）.

        GSI の無い古いテーブルでは、警告して従来どおり Scan する（scripts/create_tables.py をもう一度実行すると GSI を足す）。
        GSI は結果整合なので、登録の直後（1 秒未満）は一覧に出ないことがある。
        """
        from boto3.dynamodb.conditions import Attr, Key
        from botocore.exceptions import ClientError

        if self._channel_index is not False:
            try:
                items = self._paginate(
                    "meetings", "query", {"IndexName": CHANNEL_INDEX, "KeyConditionExpression": Key("channel_id").eq(channel_id)}
                )
                self._channel_index = True
                return [_build(Meeting, v) for v in items]
            except ClientError as e:
                # GSI が無い・作成中: DynamoDB は ValidationException（DynamoDB Local・moto は ResourceNotFoundException のことがある）
                if e.response.get("Error", {}).get("Code") not in ("ValidationException", "ResourceNotFoundException"):
                    raise
                self._channel_index = False
                log.warning(
                    "%s に GSI %s がありません。会議の全件を読みます（scripts/create_tables.py で足せます）",
                    self.tables["meetings"].name,
                    CHANNEL_INDEX,
                )
        items = self._paginate("meetings", "scan", {"FilterExpression": Attr("channel_id").eq(channel_id)})
        return [_build(Meeting, v) for v in items]

    def _paginate(self, table: str, op: str, kw: dict[str, Any]) -> list[dict]:
        items: list[dict] = []
        while True:
            res = getattr(self.tables[table], op)(**kw)
            items += res.get("Items", [])
            if "LastEvaluatedKey" not in res:
                return items
            kw = {**kw, "ExclusiveStartKey": res["LastEvaluatedKey"]}

    def put_fixed(self, f: FixedItem) -> None:
        self.tables["fixed_items"].put_item(Item=to_dict(f))

    def delete_fixed(self, meeting_id: str, item_id: str) -> None:
        self.tables["fixed_items"].delete_item(Key={"meeting_id": meeting_id, "item_id": item_id})

    def fixed_items(self, meeting_id: str) -> list[FixedItem]:
        return [_build(FixedItem, v) for v in self._query("fixed_items", meeting_id)]

    def put_draft(self, d: DraftItem) -> None:
        self.tables["drafts"].put_item(Item=_draft_item(d))

    def put_draft_if_schedule(self, d: DraftItem, schedule_version: int) -> bool:
        """会議があり、その schedule_version が渡した版のままのときだけ議題を書く（TransactWriteItems: 会議の条件確認と議題の Put を 1 回で）.

        会議の変更（update_schedule）と同時に出された議題が、古い開催日に残らないようにする。書けたら True.
        """
        from botocore.exceptions import ClientError

        # resource の client（meta.client）は Python の値を DynamoDB の型に自動で直すので、値はそのまま渡す
        try:
            self.db.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.tables["meetings"].name,
                            "Key": {"meeting_id": d.meeting_id},
                            "ConditionExpression": "attribute_exists(meeting_id) AND " + _version_condition(schedule_version),
                            "ExpressionAttributeValues": {":v": schedule_version},
                        }
                    },
                    {"Put": {"TableName": self.tables["drafts"].name, "Item": _draft_item(d)}},
                ]
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons") or []]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                return False
            raise
        return True

    def delete_draft(self, meeting_id: str, meeting_date: str, item_id: str) -> None:
        self.tables["drafts"].delete_item(Key={"meeting_id": meeting_id, "sk": f"{meeting_date}#{item_id}"})

    def drafts(self, meeting_id: str, meeting_date: str) -> list[DraftItem]:
        return [_build(DraftItem, v) for v in self._query("drafts", meeting_id, f"{meeting_date}#")]

    def drafts_before(self, meeting_id: str, meeting_date: str, since: str = "", consistent: bool = False) -> list[DraftItem]:
        """meeting_date より前（since より後）の開催日の議題（扱い済みの accepted を除く）.

        ソートキーの範囲（キー条件）で、確定・中止を済ませた開催日より前を読まない（扱い済みの議題が増えても読む量が増えない）。
        consistent=True は強い整合性の読み込み（会議の変更で、直前に書かれた議題も漏らさず付け替えるため）。
        """
        from boto3.dynamodb.conditions import Attr, Key

        lo, hi = sk_range(meeting_date, since)
        kw: dict[str, Any] = {
            "KeyConditionExpression": Key("meeting_id").eq(meeting_id) & Key("sk").between(lo, hi),
            "FilterExpression": Attr("status").ne("accepted"),
        }
        if consistent:
            kw["ConsistentRead"] = True
        items: list[dict] = []
        while True:
            res = self.tables["drafts"].query(**kw)
            items += res.get("Items", [])
            if "LastEvaluatedKey" not in res:
                return [_build(DraftItem, v) for v in items]
            kw["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def mark_settled(self, meeting_id: str, meeting_date: str) -> None:
        """確定・中止を済ませた最後の開催日を記録する（新しい日付のときだけ. 会議のほかの項目は書き換えない）."""
        from botocore.exceptions import ClientError

        try:
            self.tables["meetings"].update_item(
                Key={"meeting_id": meeting_id},
                UpdateExpression="SET settled_through = :d",
                ConditionExpression="attribute_exists(meeting_id) AND (attribute_not_exists(settled_through) OR settled_through < :d)",
                ExpressionAttributeValues={":d": meeting_date},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

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


def _version_condition(version: int) -> str:
    """schedule_version が version のままか（この版より前に作った会議には属性が無いので、0 は「無い」も含める）."""
    return "(attribute_not_exists(schedule_version) OR schedule_version = :v)" if version == 0 else "schedule_version = :v"


def _channel_index_spec() -> dict[str, Any]:
    return {
        "IndexName": CHANNEL_INDEX,
        "KeySchema": [{"AttributeName": "channel_id", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "ALL"},
    }


def create_tables(resource: Any, prefix: str = "agenda_") -> list[str]:
    """テーブルが無ければ作る（オンデマンド課金）. 作ったテーブル名を返す. 既にある meetings に GSI が無ければ足す."""
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
        extra: dict[str, Any] = {}
        if name == "meetings":
            attrs.append({"AttributeName": "channel_id", "AttributeType": "S"})
            extra["GlobalSecondaryIndexes"] = [_channel_index_spec()]
        resource.create_table(
            TableName=full, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST", **extra
        ).wait_until_exists()
        made.append(full)
    ensure_channel_index(resource, prefix + "meetings")
    enable_ttl(resource, prefix + "drafts")
    return made


def ensure_channel_index(resource: Any, table_name: str) -> bool:
    """meetings に GSI channel_id-index が無ければ足す（この版より前に作ったテーブル向け）. → 今回足したか.

    足した直後は既存の項目を GSI に写す間（数分）「作成中」で、その間ボットは従来どおり全件を読む。
    """
    client = resource.meta.client
    desc = client.describe_table(TableName=table_name)["Table"]
    if any(g.get("IndexName") == CHANNEL_INDEX for g in desc.get("GlobalSecondaryIndexes") or []):
        return False
    client.update_table(
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": "channel_id", "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[{"Create": _channel_index_spec()}],
    )
    return True


def enable_ttl(resource: Any, table_name: str) -> bool:
    """drafts の TTL（expires_at）を有効にする. 既に有効なら何もしない. → 今回有効にしたか."""
    client = resource.meta.client
    desc = client.describe_time_to_live(TableName=table_name).get("TimeToLiveDescription", {})
    if desc.get("AttributeName") == TTL_ATTRIBUTE and desc.get("TimeToLiveStatus") in ("ENABLED", "ENABLING"):
        return False
    client.update_time_to_live(TableName=table_name, TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE})
    return True
