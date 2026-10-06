# slack-agenda-bot

定例会議の **アジェンダを Slack で集めて、まとめる** ボットです。

- `/agenda` で本人にだけメニューが出る: **議題を出す** ／ **次回のアジェンダ** ／ **固定の議題** ／ **Canvas に出す**
- 毎回の議題（固定の議題）と、メンバーが出した議題（下書き）を、次の開催日のアジェンダにまとめる
- 会議時間を超える議題は「次回へ」に回す（出した順）。議題は出した人と会議の担当者が取り下げられる
- アジェンダは Canvas（有料プラン）か、チャンネルへのメッセージで共有
- Slack Bolt（Socket Mode）＋ DynamoDB（4 テーブル）。ローカルは DynamoDB Local

```text
/agenda ─▶ メニュー ─▶ 議題を出す（モーダル）─▶ drafts
                    ├▶ 固定の議題（モーダル）─▶ fixed_items
                    ├▶ 次回のアジェンダ ◀── fixed_items + drafts（会議時間で切る）
                    └▶ Canvas に出す ─▶ canvases（開催日ごとの Canvas ID）
```

## すぐ使う

必要なもの: Python 3.12、Git、Docker（Rancher Desktop など）、Slack のワークスペース（アプリを追加できる権限）

1. clone して **Windows**: `setup.cmd` ／ **macOS・Linux**: `./setup.sh`（.venv・DynamoDB Local・テーブルを作る）
2. [docs/setup.md](docs/setup.md) の手順で Slack アプリを作り、`.env` にトークンを書く
3. `agenda-bot` で起動し、チャンネルで `/agenda` → 「会議を登録する」

次の開催日は `.env` の `TIMEZONE`（既定は `Asia/Tokyo`）の日付で決めます。サーバーの時計が UTC でも、日本時間の 0〜9 時に前日扱いになりません。

## 構成

| ファイル | 役割 |
|---|---|
| `src/agenda_bot/clock.py` | 「今日」と「今」（設定したタイムゾーン。既定は Asia/Tokyo） |
| `src/agenda_bot/model.py` | 会議・議題とアジェンダの組み立て（会議時間で切る・Markdown にする） |
| `src/agenda_bot/service.py` | ユースケース（登録・議題の追加と取り下げ・アジェンダ・Canvas への出力）。入力のチェック |
| `src/agenda_bot/views.py` | Slack の画面（Block Kit・モーダル） |
| `src/agenda_bot/repository.py` | 保存先（DynamoDB とメモリ）とテーブルの定義 |
| `src/agenda_bot/app.py` | Bolt との結合（受け渡しだけ） |
| `scripts/create_tables.py` | テーブルの作成（何度実行してもよい） |
| `manifest.yml` | Slack アプリのマニフェスト |

## 開発

```bash
ruff check . && ruff format --check . && pytest
```

テストは保存先をメモリと moto（DynamoDB の模擬）、Slack を偽のクライアントにして動き、外部には接続しません。
CI では DynamoDB Local のコンテナでもテーブルの作成と読み書きを確かめます。

## セキュリティ・ライセンス

- 脆弱性を見つけたら、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で知らせてください
- ライセンスは [MIT](LICENSE) です
