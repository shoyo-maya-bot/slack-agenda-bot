#!/usr/bin/env bash
# セットアップ（macOS / Linux / Git Bash）: .venv、DynamoDB Local、テーブルの作成
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip || true
# constraints.txt（GitHub Actions の deps-lock が作る）があれば、依存の版を CI と同じにする
# （macOS の bash 3.2 は set -u で空の配列を展開できないので、配列ではなく文字列にする）
c=""; [ -f constraints.txt ] && c="-c constraints.txt"
# shellcheck disable=SC2086
.venv/bin/python -m pip install -q $c -e ".[dev]"
[ -f .env ] || cp .env.example .env
docker compose -f local/docker-compose.yml up -d
sleep 3
.venv/bin/python scripts/create_tables.py
echo "完了: .env に Slack のトークンを書いてから .venv/bin/agenda-bot"
