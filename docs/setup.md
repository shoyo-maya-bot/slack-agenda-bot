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

| AGENDA_ADMINS | 会議の登録 | 固定の議題の追加と削除・Canvas への出力・過去の会議の確定と中止 | 会議の変更（曜日・時刻・長さ・登録者の引き継ぎ）と削除 | 議題の取り下げ | 議題を出す・見る |
|---|---|---|---|---|---|
| 空（既定） | 誰でもできる | **その会議の登録者だけ** | その会議の登録者 | 出した人・会議の登録者 | 誰でもできる |
| `U0123ABCD,U0456EFGH` | 書いたユーザーだけ | 書いたユーザーだけ | 会議の登録者と書いたユーザー | 出した人・会議の登録者・書いたユーザー | 誰でもできる |

ユーザー ID は Slack のプロフィールの「︙ → メンバー ID をコピー」で分かります。ID の形でない値は警告を出して使わず、1 つも使えなければ起動時に止まります。

#### 登録者がいなくなった会議（管理者による引き継ぎ）

`AGENDA_ADMINS` が空で、会議の登録者が異動・退職などでいなくなると、その会議の確定・中止・Canvas への出力・固定の議題は誰もできません。
次の手順で、別の人に登録者を引き継ぎます。

1. `.env` の `AGENDA_ADMINS` に、引き継ぎをする人のユーザー ID を書いてボットを起動し直す
2. その人が `/agenda` →「会議の変更」→「会議の登録者」で新しい登録者を選んで「変更」（曜日・時刻・長さはそのままでよい）
3. 担当者を置かない運用に戻すなら、`AGENDA_ADMINS` を空に戻して起動し直す

登録者がいるうちなら、登録者自身が 2 の操作で引き継げます（ボットの再起動は要りません）。

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
3. ボットを動かす場所（ECS・EC2 など）に、4 つのテーブルの読み書き権限だけを付ける（下の表）

| 権限 | 使うところ |
|---|---|
| `dynamodb:GetItem`・`PutItem`・`UpdateItem`・`DeleteItem`・`Query`・`BatchWriteItem` | 4 つのテーブル（`agenda_*`）の読み書き |
| `dynamodb:Query` を `agenda_meetings/index/channel_id-index` に | `/agenda` のチャンネルの会議の一覧 |
| `dynamodb:ConditionCheckItem`（`agenda_meetings`） | 議題を出すとき、会議の予定が変わっていないかの確認（TransactWriteItems）。無ければ警告して、会議を読んで確かめてから書く（会議の変更との競合は防ぎきれない） |
| `dynamodb:Scan`（`agenda_meetings`） | GSI の無い古いテーブルのときだけ（GSI を足したら外してよい） |
| `dynamodb:DescribeTable`（任意） | 起動時にテーブルがあるかの確認。無ければ確かめずに起動する |

`scripts/create_tables.py` を実行する人（ボットではない）には、加えて `CreateTable`・`UpdateTable`・`DescribeTable`・
`DescribeTimeToLive`・`UpdateTimeToLive` が要ります。

ボットは起動時にテーブルがあるか確かめ、無ければ（作り忘れ・`TABLE_PREFIX` や `AWS_REGION` の誤り）理由を出して止まります。

| テーブル | キー | 中身 |
|---|---|---|
| `agenda_meetings` | meeting_id（GSI `channel_id-index`: channel_id） | 会議（チャンネル・曜日・時刻・時間・登録者 `owner`・予定の版 `schedule_version`・削除中の印 `deleting`） |
| `agenda_fixed_items` | meeting_id + item_id | 固定の議題 |
| `agenda_drafts` | meeting_id + sk（開催日#item_id） | 出された議題（status: proposed / accepted＝扱い済み / deferred＝繰り越し）。TTL 属性 `expires_at` |
| `agenda_canvases` | meeting_id + meeting_date | 開催日ごとの Canvas の ID（空は作成中） |

### チャンネルの会議の一覧（GSI）

`/agenda` は、`agenda_meetings` の GSI `channel_id-index` を Query して、そのチャンネルの会議だけを読みます（テーブル全体を Scan しない）。

- この版より前に作ったテーブルには GSI がありません。**`python scripts/create_tables.py --aws` をもう一度実行すると GSI だけ足します**
  （既存の会議を GSI に写す間、数分は「作成中」です）。GSI が無い・作成中の間、ボットは警告を出して従来どおり Scan します。
  作成が終わったらボットを起動し直すと GSI を使います
- GSI は結果整合なので、会議を登録した直後（1 秒未満）は `/agenda` の一覧に出ないことがあります

### 会議の変更と議題の追加が重なったとき

会議には予定の版（`schedule_version`。曜日・時刻・長さを変えるたびに 1 増える）があり、議題は「会議を読んだときの版のまま」のときだけ書きます
（会議の条件確認と議題の書き込みを TransactWriteItems で 1 回に）。会議の変更と同時に出された議題は、新しい予定の開催日に出し直すので、
古い開催日に残って付け替えから漏れることはありません。同時に 2 人が会議を変更したときは、後の人に「開き直してください」と返します。
この版より前に作った会議（`schedule_version` が無い）は版 0 として扱うので、データの移行は要りません。

### 会議の削除と議題の追加が重なったとき

会議を消すときは、先に会議に削除中の印（`deleting`）を付け、印の付いた会議には議題を書きません（上と同じ条件付きの書き込み）。
その後で固定の議題・議題・Canvas の記録を消し、会議を消し、最後にもう一度それらを消します（削除の途中に書かれた固定の議題や繰り越しも残さない）。
途中で止まっても、もう一度削除すれば残りを消します。削除と同時に出した人には「もう一度出してください」と返り、出し直すと「見つかりません」になります。

### 扱い済みの議題の後片付け（TTL）

「確定」で扱い済み（accepted）にした議題には `expires_at`（開催日の 180 日後、UNIX 秒）を入れ、DynamoDB の TTL で自動で消します
（TTL の削除は無料。期限を過ぎてから実際に消えるまで数日かかることがあります）。まだ扱っていない議題・繰り越した議題には入れません。

- `scripts/create_tables.py` は `agenda_drafts` の TTL（属性名 `expires_at`）も有効にします。**既にテーブルがある環境でも、もう一度実行すれば TTL だけ有効にします**
  （手で設定するなら、コンソールの「テーブル → 追加の設定 → Time to Live」か
  `aws dynamodb update-time-to-live --table-name agenda_drafts --time-to-live-specification "Enabled=true,AttributeName=expires_at"`）。
- 残す日数を変えるときは `agenda_bot/model.py` の `ACCEPTED_RETENTION_DAYS` を変えます（変えた後に確定した議題から効きます）。
- この版より前に扱い済みにした議題には `expires_at` がありません。消すなら、`status = accepted` の項目をコンソールの「項目を探索」で絞り込んで削除します
  （残しておいても動きは変わりません。会議ごとに確定・中止を済ませた最後の開催日（`agenda_meetings` の `settled_through`）より前は読まないため）。
- アジェンダを開くたびに読むのは、確定・中止を済ませた開催日より後の議題だけです（ソートキーの範囲で絞るので、扱い済みの議題が増えても読む量は増えません）。

DynamoDB の料金は使った分だけ（オンデマンド）です。定例会議のアジェンダ程度の読み書きなら、無料利用枠に収まることが多いですが、
最新の料金は AWS の料金ページで確認してください。

## 3. 起動

```bash
agenda-bot          # または python -m agenda_bot
```

Socket Mode なので、公開 URL やファイアウォールの穴あけは不要です（外向きの HTTPS だけ）。

## 4. 本番運用

### データの保護（DynamoDB のポイントインタイムリカバリ）

誤って議題や会議を消したときに戻せるよう、4 つのテーブルで **ポイントインタイムリカバリ（PITR）** を有効にします（直近 35 日の任意の時点に戻せる。
保存しているデータ量に応じた料金がかかるので、料金ページで確認してください）。

```bash
for t in meetings fixed_items drafts canvases; do
  aws dynamodb update-continuous-backups --table-name "agenda_$t" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true
done
```

戻すときは別名のテーブルに復元され（`aws dynamodb restore-table-to-point-in-time`）、元のテーブルは変わりません。
復元したテーブルを使うには `.env` の `TABLE_PREFIX` を合わせるか、必要な項目だけ元のテーブルへ書き戻します。TTL の設定は復元先に引き継がれないので、
`scripts/create_tables.py --aws --prefix <復元先の接頭辞>` で付け直します。

### 常駐と再起動

ボットは Socket Mode の WebSocket をつなぎっぱなしにする常駐プロセスです。**同じアプリトークンで動かすのは 1 つだけ** にします
（複数起動すると、イベントがどちらか一方に届くため、動きが追いにくくなります）。落ちたら自動で起動し直すようにします。

- ECS（Fargate）: サービスの「必要なタスク数」を 1 にする（タスクが止まると ECS が起動し直す）。タスクロールに 4 つのテーブルの読み書き権限だけを付ける。
- Linux のサーバー: systemd のユニットにする。

```ini
# /etc/systemd/system/agenda-bot.service
[Unit]
Description=Slack agenda bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/slack-agenda-bot          # .env を置いたフォルダ
ExecStart=/opt/slack-agenda-bot/.venv/bin/agenda-bot
Restart=always
RestartSec=10
User=agenda-bot

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now agenda-bot
sudo systemctl restart agenda-bot    # 更新（git pull と pip install の後）
```

設定（`.env`）の誤りでは起動時に「設定の誤り: …」と出して止まります。再起動を繰り返す場合はまずこれを確認してください。
接続が切れたときは slack_bolt が自動でつなぎ直すので、プロセスを再起動する必要はありません。

### ログの見方

ログは標準エラーに出ます（ECS なら CloudWatch Logs、systemd なら `journalctl -u agenda-bot -f`）。`.env` の `LOG_LEVEL`（既定 `INFO`）で量を変えます。

| ログ | 意味と対処 |
|---|---|
| `timezone: … / canvas mode: … / admins: … / error channel: …` | 起動時の設定。起動するたびに 1 回出る（`admins` は `AGENDA_ADMINS` の人数。空なら「なし（確定・中止などは各会議の登録者）」） |
| `ハンドラーで想定外のエラー（action:… など）` | 操作が失敗した（スタックトレース付き）。押した本人と、`ERROR_CHANNEL` があればそこにも知らせている |
| `Canvas の ID の保存に失敗しました（n/3 回目）` | DynamoDB への書き込みが一時的に失敗した。続くなら権限・スロットリングを確認 |
| `記録した Canvas … が見つかりません` | Slack 側で Canvas が消された。記録を消して作り直したので対処は不要 |
| `canvas failed` | Canvas の作成・更新が失敗した（権限・プラン）。上の「Canvas とプラン」を確認 |
| `AGENDA_ADMINS の「…」は Slack のユーザー ID ではない` | 設定の書き間違い。その値は使われていない |

ログには議題の本文やトークンを出しません（例外の種類と操作の名前だけ）。障害の調査で `LOG_LEVEL=DEBUG` にした場合は、slack_bolt が受け取った内容を出すことがあるので、調査が終わったら戻してください。
