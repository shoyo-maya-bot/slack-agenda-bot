# セットアップ

## 1. Slack アプリを作る

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → ワークスペースを選び、`manifest.yml` の中身を貼る
2. **Basic Information → App-Level Tokens** で `connections:write` のトークンを作る → `.env` の `SLACK_APP_TOKEN`（xapp-…）
3. **Install App** でワークスペースに入れる → **OAuth & Permissions** の Bot User OAuth Token → `.env` の `SLACK_BOT_TOKEN`（xoxb-…）
4. 使うチャンネルにアプリを招待する（`/invite @アジェンダ係`）

アプリの追加にワークスペースの管理者の承認が要る場合があります。社内のルールに従ってください。
トークンは `.env` にだけ書き、コミット・チャットへの貼り付けをしないでください。
`.env` のコメントは値の前の行に書いてください（値の後ろの ` # …` は外して読みますが、値に ` #` を含めるなら `"…"` で囲みます）。

### 操作できる人（AGENDA_ADMINS）

| AGENDA_ADMINS | 会議の登録・固定の議題の追加と削除・Canvas への出力・過去の会議の確定と中止 | 議題を出す・見る・取り下げ |
|---|---|---|
| 空（既定） | 誰でもできる | 誰でもできる |
| `U0123ABCD,U0456EFGH` | 書いたユーザーだけ | 誰でもできる（取り下げは出した人と会議の担当者） |

ユーザー ID は Slack のプロフィールの「︙ → メンバー ID をコピー」で分かります。ID の形でない値は警告を出して使わず、1 つも使えなければ起動時に止まります。

### Canvas とプラン

Canvas を API で作る・更新するには Slack の **有料プラン** が必要です（2026-10 時点の Slack API ドキュメント）。

| CANVAS_MODE | 動き | 必要なもの |
|---|---|---|
| `message`（既定） | アジェンダをチャンネルにメッセージで投稿 | なし（無料プランでも可） |
| `channel` | チャンネルの Canvas（1 チャンネル 1 つ）を毎回置き換える | 有料プラン、`canvases:write`。チャンネルに Canvas が既にあると `channel_canvas_already_exists` |
| `standalone` | 開催日ごとに Canvas を作り、チャンネルに編集権限を付ける | 有料プラン、`canvases:write` |

Canvas は、作った直後に ID を保存してから権限を付けます。ID の保存が何度やっても失敗したときは、作った Canvas を消して「もう一度押してください」と出します
（消せなかったときは Canvas の ID を出すので、手で消してから押し直してください。いずれも 2 つ目は作りません）。権限の付与に失敗しても、もう一度押せば同じ Canvas を更新して権限を付け直します（重複して作りません）。
同時に押された・連打されたときは 1 人だけが作り、ほかの人には「作成中です」と出します。作成が途中で止まった場合は 5 分たつと作り直せます。

### タイムゾーン

「今日」と次の開催日、議題を出した時刻は `.env` の `TIMEZONE` で決めます（IANA の名前。未設定なら `Asia/Tokyo`）。
サーバー（ECS・EC2 など）の時計が UTC でも、この設定の日付で動きます。

| TIMEZONE | 使いどころ |
|---|---|
| `Asia/Tokyo`（既定） | 日本の会議 |
| `America/New_York` など | ほかの地域の会議（ボット 1 つにつき 1 つのタイムゾーン） |

名前を間違えると起動時に止まります。Windows は `pip install` で `tzdata` も入ります。

## 2. DynamoDB

### ローカル（DynamoDB Local）

Docker が使える環境（Rancher Desktop など無料のものでよい）で:

```bash
docker compose -f local/docker-compose.yml up -d
python scripts/create_tables.py          # .env の DYNAMODB_ENDPOINT（http://localhost:8000）に 4 つのテーブルを作る
```

データは `local/data/` に残ります（コミットしません）。

### AWS

1. `.env` の `DYNAMODB_ENDPOINT` を空にし、AWS の認証情報は IAM ロール（ECS のタスクロールなど）か AWS CLI のプロファイル（`AWS_PROFILE`）を使う。
   アクセスキーを `.env` に書かない（DynamoDB Local のときは、ボットがダミーの値を自動で使う）
2. `python scripts/create_tables.py --aws`（オンデマンド課金のテーブルを作る）
3. ボットを動かす場所（ECS・EC2 など）に、4 つのテーブルの読み書き権限だけを付ける

| テーブル | キー | 中身 |
|---|---|---|
| `agenda_meetings` | meeting_id | 会議（チャンネル・曜日・時刻・時間・担当者） |
| `agenda_fixed_items` | meeting_id + item_id | 固定の議題 |
| `agenda_drafts` | meeting_id + sk（開催日#item_id） | 出された議題（status: proposed / accepted＝扱い済み / deferred＝繰り越し） |
| `agenda_canvases` | meeting_id + meeting_date | 開催日ごとの Canvas の ID（空は作成中） |

DynamoDB の料金は使った分だけ（オンデマンド）です。定例会議のアジェンダ程度の読み書きなら、無料利用枠に収まることが多いですが、
最新の料金は AWS の料金ページで確認してください。

## 3. 起動

```bash
agenda-bot          # または python -m agenda_bot
```

Socket Mode なので、公開 URL やファイアウォールの穴あけは不要です（外向きの HTTPS だけ）。
