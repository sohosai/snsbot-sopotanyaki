# Slack Bot 環境設定

## 依存パッケージ
以下のパッケージ以上が必要です。

- `slack-bolt==1.22.0`
- `python-dotenv==0.21.0`

## 環境変数

### Slack API Tokens
- `SLACK_BOT_TOKEN`
- `SIGNING_SECRET`
- `SLACK_APP_TOKEN`
- `APP_ID`
- `CLIENT_ID`
- `CLIENT_SECRET`
- `VERIFICATION_TOKEN`
- `APP_LEVEL_TOKENS`

### 決議数
- `REQUIRED_APPROVALS`

### システム設定
- `JWT_Aexpiresin`
- `BASE_URL`
- `PORT`

## Scopes

### Bot Token Scopes
- `app_mentions:read`
- `assistant:write`
- `chat:write`
- `commands`
- `reactions:read`
- `reactions:write`
- `users:read`

## コマンド
- `/register`
- `/review`
- `/post`

## Event Subscriptions

### Bot イベントの購読
- `reaction_added`
- `reaction_removed`
