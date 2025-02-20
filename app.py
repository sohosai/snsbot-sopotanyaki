import os
import time
import threading
import re
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ログ設定：DEBUGレベルのログをコンソールに出力
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Slackアプリの各種環境変数の読み込み
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
APP_ID = os.environ.get("APP_ID")
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
VERIFICATION_TOKEN = os.environ.get("VERIFICATION_TOKEN")
APP_LEVEL_TOKENS = [token.strip() for token in os.environ.get("APP_LEVEL_TOKENS", "").split(",") if token.strip()]

# 初期レビュワーと承認必要件数
REVIEWER_IDS = [uid for uid in os.environ.get("REVIEWER_IDS", "").split(",") if uid.strip()]
REQUIRED_APPROVALS = int(os.environ.get("REQUIRED_APPROVALS", "2"))

app = App(token=SLACK_BOT_TOKEN, signing_secret=SIGNING_SECRET)

# レビュー申請情報を管理するグローバル辞書（キーはレビュー用メッセージの ts）
review_requests = {}

# レビュー申請の構造体
class ReviewRequest:
    def __init__(self, author, title, account, text, images, channel, ts):
        self.author = author            # 申請者のユーザーID
        self.title = title              # タイトル
        self.account = account          # 投稿アカウント
        self.text = text                # 本文
        self.images = images            # 添付画像リスト
        self.channel = channel          # チャンネルID
        self.ts = ts                    # レビュー申請メッセージのタイムスタンプ
        self.approvals = {}             # 承認したユーザー {user_id: 承認時刻}
        self.rejections = {}            # 却下したユーザー {user_id: 却下時刻}
        self.reject_timer = None        # 却下タイマー（5分後に確定）
        self.approved = False           # 承認済みフラグ
        self.rejected = False           # リジェクト済みフラグ

    def add_approval(self, user, timestamp):
        self.approvals[user] = timestamp

    def remove_approval(self, user):
        if user in self.approvals:
            del self.approvals[user]

    def add_rejection(self, user, timestamp):
        self.rejections[user] = timestamp

    def remove_rejection(self, user):
        if user in self.rejections:
            del self.rejections[user]

# レビュー申請メッセージの更新（現状の承認件数などを反映）
def update_review_message(review: ReviewRequest):
    approvals_count = len(review.approvals)
    message = (
        f"<@{review.author}>さんの投稿レビュー\n"
        f"【タイトル】 {review.title}\n"
        f"【投稿アカウント】 {review.account}\n"
        f"【承認】 {approvals_count}/{REQUIRED_APPROVALS}\n"
    )
    if review.approved:
        message += "→ 承認済み。投稿可能です。"
    elif review.rejected:
        message += "→ リジェクト済み。"
    else:
        message += "許可の場合は :review_accept: 、却下の場合は :review_reject: を押してください。"
    
    app.client.chat_update(
        channel=review.channel,
        ts=review.ts,
        text=message
    )

# /review コマンド：投稿内容をレビュー状態にする
@app.command("/review")
def handle_review_command(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    text = body.get("text", "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if len(lines) < 3:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="フォーマットエラー：必要な情報が不足しています。"
        )
        return

    if not lines[0].startswith("## "):
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="フォーマットエラー：タイトル行の形式が正しくありません。"
        )
        return
    title = lines[0][3:].strip()
    if not title:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="エラー：タイトルが空です。"
        )
        return

    if not lines[1].startswith("## "):
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="フォーマットエラー：投稿アカウント行の形式が正しくありません。"
        )
        return
    account = lines[1][3:].strip()
    if not account:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="エラー：投稿アカウントが指定されていません。"
        )
        return

    if not lines[2].startswith(">"):
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="フォーマットエラー：テキスト文章行の形式が正しくありません。"
        )
        return
    post_text = lines[2][1:].strip()
    if not post_text:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="エラー：テキスト文章が空です。"
        )
        return

    images = lines[3:]
    if len(images) >= 4:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="エラー：添付画像は最大3枚までです。"
        )
        return

    reviewer_mentions = " ".join([f"<@{uid.strip()}>" for uid in REVIEWER_IDS if uid.strip()])
    review_message = (
        f"<@{user_id}>さんより {reviewer_mentions} に投稿チェックが届いています。\n"
        "許可の場合は :review_accept: 、却下の場合は :review_reject: を押してください。"
    )
    response = app.client.chat_postMessage(
        channel=channel_id,
        text=review_message
    )
    review_ts = response["ts"]

    review = ReviewRequest(
        author=user_id, title=title, account=account,
        text=post_text, images=images, channel=channel_id, ts=review_ts
    )
    review_requests[review_ts] = review
    update_review_message(review)

# リアクション追加イベント：承認・却下の処理
@app.event("reaction_added")
def handle_reaction_added(event, logger):
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    
    if ts not in review_requests:
        return
    review = review_requests[ts]

    if reaction == "review_accept":
        review.add_approval(user, time.strftime("%Y-%m-%d-%H:%M"))
        update_review_message(review)
        if len(review.approvals) >= REQUIRED_APPROVALS and not review.approved:
            review.approved = True
            app.client.chat_postMessage(
                channel=review.channel,
                text=f"<@{review.author}>さんの投稿は全てのレビュワーによって承認されました。"
            )
            update_review_message(review)
    elif reaction == "review_reject":
        review.add_rejection(user, time.strftime("%Y-%m-%d-%H:%M"))
        update_review_message(review)
        if review.reject_timer is None:
            def finalize_rejection():
                if review.rejections and not review.approved:
                    review.rejected = True
                    app.client.chat_postMessage(
                        channel=review.channel,
                        text=f"<@{review.author}>さんの投稿はリジェクトされました。"
                    )
                    update_review_message(review)
            review.reject_timer = threading.Timer(300, finalize_rejection)
            review.reject_timer.start()

# リアクション削除イベント：承認・却下の取り消し処理
@app.event("reaction_removed")
def handle_reaction_removed(event, logger):
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    
    if ts not in review_requests:
        return
    review = review_requests[ts]

    if reaction == "review_accept":
        review.remove_approval(user)
        update_review_message(review)
    elif reaction == "review_reject":
        review.remove_rejection(user)
        update_review_message(review)
        if not review.rejections and review.reject_timer is not None:
            review.reject_timer.cancel()
            review.reject_timer = None

# /twitter_post コマンド：Twitter への投稿実行（シミュレーション）
@app.command("/twitter_post")
def handle_twitter_post(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    title = body.get("text", "").strip()
    review_found = None
    for r in review_requests.values():
        if r.author == user_id and r.title == title and r.approved:
            review_found = r
            break
    if not review_found:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="該当する承認済みの投稿が見つかりません。"
        )
        return

    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}>さんの投稿がTwitterで実行されました。"
    )

# /insta_post コマンド：Instagram への投稿実行（シミュレーション）
@app.command("/insta_post")
def handle_insta_post(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    title = body.get("text", "").strip()
    review_found = None
    for r in review_requests.values():
        if r.author == user_id and r.title == title and r.approved:
            review_found = r
            break
    if not review_found:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="該当する承認済みの投稿が見つかりません。"
        )
        return

    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}>さんの投稿がInstagramで実行されました。"
    )

# /all_post コマンド：全 SNS への投稿実行（シミュレーション）
@app.command("/all_post")
def handle_all_post(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    title = body.get("text", "").strip()
    review_found = None
    for r in review_requests.values():
        if r.author == user_id and r.title == title and r.approved:
            review_found = r
            break
    if not review_found:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="該当する承認済みの投稿が見つかりません。"
        )
        return

    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}>さんの投稿が全てのSNSで実行されました。"
    )

# /register コマンド：Slackのメンション形式（例：/register <@USERID>）でレビュワー（認証者）を追加
@app.command("/register")
def handle_register(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    text = body.get("text", "").strip()
    
    # 入力内容とその状態をデバッグログに出力
    logger.debug("Received /register command from user %s in channel %s with text: %s", user_id, channel_id, text)
    
    if not text:
        error_message = "エラー：追加するユーザーを指定してください。（例：/register <@USERID>）"
        logger.debug("No text provided in /register command: %s", error_message)
        app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
        return

    # Slackのメンション形式の確認
    match = re.search(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", text)
    logger.debug("Regex match result: %s", match)
    
    if not match:
        error_message = "エラー：ユーザーはSlackのメンション形式で指定してください。（例：/register <@USERID>）"
        logger.debug("User mention not in correct format. Input text: %s | Error: %s", text, error_message)
        app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
        return

    new_reviewer = match.group(1)
    logger.debug("Extracted new reviewer user ID: %s", new_reviewer)
    
    global REVIEWER_IDS
    if new_reviewer in REVIEWER_IDS:
        error_message = f"<@{new_reviewer}> は既にレビュワーに登録されています。"
        logger.debug("User already registered: %s", error_message)
        app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
        return

    REVIEWER_IDS.append(new_reviewer)
    logger.debug("New reviewer added: %s, updated REVIEWER_IDS: %s", new_reviewer, REVIEWER_IDS)
    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{new_reviewer}> をレビュワーに追加しました。"
    )

if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
