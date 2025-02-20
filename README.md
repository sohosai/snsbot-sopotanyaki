# Slack Bot 環境設定

## 依存パッケージ
以下のパッケージが必要です。

- `slack-bolt==1.22.0`
- `python-dotenv==0.21.0`

## 環境変数

### Slack API Tokens
Slack API の認証に必要な環境変数:

- `SLACK_BOT_TOKEN`
- `SIGNING_SECRET`
- `SLACK_APP_TOKEN`
- `APP_ID`
- `CLIENT_ID`
- `CLIENT_SECRET`
- `VERIFICATION_TOKEN`
- `APP_LEVEL_TOKENS`

### 決議数
承認に必要な決議数を指定する環境変数:

- `REQUIRED_APPROVALS`
