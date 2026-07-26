# おたすけさんLINEボット（試作版）

LINE Messaging API、OpenAI API、Renderを使った相談ボットです。

## Webhook URL

Renderで公開後、LINE Developersには次の形式で登録します。

```text
https://あなたのRenderサービス名.onrender.com/callback
```

## Renderに設定する秘密情報

RenderのEnvironment Variablesに次の3つを設定します。

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`

任意設定：

- `OPENAI_MODEL`（初期値：`gpt-4.1-mini`）

**APIキーやLINEの秘密情報は、GitHubのファイルに直接書かないでください。**

## Render設定

- Language：Python
- Build Command：`pip install -r requirements.txt`
- Start Command：`gunicorn app:app`
- Health Check Path：`/`

`render.yaml`を使う場合は、これらが自動設定されます。
