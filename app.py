import os
import time
import threading
import re
import logging
import datetime  # 日時操作用
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
load_dotenv()

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

# カンマ区切りの環境変数（必要に応じて）
APP_LEVEL_TOKENS = [token.strip() for token in os.environ.get("APP_LEVEL_TOKENS", "").split(",") if token.strip()]

# 初期レビュワーと承認必要件数
REVIEWER_IDS = [uid for uid in os.environ.get("REVIEWER_IDS", "").split(",") if uid.strip()]
REQUIRED_APPROVALS = int(os.environ.get("REQUIRED_APPROVALS", "1"))

app = App(token=SLACK_BOT_TOKEN, signing_secret=SIGNING_SECRET)

# レビュー中の投稿は1件のみ管理するためのグローバル変数
review_request = None

# レビュー申請の構造体（タイトルの概念はなく、SNS種別を保持）
class ReviewRequest:
    def __init__(self, author, sns, account, text, images, channel, ts):
        self.author = author            # 投稿者のユーザーID
        self.sns = sns                  # SNS種別（例: Twitter, Instagram など）
        self.account = account          # 投稿アカウント
        self.text = text                # 本文
        self.images = images            # 添付画像リスト
        self.channel = channel          # チャンネルID
        self.ts = ts                    # レビュー申請メッセージのタイムスタンプ
        self.approvals = {}             # 承認したユーザー {user_id: 承認時刻}
        self.rejections = {}            # リジェクトしたユーザー {user_id: リジェクト時刻}
        self.reject_timer = None        # リジェクトタイマー（5分後に確定）
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

# レビュー申請メッセージの更新（承認件数・リジェクト状況を反映）
def update_review_message(review: ReviewRequest):
    approvals_count = len(review.approvals)
    message = (
        f"<@{review.author}>さんの投稿レビュー\n"
        f"【SNS】 {review.sns}\n"
        f"【投稿アカウント】 {review.account}\n"
        f"【承認】 {approvals_count}/{REQUIRED_APPROVALS}\n"
    )
    if review.approved:
        message += "→ 承認済み。投稿可能です。"
    elif review.rejected:
        message += "→ リジェクト済み。"
    elif review.rejections:
        # 最初にリジェクトしたユーザーと時刻を表示
        first_reject_time_str = min(review.rejections.values())
        first_rejecter = next(user for user, t in review.rejections.items() if t == first_reject_time_str)
        dt = datetime.datetime.strptime(first_reject_time_str, "%Y-%m-%d-%H:%M")
        formal_dt = dt + datetime.timedelta(minutes=5)
        formal_time_str = formal_dt.strftime("%Y-%m-%d-%H:%M")
        message += (
            f"\n<@{first_rejecter}>さんが{first_reject_time_str}にリジェクトしました。"
            f" 5分後の{formal_time_str}に正式に拒否されます。"
            " 間違って押した場合は5分以内に取り消しして下さい。"
        )
        message += "\n許可の場合は :review_accept: 、却下の場合は :review_reject: を押してください。"
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
    global review_request
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    text = body.get("text", "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # すでにレビュー中の投稿がある場合はエラー
    if review_request is not None:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="すでにレビュー中の投稿があります。"
        )
        return

    if len(lines) < 3:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="フォーマットエラー：必要な情報が不足しています。"
        )
        return

    # 1行目：SNS種別（例: ## Twitter）
    if not lines[0].startswith("## "):
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="フォーマットエラー：SNS行の形式が正しくありません。（例: ## Twitter）"
        )
        return
    sns = lines[0][3:].strip()
    if not sns:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="エラー：SNSが空です。"
        )
        return

    # 2行目：投稿アカウント（例: ## アカウントA）
    if not lines[1].startswith("## "):
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="フォーマットエラー：投稿アカウント行の形式が正しくありません。（例: ## アカウントA）"
        )
        return
    account = lines[1][3:].strip()
    if not account:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="エラー：投稿アカウントが指定されていません。"
        )
        return

    # 3行目：本文（例: > 本文）
    if not lines[2].startswith(">"):
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="フォーマットエラー：テキスト文章行の形式が正しくありません。（例: > 本文）"
        )
        return
    post_text = lines[2][1:].strip()
    if not post_text:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="エラー：テキスト文章が空です。"
        )
        return

    # 4行目以降は添付画像（最大3枚）
    images = lines[3:]
    if len(images) >= 4:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="エラー：添付画像は最大3枚までです。"
        )
        return

    # レビュワーへのメンションを作成
    reviewer_mentions = " ".join([f"<@{uid.strip()}>" for uid in REVIEWER_IDS if uid.strip()])
    # ※ 初期メッセージを指定の文言に変更
    review_message = f"<@{user_id}>さんの投稿レビューが {reviewer_mentions} に認証依頼が届いています。"
    response = app.client.chat_postMessage(
        channel=channel_id,
        text=review_message
    )
    ts = response["ts"]

    review_request = ReviewRequest(
        author=user_id,
        sns=sns,
        account=account,
        text=post_text,
        images=images,
        channel=channel_id,
        ts=ts
    )
    update_review_message(review_request)

# リアクション追加イベント：承認・却下の処理
@app.event("reaction_added")
def handle_reaction_added(event, logger):
    global review_request
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    
    if review_request is None or review_request.ts != ts:
        return

    if reaction == "review_accept":
        review_request.add_approval(user, time.strftime("%Y-%m-%d-%H:%M"))
        update_review_message(review_request)
        # 必要承認数に達した場合は承認状態にする
        if len(review_request.approvals) >= REQUIRED_APPROVALS and not review_request.approved:
            review_request.approved = True
            app.client.chat_postMessage(
                channel=review_request.channel,
                text=f"<@{review_request.author}>さんの投稿は全てのレビュワーによって承認されました。"
            )
            update_review_message(review_request)

    elif reaction == "review_reject":
        review_request.add_rejection(user, time.strftime("%Y-%m-%d-%H:%M"))
        update_review_message(review_request)
        if review_request.reject_timer is None:
            def finalize_rejection():
                global review_request
                if review_request and review_request.rejections and not review_request.approved:
                    # 最初にリジェクトしたユーザーと時刻を取得
                    first_reject_time_str = min(review_request.rejections.values())
                    first_rejecter = next(u for u, t in review_request.rejections.items() if t == first_reject_time_str)
                    dt = datetime.datetime.strptime(first_reject_time_str, "%Y-%m-%d-%H:%M")
                    final_dt = dt + datetime.timedelta(minutes=5)
                    final_time_str = final_dt.strftime("%Y-%m-%d-%H:%M")
                    # 元のレビュー投稿を削除
                    app.client.chat_delete(channel=review_request.channel, ts=review_request.ts)
                    # 完全リジェクトの最終メッセージを投稿
                    final_message = f"<@{review_request.author}>さんの投稿は <@{first_rejecter}>さんによって、{final_time_str}に完全にリジェクトされました。"
                    app.client.chat_postMessage(
                        channel=review_request.channel,
                        text=final_message
                    )
                    # レビュー状態を解除
                    review_request = None
            review_request.reject_timer = threading.Timer(300, finalize_rejection)
            review_request.reject_timer.start()

# リアクション削除イベント：承認・却下の取り消し処理
@app.event("reaction_removed")
def handle_reaction_removed(event, logger):
    global review_request
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    
    if review_request is None or review_request.ts != ts:
        return

    if reaction == "review_accept":
        review_request.remove_approval(user)
        if review_request.approved and len(review_request.approvals) < REQUIRED_APPROVALS:
            review_request.approved = False
        update_review_message(review_request)

    elif reaction == "review_reject":
        review_request.remove_rejection(user)
        update_review_message(review_request)
        if not review_request.rejections and review_request.reject_timer is not None:
            review_request.reject_timer.cancel()
            review_request.reject_timer = None

# /post コマンド：投稿実行（引数不要）
@app.command("/post")
def handle_post_command(ack, body, logger):
    global review_request
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]

    if review_request is None or review_request.author != user_id or not review_request.approved:
        app.client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="該当する承認済みの投稿が見つかりません。"
        )
        return

    # シミュレーションとして投稿実行
    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}>さんの投稿が{review_request.sns}で実行されました。"
    )
    # 投稿後はレビュー状態を解除
    review_request = None

# /register コマンド：レビュワーの追加
@app.command("/register")
def handle_register(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    text = body.get("text", "").strip()

    logger.debug(f"Received /register command from user {user_id} in channel {channel_id} with text: {text}")

    if not text:
        error_message = (
            "エラー：追加するユーザーを指定してください。\n"
            "例：/register <@U1234567> または /register @UserName"
        )
        app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
        return

    match = re.search(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", text)
    if match:
        new_reviewer = match.group(1)
    else:
        if text.startswith("@"):
            possible_name = text[1:].strip()
        else:
            possible_name = text

        logger.debug(f"Try to find Slack user whose display_name, real_name, or name is '{possible_name}'")

        try:
            all_members = []
            cursor = None
            while True:
                response = app.client.users_list(cursor=cursor)
                members = response.get("members", [])
                all_members.extend(members)
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

            matched_user_id = None
            for mem in all_members:
                profile = mem.get("profile", {})
                display_name = profile.get("display_name", "") or ""
                real_name = profile.get("real_name", "") or ""
                slack_name = mem.get("name", "") or ""

                if (
                    display_name.lower() == possible_name.lower() or
                    real_name.lower() == possible_name.lower() or
                    slack_name.lower() == possible_name.lower()
                ):
                    matched_user_id = mem.get("id")
                    break

            if matched_user_id:
                new_reviewer = matched_user_id
            else:
                error_message = (
                    f"エラー：@{possible_name} に対応するSlackユーザーが見つかりませんでした。\n"
                    "別の指定方法（<@U1234567>形式）を試すか、正しい表示名/実名/ユーザー名かご確認ください。"
                )
                app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
                return

        except Exception as e:
            error_message = f"ユーザーリスト取得時にエラーが発生しました: {e}"
            logger.exception(error_message)
            app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
            return

    global REVIEWER_IDS
    if new_reviewer in REVIEWER_IDS:
        error_message = f"<@{new_reviewer}> は既にレビュワーに登録されています。"
        app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=error_message)
        return

    REVIEWER_IDS.append(new_reviewer)
    logger.debug(f"New reviewer added: {new_reviewer}, updated REVIEWER_IDS: {REVIEWER_IDS}")
    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{new_reviewer}> をレビュワーに追加しました。"
    )

if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
