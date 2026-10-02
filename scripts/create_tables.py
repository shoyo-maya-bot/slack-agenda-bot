"""DynamoDB のテーブル（4 つ）を作る. 既にあるものは作らない.

python scripts/create_tables.py                                  # .env の DYNAMODB_ENDPOINT（DynamoDB Local）へ
python scripts/create_tables.py --endpoint http://localhost:8000
python scripts/create_tables.py --aws                            # AWS の DynamoDB へ（認証情報は AWS CLI の設定）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agenda_bot.app import load_env  # noqa: E402
from agenda_bot.repository import create_tables  # noqa: E402


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default=os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8000"))
    ap.add_argument("--aws", action="store_true", help="AWS の DynamoDB に作る（endpoint を使わない）")
    ap.add_argument("--prefix", default=os.environ.get("TABLE_PREFIX", "agenda_"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    args = ap.parse_args()
    import boto3

    kw = {"region_name": args.region}
    if not args.aws:
        kw["endpoint_url"] = args.endpoint
        # DynamoDB Local は認証情報を検証しないが、boto3 は値を必要とする
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    made = create_tables(boto3.resource("dynamodb", **kw), args.prefix)
    print("作成: " + (", ".join(made) if made else "なし（すべて作成済み）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
