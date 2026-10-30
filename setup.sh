#!/usr/bin/env bash
# セットアップ（macOS / Linux / Git Bash）: .venv、DynamoDB Local、テーブルの作成
set -euo pipefail
cd "$(dirname "$0")"
# Git Bash（Windows）には python3 が無いことがあり、venv の中は .venv/Scripts になる
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "Python 3.12 以上が要ります（$("$PY" -V 2>&1)）" >&2; exit 1; }
"$PY" -m venv .venv
BIN=.venv/bin; [ -d .venv/Scripts ] && BIN=.venv/Scripts
"$BIN/python" -m pip install -q --upgrade pip || true
# constraints.txt（GitHub Actions の deps-lock が作る）があれば、依存の版を CI と同じにする
# （macOS の bash 3.2 は set -u で空の配列を展開できないので、配列ではなく文字列にする）
c=""; [ -f constraints.txt ] && c="-c constraints.txt"
# shellcheck disable=SC2086
"$BIN/python" -m pip install -q $c -e ".[dev]"
[ -f .env ] || cp .env.example .env
docker compose -f local/docker-compose.yml up -d
sleep 3
"$BIN/python" scripts/create_tables.py
echo "完了: .env に Slack のトークンを書いてから $BIN/agenda-bot"
