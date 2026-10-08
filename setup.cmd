@echo off
rem セットアップ（Windows）: .venv、DynamoDB Local、テーブルの作成
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m venv .venv || goto :err
.venv\Scripts\python -m pip install -q --upgrade pip
rem constraints.txt（GitHub Actions の deps-lock が作る）があれば、依存の版を CI と同じにする
set C=
if exist constraints.txt set C=-c constraints.txt
.venv\Scripts\python -m pip install -q %C% -e ".[dev]" || goto :err
if not exist .env copy .env.example .env >nul
docker compose -f local\docker-compose.yml up -d || goto :err
timeout /t 3 >nul
.venv\Scripts\python scripts\create_tables.py || goto :err
echo 完了: .env に Slack のトークンを書いてから .venv\Scripts\agenda-bot
pause
exit /b 0
:err
echo セットアップに失敗しました。Python 3.12・Git・Docker Desktop（または Rancher Desktop）を確認してください。
pause
exit /b 1
