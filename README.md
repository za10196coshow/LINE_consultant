# LINEグループ常駐 AI幹事

LINEグループの会話から、企画、日程、参加可否、場所、予算などを自然に整理する第一版です。日程回答には原則返信せず記録だけ行い、「今どんな感じ？」のような依頼時に整理して返します。実在店舗・施設を求められたときだけOpenAI Responses APIのWeb Searchを使います。

## 主な機能

- group / room / 1対1を識別し、会話状態を完全分離
- LINE署名検証、`webhookEventId`による再送・重複防止
- 発言者プロフィール取得（失敗時は安全に継続）
- `IGNORE / REMEMBER_ONLY / REPLY / ORGANIZE / SEARCH / PROPOSE` の型付き判断
- 日本時間を明示した自然な日付解釈と `yes / no / maybe / unknown` 保存
- 直近10件＋構造化状態だけをAIへ渡す省コスト設計
- 必要時だけWeb Searchを使う実在候補検索
- SQLite、FastAPI、pytest、Render Blueprint対応

## 構成

```text
app/
  ai/             OpenAI Responses API
  line/           LINE Messaging API
  models/         Structured Outputs用Pydanticモデル
  prompts/        AI幹事の指示
  repositories/   SQLiteアクセス
  services/       会話処理の調整
  config.py       環境変数
  main.py         FastAPIエンドポイント
tests/            外部APIをモックしたテスト
render.yaml       Render Blueprint
```

## ローカルで動かす

Python 3.12を用意し、次を実行します。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

`.env.example` を `.env` にコピーし、3つの必須値を設定します。秘密値をGitへコミットしないでください。

```bash
uvicorn app.main:app --reload
pytest -q
```

ブラウザで `http://localhost:8000/health` を開き、`{"status":"ok"}` なら起動成功です。LINEからlocalhostへは直接接続できないため、実利用はRender等の公開HTTPS URLが必要です。

## GitHubからRenderへデプロイしてLINEで使う手順

1. **必要サービスを用意**: GitHub、Render、LINE Developers、LINE Official Account、OpenAI Platformの各アカウントを用意します。
2. **LINE Developers設定**: [LINE Developers Console](https://developers.line.biz/console/) でProviderを作成または選択します。
3. **Messaging API設定**: LINE Official Accountを作成し、Messaging APIチャネルを有効化してProviderへ関連付けます。
4. **Channel Secret取得**: チャネルの「Basic settings」にあるChannel secretをコピーし、安全に保管します。
5. **Channel Access Token取得**: 「Messaging API」タブで長期Channel access tokenを発行して安全に保管します。
6. **OpenAI API Key準備**: [OpenAI API Keys](https://platform.openai.com/api-keys) でキーを作成します。ChatGPTの契約とは課金が別なので、APIの請求設定と利用上限も確認します。
7. **Renderアカウント作成**: [Render](https://render.com/) に登録します。
8. **GitHubとRender接続**: このリポジトリをGitHubへpushし、RenderにGitHubリポジトリへのアクセスを許可します。
9. **Web Service作成**: Render Dashboardで「New」→「Blueprint」を選び、このリポジトリの `render.yaml` を読み込みます（通常のWeb Serviceでも可）。
10. **Environment Variables設定**: Renderで下表の必須値を入力します。値をログやREADMEへ貼らないでください。
11. **Deploy**: Blueprintを適用してデプロイ完了を待ちます。開始コマンドは `uvicorn app.main:app --host 0.0.0.0 --port $PORT` です。
12. **Render URL確認**: サービス画面の `https://{render-domain}` を開き、稼働JSONが表示されることを確認します。
13. **Health確認**: `https://{render-domain}/health` を開き、`{"status":"ok"}` を確認します。
14. **LINE Webhook URL設定**: LINE DevelopersのMessaging APIタブで `https://{render-domain}/webhook` をWebhook URLへ設定します。
15. **Verify**: Webhook URL横の「Verify」を押します。イベントが空の確認リクエストにも200を返します。
16. **Use webhook ON**: 「Use webhook」をONにします。可能ならWebhook redeliveryもONにします（本実装は重複排除済み）。
17. **グループ参加許可**: Messaging API設定の「Allow bot to join group chats」をEnabledにします。
18. **グループへ招待**: 対象LINEグループで公式アカウントを招待します。LINEの仕様上、1グループに参加できる公式アカウントは通常1つです。
19. **テスト**: グループで「9月飲もうぜ」→「19か21なら」→「今どんな感じ？」の順に試します。日程回答時に黙るのは仕様です。
20. **トラブルシューティング**: 下の項目を上から確認します。

## Renderで設定する環境変数

| 変数 | 必須 | 内容 / 既定値 |
|---|---:|---|
| `LINE_CHANNEL_SECRET` | 必須 | LINE Basic settingsのChannel secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | 必須 | LINE Messaging APIのaccess token |
| `OPENAI_API_KEY` | 必須 | OpenAI PlatformのAPI key |
| `OPENAI_MODEL` | 任意 | `gpt-5-mini`。利用可能モデルへ変更可能 |
| `OPENAI_TIMEOUT_SECONDS` | 任意 | `45`。OpenAI 1回あたりの待機上限（秒） |
| `OPENAI_SEARCH_TIMEOUT_SECONDS` | 任意 | `75`。Web Search 1回あたりの待機上限（秒） |
| `DATABASE_PATH` | 任意 | `data/kanji.db` |
| `AI_KANJI_NAME` | 任意 | `幹事` |
| `LOG_LEVEL` | 任意 | `INFO` |
| `TIMEZONE` | 任意 | `Asia/Tokyo` |

## Webhookとタイムアウト

LINEのWebhookは署名とJSONを検証した時点で処理を受理し、FastAPIのBackgroundTasksへ渡してすぐにHTTP 200を返します。AI処理の完了後は、有効期限の短いreplyTokenに依存せず、グループ・room・userのIDを宛先としたPush Messageで応答します。外部Queueを使わない第一版のため、プロセス停止や再デプロイの瞬間には受理済みバックグラウンド処理が失われる可能性があります。本格運用では永続Queueへの移行を検討してください。

OpenAIクライアントのタイムアウトは既定45秒です。SDKの自動リトライを無効にし、Timeoutに限ってアプリが1回だけ再試行します。Rate Limit、認証エラー、その他APIエラーは無意味に再試行しません。2回ともTimeoutした場合、質問や集計・検索依頼には短い失敗通知をPushしますが、単なる日程回答では黙ります。

## お店・企画のWeb検索

「店探して」「横浜で焼肉屋探して」「この条件で候補出して」など、実在店舗・施設を探す明示的な発言だけを `SEARCH_VENUE` として扱います。「横浜がいい」「焼肉いいね」「19日行ける」「ありがとう」などの希望・日程・雑談ではWeb Searchを呼びません。

検索時は現在のイベントから、候補日、場所、参加人数、予算、料理ジャンル、雰囲気、開始時刻、その他条件を取り出します。致命的な不足でなければ、ユーザーにすべて言い直してもらわず、そのまま日本向けのOpenAI Responses API built-in Web Searchを実行します。検索は通常会話のStructured Outputとは分離されています。

検索結果はそのままLINEへ送りません。Web Searchの出力をまず `VenueCandidate`（店名、エリア、ジャンル、予算、一言理由、URL、source）へStructured Outputで変換し、source URLと一致する候補を最大3件だけ採用します。その後、通常会話と同じ共通personaを使った別のOpenAI呼び出しで、LINE向けの自然な最終返信を生成します。

最終返信では候補名、説明、確認済みURLがすべて含まれ、URLが一字も変わっていないことをコード側でも再検証します。URLだけの文、JSON、架空URL、候補名の欠落を検出した場合は、生レスポンスを送らず、構造化候補から自然なフォールバック文を作ります。候補0件や最終生成エラーも内部状態を見せず幹事らしく返します。空席、現在営業中、予約可能などはリアルタイムに確認できない限り断定しません。

追加のAPIキーは不要で、既存の `OPENAI_API_KEY` を使います。ただし、Web Searchのモデル利用・ツール呼び出しにはOpenAI API利用料金が発生し得ます。検索時だけ呼び出すことで不要なコストを抑えています。

## Pythonバージョン

Pythonは `.python-version`、`render.yaml` の `PYTHON_VERSION`、`pyproject.toml` の `requires-python` の3箇所で `3.12.7` / Python 3.12系に固定しています。起動時にも3.12系以外なら明示的に停止します。既存サービスをBlueprintで作成していない場合は、Render Dashboardの **Environment** で `PYTHON_VERSION=3.12.7` を設定し、**Clear build cache & deploy** を実行してください。デプロイログの `Using Python version 3.12.7` を確認します。

## SQLiteとRender無料環境の重要な制約

Renderのサービス実行ファイルシステムは一時的です。再デプロイ、再起動、インスタンス交換などでSQLiteデータが消える可能性があります。また複数インスタンスから単一SQLiteを安全に共有できません。第一版の試用には使えますが、データを残す本運用ではRender Persistent Disk（対応プランで `DATABASE_PATH` をマウント先へ変更）またはPostgreSQLへ移行してください。DBアクセスをrepositoryへ分離しているため移行範囲を限定できます。

## トラブルシューティング

- **Verifyが失敗**: RenderのDeploy Logs、URL末尾の `/webhook`、HTTPS、環境変数を確認します。Channel secretを再発行した場合はRender側も更新します。
- **署名エラー400**: `LINE_CHANNEL_SECRET` が対象チャネルと一致するか確認します。Webhook本文は署名検証前に変更してはいけません。
- **返信しない**: 日程回答だけなら正常です。「今どんな感じ？」で確認してください。LINE Official Account Managerの自動応答は競合防止のためOFFを推奨します。
- **グループへ招待できない**: 「Allow bot to join group chats」を有効化し、別の公式アカウントが既に参加していないか確認します。
- **OpenAIエラー**: API key、API請求設定、利用上限、`OPENAI_MODEL`へのアクセスをRender Logsで確認します。秘密値そのものはログに出ません。
- **データが消えた**: 無料環境の一時ディスク仕様です。Persistent DiskかPostgreSQLへ移行してください。
- **プロフィール名が取れない**: 同意・権限・参加状況により失敗し得ます。その場合は「メンバー」として処理を継続します。

## セキュリティと運用

Webhookは受信した生バイト列をHMAC-SHA256で検証してからJSON化します。Web検索結果は参考データとしてのみ扱い、ページ内の命令を無視するようシステム指示しています。APIキーやトークンは環境変数だけから読み、ログへ出しません。会話本文は最大50件だけDBへ残し、AIには直近10件だけを送ります。利用者への告知、保存期間、削除方針は運用前にグループ内で合意してください。

## 参照した現行公式仕様

- [LINE: Webhook署名検証](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)
- [LINE: Webhook受信と再送](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [OpenAI: Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI: Web search](https://developers.openai.com/api/docs/guides/tools-web-search)
