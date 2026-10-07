# slack-agenda-bot

定例会議の **アジェンダを Slack で集めて、まとめる** ボットです。

- `/agenda` で本人にだけメニューが出る: **議題を出す** ／ **次回のアジェンダ** ／ **固定の議題** ／ **Canvas に出す**
- 毎回の議題（固定の議題）と、メンバーが出した議題（下書き）を、次の開催日のアジェンダにまとめる
- 会議時間を超える議題は「次回へ」に回す（出した順）。開催日が過ぎると、次の開催日のアジェンダの先頭側に **繰り越す**（「繰越」と表示）
- 開催日の会議が終わった後（開始時刻＋会議時間を過ぎた後）に出した議題は、翌週の会議に入る
- 議題は出した人と会議の担当者が取り下げられる。固定の議題は「固定の議題」の画面で追加・削除できる
- 会議の登録・固定の議題・Canvas への出力ができる人を、`.env` の `AGENDA_ADMINS`（Slack のユーザー ID）で絞れる。空なら誰でもできる
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

`.env` の値の後ろにコメントを書いても、空白と `#` から後ろは外して読みます（値に ` #` を含めたいときは `"…"` で囲む）。
必須の設定（`SLACK_BOT_TOKEN`・`SLACK_APP_TOKEN`）が無い、`CANVAS_MODE` が誤っている、などのときは、足りない設定の名前を出して止まります。

### 繰り越しの決まり

| いつ | どうなる |
|---|---|
| アジェンダを開く・Canvas に出すとき、過去の開催日に議題が残っている | その日のアジェンダを今の固定の議題で組み直し、収まった議題は「扱い済み」、収まらなかった議題は次の開催日へ移す |
| 移した議題 | 次の開催日のアジェンダで、新しく出た議題より先に並ぶ（「繰越」）。取り下げもできる |
| 何週も使わなかった | 収まらなかった議題は、次に開くアジェンダに移る（黙って消さない） |

### 入力の決まり

| 項目 | 決まり |
|---|---|
| 議題・会議名 | 100 文字まで |
| 補足 | 2000 文字まで |
| `<!channel>`・`<@U…>` などの特殊な書式 | メッセージでは `&` `<` `>` をエスケープし、Canvas では `<` `>` を全角にして、メンションにならないようにする |

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

依存の版は `constraints.txt` で固定しています（GitHub Actions の deps-lock が pyproject.toml から作る）。
同じ版で入れるときは `pip install -e ".[dev]" -c constraints.txt` です。CI は Linux と Windows でテストし、bandit と pip-audit も実行します。

テストは保存先をメモリと moto（DynamoDB の模擬）、Slack を偽のクライアントにして動き、外部には接続しません。
CI では DynamoDB Local のコンテナでもテーブルの作成と読み書きを確かめます。

## 変更点

| 版 | 変わったこと | 利用者がすること |
|---|---|---|
| 2026-10 | `CANVAS_MODE` の既定を、コードでもドキュメントと同じ `message` にした（前はコードだけ `standalone`） | Canvas を使っていて `.env` に `CANVAS_MODE` を書いていない場合は、`CANVAS_MODE=standalone` を書く |
| 2026-10 | 収まらなかった議題を次の開催日に繰り越す。会議が終わった後に出した議題は翌週に入る | なし |
| 2026-10 | `AGENDA_ADMINS` を追加（空なら今までどおり） | 絞りたいときだけ設定する |
| 2026-10 | `.env.example` の行末コメントをやめた。`.env` の行末コメントは外して読む | なし |

## セキュリティ・ライセンス

- 脆弱性を見つけたら、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で知らせてください
- ライセンスは [MIT](LICENSE) です
