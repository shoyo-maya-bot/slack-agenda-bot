# Copilot への指示（slack-agenda-bot）

Slack のアジェンダ収集ボットです。説明は日本語で行ってください。

- 処理は service.py、画面は views.py、保存は repository.py に置く。app.py（Bolt）は受け渡しだけにする
- 保存先を増やす・変えるときは MemoryRepository と DynamoRepository の両方を同じ動きにし、テストは両方で通す
- テーブルを変えたら repository.TABLES、docs/setup.md の表、scripts/create_tables.py の動きを合わせる
- トークンは .env だけに書く。ログに Slack の本文やトークンを出さない
- Slack の API（特に Canvas）はプランで使える機能が違う。新しい API を使うときは公式ドキュメントで必要なスコープとプランを確かめ、manifest.yml と docs/setup.md を直す
- コードを変更したら `ruff check . && ruff format --check . && pytest` を実行して結果を報告する
