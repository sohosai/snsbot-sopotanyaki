import os
import time
import threading
import logging
import datetime
import re
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from flask import Flask, render_template, request, redirect, url_for, flash, session
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")

REVIEWER_IDS = [uid for uid in os.environ.get("REVIEWER_IDS", "").split(",") if uid.strip()]
REQUIRED_APPROVALS = int(os.environ.get("REQUIRED_APPROVALS", "1"))

current_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(current_dir, 'templates')
static_dir = os.path.join(current_dir, 'static')

if not os.path.exists(template_dir):
    os.makedirs(template_dir)
    print(f"テンプレートディレクトリを作成しました: {template_dir}")

flask_app = Flask(__name__, 
                 template_folder=template_dir,
                 static_folder=static_dir)
flask_app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev_secret_key")

app = App(token=SLACK_BOT_TOKEN, signing_secret=SIGNING_SECRET)

review_request = None

class ReviewRequest:
    def __init__(self, author, sns, account, text, images, channel, ts):
        self.author = author
        self.sns = sns
        self.account = account
        self.text = text
        self.images = images
        self.channel = channel
        self.ts = ts
        self.approvals = {}
        self.rejections = {}
        self.reject_timer = None
        self.approved = False
        self.rejected = False

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


def build_review_blocks(review: ReviewRequest) -> list:
    approvals_count = len(review.approvals)
    
    reject_info = ""
    if review.rejected:
        reject_info = "→ リジェクト済み。"
    elif review.rejections:
        first_reject_time_str = min(review.rejections.values())
        first_rejecter = next(u for u, t in review.rejections.items() if t == first_reject_time_str)
        dt = datetime.datetime.strptime(first_reject_time_str, "%Y-%m-%d-%H:%M")
        formal_dt = dt + datetime.timedelta(minutes=5)
        formal_time_str = formal_dt.strftime("%Y-%m-%d-%H:%M")
        reject_info = (
            f"<@{first_rejecter}>さんが{first_reject_time_str}にリジェクトしました。"
            f" 5分後（{formal_time_str}）に正式に拒否されます。"
            " （間違った場合は5分以内にリアクションを取り消してください。）"
        )

    description_text = f"""
*<@{review.author}> さんの投稿レビュー*
• SNS: *{review.sns}*
• 投稿アカウント: *{review.account}*
• 承認状況: {approvals_count}/{REQUIRED_APPROVALS}
"""
    if review.approved:
        description_text += "\n→ *承認済み*。投稿可能です。"
    elif review.rejected:
        description_text += "\n→ *リジェクト済み*。"
    else:
        if review.rejections:
            description_text += "\n" + reject_info
        description_text += "\n許可の場合は :review_accept:、却下の場合は :review_reject: を押してください。"

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": description_text.strip()}
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*本文*\n```{review.text}```"}
        }
    ]

    if review.images:
        blocks.append({"type": "divider"})
        for i, (file_id, _) in enumerate(review.images, start=1):
            blocks.append({
                "type": "image",
                "slack_file": {"id": file_id},
                "alt_text": f"Attached image {i}"
            })
    return blocks


def update_review_message(review: ReviewRequest):
    blocks = build_review_blocks(review)
    app.client.chat_update(
        channel=review.channel,
        ts=review.ts,
        text="レビュー内容を更新しました",
        blocks=blocks
    )


@app.command("/review")
def handle_review_command(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    
    base_url = os.environ.get("BASE_URL", "http://localhost:5000/")
    if not base_url.endswith('/'):
        base_url += '/'
    
    review_url = f"{base_url}review_form?user_id={user_id}&channel_id={channel_id}"
    
    app.client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f"レビュー申請フォームを開いてください: {review_url}"
    )


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
        if len(review_request.approvals) >= REQUIRED_APPROVALS and not review_request.approved:
            review_request.approved = True
            app.client.chat_postMessage(
                channel=review_request.channel,
                text=f"<@{review_request.author}>さんの投稿は必要数のレビュワーによって承認されました。"
            )
            update_review_message(review_request)
    elif reaction == "review_reject":
        review_request.add_rejection(user, time.strftime("%Y-%m-%d-%H:%M"))
        update_review_message(review_request)
        if review_request.reject_timer is None:
            def finalize_rejection():
                global review_request
                if review_request and review_request.rejections and not review_request.approved:
                    first_reject_time_str = min(review_request.rejections.values())
                    first_rejecter = next(u for u, t in review_request.rejections.items() if t == first_reject_time_str)
                    dt = datetime.datetime.strptime(first_reject_time_str, "%Y-%m-%d-%H:%M")
                    final_dt = dt + datetime.timedelta(minutes=5)
                    final_time_str = final_dt.strftime("%Y-%m-%d-%H:%M")
                    app.client.chat_delete(channel=review_request.channel, ts=review_request.ts)
                    final_message = f"<@{review_request.author}>さんの投稿は <@{first_rejecter}>さんによって、{final_time_str}に完全にリジェクトされました。"
                    app.client.chat_postMessage(channel=review_request.channel, text=final_message)
                    review_request = None
            review_request.reject_timer = threading.Timer(300, finalize_rejection)
            review_request.reject_timer.start()


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


@app.command("/register")
def handle_register_command(ack, body, logger):
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

    app.client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}>さんの投稿が{review_request.sns}で実行されました。"
    )
    review_request = None


@flask_app.route("/review_form")
def review_form():
    user_id = request.args.get("user_id")
    channel_id = request.args.get("channel_id")
    if not user_id or not channel_id:
        return "Invalid parameters", 400
    
    return render_template("review_form.html", user_id=user_id, channel_id=channel_id)


@flask_app.route("/submit_review", methods=["POST"])
def submit_review():
    global review_request
    
    user_id = request.form.get("user_id")
    channel_id = request.form.get("channel_id")
    sns = request.form.get("sns")
    account = request.form.get("account")
    post_text = request.form.get("post_text")
    
    if not all([user_id, channel_id, sns, account, post_text]):
        flash("すべての必須フィールドを入力してください。")
        return redirect(url_for("review_form", user_id=user_id, channel_id=channel_id))
    
    reviewer_mentions = ' '.join(f'<@{uid}>' for uid in REVIEWER_IDS if uid.strip())
    if reviewer_mentions:
        review_message = f"<@{user_id}>さんの投稿レビューが {reviewer_mentions} に認証依頼が届いています。"
    else:
        review_message = f"<@{user_id}>さんの投稿レビューが届いています。レビュワーが設定されていません。"
    
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
        images=[],  
        channel=channel_id,
        ts=ts
    )
    
    update_review_message(review_request)
    
    uploaded_images = []
    files = request.files.getlist("images")
    
    for file in files:
        if file and file.filename:
            try:
                temp_file_path = os.path.join("/tmp", file.filename)
                file.save(temp_file_path)
                
                # ファイルをアップロード
                upload_res = app.client.files_upload_v2(
                    file=temp_file_path,
                    title=file.filename,
                    channels=channel_id
                )
                
                os.remove(temp_file_path)
                
                if not upload_res["ok"]:
                    raise Exception(f"ファイルアップロードエラー: {upload_res['error']}")
                
                file_info = upload_res["file"]
                file_id = file_info["id"]
                permalink = file_info.get("permalink", "")
                
                uploaded_images.append((file_id, permalink))
                logger.debug(f"画像がアップロードされました: {file.filename}, file_id: {file_id}, permalink: {permalink}")
                
            except Exception as e:
                logger.error(f"画像のアップロードに失敗しました: {e}")
                flash(f"画像 {file.filename} のアップロードに失敗しました。レビューは作成されましたが、一部の画像は含まれていません。")
    
    if uploaded_images:
        review_request.images = uploaded_images
        update_review_message(review_request)
    
    return render_template("submission_success.html")


@flask_app.route("/")
def index():
    return "Slack Review System"


def run_flask():
    port = int(os.environ.get("PORT", 7700))
    try:
        print(f"Flaskサーバーを開始: http://localhost:{port}/")
        flask_app.run(host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        print(f"Flaskサーバー起動エラー: {e}")
        raise


def run_slack():
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7700))
    base_url = os.environ.get("BASE_URL", f"http://localhost:{port}/")
    
    print(f"=== 設定情報 ===")
    print(f"ポート番号: {port}")
    print(f"ベースURL: {base_url}")
    print(f"レビューフォームURL: {base_url}review_form")
    print(f"レビュワー: {REVIEWER_IDS}")
    print(f"必要承認数: {REQUIRED_APPROVALS}")
    print(f"===============")
    
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--flask-only":
        print("Flaskサーバーのみを起動します...")
        run_flask()
    elif len(sys.argv) > 1 and sys.argv[1] == "--slack-only":
        print("Slackボットのみを起動します...")
        run_slack()
    else:
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        time.sleep(2)
        
        print("Slackボットを起動しています...")
        try:
            run_slack()
        except KeyboardInterrupt:
            print("アプリケーションを終了します...")