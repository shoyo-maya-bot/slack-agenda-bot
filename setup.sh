#!/usr/bin/env bash
# セットアップ（macOS / Linux / Git Bash）: .venv、DynamoDB Local、テーブルの作成
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip || true
.venv/bin/python -m pip install -q -e ".[dev]"
[ -f .env ] || cp .env.example .env
docker compose -f local/docker-compose.yml up -d
sleep 3
.venv/bin/python scripts/create_tables.py
echo "完了: .env に Slack のトークンを書いてから .venv/bin/agenda-bot"
