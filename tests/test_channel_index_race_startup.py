"""/agenda の会議一覧を GSI で引く（全件 Scan しない）・会議の変更と議題の追加が競合しても議題が古い開催日に残らない・起動時のテーブル確認."""

from __future__ import annotations

import logging
from datetime import date

import pytest

from agenda_bot import service
from agenda_bot.model import Meeting
from agenda_bot.repository import CHANNEL_INDEX, TABLES, MemoryRepository

TODAY = date(2026, 10, 2)  # 金曜. 月曜の会議の次の開催日は 10/5、火曜にすると 10/6
OWNER = "UOWNER"


@pytest.fixture()
def aws(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        yield boto3.resource("dynamodb", region_name="ap-northeast-1")


def _legacy_tables(res, prefix: str = "agenda_") -> None:
    """この版より前の create_tables（GSI なし）と同じテーブルを作る."""
    for name, spec in TABLES.items():
        keys = [{"AttributeName": spec["pk"], "KeyType": "HASH"}]
        attrs = [{"AttributeName": spec["pk"], "AttributeType": "S"}]
        if "sk" in spec:
            keys.append({"AttributeName": spec["sk"], "KeyType": "RANGE"})
            attrs.append({"AttributeName": spec["sk"], "AttributeType": "S"})
        res.create_table(TableName=prefix + name, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST")


# --- /agenda: GSI で引く ---


def test_meetings_in_channel_queries_gsi_without_scan(aws, monkeypatch):
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    repo = DynamoRepository(resource=aws)
    assert repo.check_tables() == []
    a = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    b = service.register_meeting(repo, "C1", OWNER, "週次", 2, "15:00", 30)
    service.register_meeting(repo, "C2", OWNER, "別のチャンネル", 1, "10:00", 30)

    def no_scan(**kw):
        raise AssertionError("meetings を Scan した")

    monkeypatch.setattr(repo.tables["meetings"], "scan", no_scan)
    assert sorted(m.meeting_id for m in repo.meetings_in_channel("C1")) == sorted([a.meeting_id, b.meeting_id])
    assert repo.meetings_in_channel("C9") == []


def test_legacy_table_without_gsi_falls_back_and_create_tables_adds_it(aws, caplog):
    from agenda_bot.repository import DynamoRepository, create_tables, ensure_channel_index

    _legacy_tables(aws)
    repo = DynamoRepository(resource=aws)
    warnings = repo.check_tables()
    assert len(warnings) == 1 and CHANNEL_INDEX in warnings[0]
    m = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 30)
    assert repo.meetings_in_channel("C1") == [m]  # GSI が無くても従来どおり（Scan で）動く

    fresh = DynamoRepository(resource=aws)  # check_tables を呼ばずに使っても、Query の失敗から Scan に切り替える
    with caplog.at_level(logging.WARNING, logger="agenda_bot"):
        assert fresh.meetings_in_channel("C1") == [m]
    assert CHANNEL_INDEX in caplog.text

    assert create_tables(aws) == []  # テーブルは作らず、GSI だけ足す
    gsis = aws.meta.client.describe_table(TableName="agenda_meetings")["Table"].get("GlobalSecondaryIndexes") or []
    assert [g["IndexName"] for g in gsis] == [CHANNEL_INDEX]
    assert ensure_channel_index(aws, "agenda_meetings") is False  # 2 回目は何もしない


def test_new_tables_have_gsi(aws):
    from agenda_bot.repository import create_tables

    create_tables(aws, "t_")
    gsis = aws.meta.client.describe_table(TableName="t_meetings")["Table"]["GlobalSecondaryIndexes"]
    assert [(g["IndexName"], g["KeySchema"][0]["AttributeName"]) for g in gsis] == [(CHANNEL_INDEX, "channel_id")]


# --- 起動時のテーブル確認 ---


def test_check_tables_raises_when_tables_missing(aws):
    from agenda_bot.repository import DynamoRepository, MissingTablesError

    with pytest.raises(MissingTablesError, match="agenda_meetings.*create_tables.py"):
        DynamoRepository(resource=aws).check_tables()


def test_check_storage_exits_with_hint(aws):
    from agenda_bot.app import check_storage
    from agenda_bot.repository import DynamoRepository

    with pytest.raises(SystemExit) as e:
        check_storage(DynamoRepository(prefix="typo_", resource=aws))
    assert "typo_meetings" in str(e.value) and "TABLE_PREFIX" in str(e.value)


def test_check_tables_continues_without_describe_permission():
    from botocore.exceptions import ClientError

    from agenda_bot.repository import DynamoRepository

    class Client:
        def describe_table(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DescribeTable")

    class Res:
        class meta:  # noqa: N801 — boto3 の resource.meta.client の形
            client = Client()

        def Table(self, name):  # noqa: N802 — boto3 の名前
            return type("T", (), {"name": name})()

    warnings = DynamoRepository(resource=Res()).check_tables()
    assert len(warnings) == 1 and "DescribeTable" in warnings[0]


# --- 会議の変更と議題の追加の競合 ---


def _race_cases(repo):
    """古い会議（変更前に読んだもの）で議題を出しても、新しい開催日に入る. 消えた会議には出さない."""
    stale = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 60)  # 月曜
    service.update_meeting(repo, stale, OWNER, "1", "10:00", "60", TODAY)  # 火曜に変更（stale は月曜のまま）
    d = service.add_draft(repo, stale, TODAY, "変更と同時に出した議題", "", 10, "", "UA")
    assert d.meeting_date == "2026-10-06"  # 変更後の開催日
    assert repo.drafts(stale.meeting_id, "2026-10-05") == []  # 古い開催日に残らない
    assert [x.title for x in repo.drafts(stale.meeting_id, "2026-10-06")] == ["変更と同時に出した議題"]
    assert repo.get_meeting(stale.meeting_id).schedule_version == 1

    repo.delete_meeting(stale.meeting_id)
    with pytest.raises(service.ValidationError, match="見つかりません"):
        service.add_draft(repo, stale, TODAY, "消えた会議", "", 10, "", "UA")
    assert repo.drafts_before(stale.meeting_id, "9999-12-31") == []  # 消えた会議の議題を作らない


def test_add_draft_with_stale_meeting_memory():
    _race_cases(MemoryRepository())


def test_add_draft_with_stale_meeting_dynamo(aws):
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    _race_cases(DynamoRepository(resource=aws))


class AddsDuringUpdate(MemoryRepository):
    """update_meeting が議題を読んだ直後（予定を書き換える前）に、別の人が古い予定で議題を出す."""

    def __init__(self):
        super().__init__()
        self.hook = None

    def drafts_before(self, *args, **kw):
        rows = super().drafts_before(*args, **kw)
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        return rows


def test_draft_added_during_update_is_moved():
    repo = AddsDuringUpdate()
    m = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 60)
    service.add_draft(repo, m, TODAY, "前からある議題", "", 10, "", "UA")
    added: list = []
    repo.hook = lambda: added.append(service.add_draft(repo, m, TODAY, "途中で出た議題", "", 10, "", "UB"))
    new, moved = service.update_meeting(repo, m, OWNER, "1", "10:00", "60", TODAY)
    assert added and added[0].meeting_date == "2026-10-05"  # 予定を書き換える前なので古い開催日に出た
    assert sorted(x.title for x in moved) == ["前からある議題", "途中で出た議題"]  # 書き換えた後に読み直して付け替える
    assert repo.drafts(m.meeting_id, "2026-10-05") == []
    assert sorted(x.title for x in repo.drafts(m.meeting_id, "2026-10-06")) == ["前からある議題", "途中で出た議題"]


def test_concurrent_update_meeting_is_rejected():
    repo = AddsDuringUpdate()
    m = service.register_meeting(repo, "C1", OWNER, "定例", 0, "10:00", 60)
    # 1 人目が予定を読んだ後、書き換える前に 2 人目が変更を終える
    repo.hook = lambda: service.update_meeting(repo, m, OWNER, "2", "10:00", "60", TODAY)
    with pytest.raises(service.ValidationError, match="同時にほかの人が会議を変更しました"):
        service.update_meeting(repo, m, OWNER, "1", "10:00", "60", TODAY)
    got = repo.get_meeting(m.meeting_id)
    assert (got.weekday, got.schedule_version) == (2, 1)  # 2 人目の変更が残る（1 人目が上書きしない）


def test_update_schedule_expected_version(aws):
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    for repo in (MemoryRepository(), DynamoRepository(resource=aws)):
        repo.put_meeting(Meeting("m1", "C1", "定例", 0, "10:00", OWNER))
        assert repo.update_schedule("m1", 1, "11:00", 30, expected_version=1) is False
        assert repo.update_schedule("m1", 1, "11:00", 30, expected_version=0) is True
        assert repo.update_schedule("m1", 2, "11:00", 30, expected_version=0) is False  # 版が上がった
        assert repo.update_schedule("m1", 2, "11:00", 30) is True  # 版を指定しなければ従来どおり
        got = repo.get_meeting("m1")
        assert (got.weekday, got.schedule_version) == (2, 2)


def test_legacy_meeting_without_version_attribute(aws):
    """この版より前に作った会議（schedule_version が無い）にも議題を出せ、変更もできる."""
    from agenda_bot.repository import DynamoRepository, create_tables

    create_tables(aws)
    repo = DynamoRepository(resource=aws)
    repo.tables["meetings"].put_item(
        Item={"meeting_id": "old", "channel_id": "C1", "name": "定例", "weekday": 0, "time": "10:00", "owner": OWNER, "default_minutes": 60}
    )
    m = repo.get_meeting("old")
    assert m is not None and m.schedule_version == 0
    assert service.add_draft(repo, m, TODAY, "議題", "", 10, "", "UA").meeting_date == "2026-10-05"
    new, moved = service.update_meeting(repo, m, OWNER, "1", "10:00", "60", TODAY)
    assert new.schedule_version == 1 and [x.meeting_date for x in moved] == ["2026-10-06"]
