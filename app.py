import os
import time
import threading
import logging
import datetime
import re
import json
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

# SNSアカウント情報をJSONから読み込む
def load_sns_accounts():
    try:
        sns_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sns.json')
        with open(sns_file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"SNSアカウント情報の読み込みに失敗しました: {e}")
        return {}

# SNSアカウント情報をグローバル変数として保存
SNS_ACCOUNTS = load_sns_accounts()

# SNSに投稿する関数
def push_sns(sns_type, account, text, images=None):
    """
    SNSに投稿する関数
    Args:
        sns_type: SNSの種類 (X, Instaなど)
        account: 投稿するアカウント名
        text: 投稿テキスト
        images: 画像リスト（オプション）
    Returns:
        bool: 成功したかどうか
    """
    logger.info(f"{sns_type}の{account}アカウントに投稿しています...")
    # ここに実際の投稿処理を実装
    # 今回は仮の実装としてログ出力のみ行う
    if images:
        logger.info(f"画像あり投稿: {len(images)}枚")
    return True

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
            
    def clear_images(self):
        """画像リストをクリアする"""
        self.images = []
        
    def execute_post(self):
        """実際にSNSに投稿する"""
        return push_sns(self.sns, self.account, self.text, self.images)


def build_review_blocks(review: ReviewRequest) -> list:
    approvals_count = len(review.approvals)
    
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

    # 画像を追加する（Slackの認証トークンを使用して画像を表示）
    if review.images:
        blocks.append({"type": "divider"})
        for i, (file_id, permalink) in enumerate(review.images, start=1):
            try:
                # ファイル情報を取得
                file_info = app.client.files_info(file=file_id)
                if file_info["ok"]:
                    # 認証付きのURLを使用（この方が確実）
                    blocks.append({
                        "type": "image",
                        "image_url": file_info["file"]["url_private"],
                        "alt_text": f"Attached image {i}"
                    })
            except Exception as e:
                logger.error(f"画像情報取得エラー: {e}")
                
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
        # レビューが却下済みの場合は何もしない
        if review_request.rejected:
            return
            
        review_request.add_approval(user, time.strftime("%Y-%m-%d-%H:%M"))
        
        # 必要な承認数に達した場合すぐに承認
        if len(review_request.approvals) >= REQUIRED_APPROVALS and not review_request.approved:
            review_request.approved = True
            
            # まずレビューメッセージを更新
            update_review_message(review_request)
            
            # 次に承認通知を送信
            app.client.chat_postMessage(
                channel=review_request.channel,
                text=f"<@{review_request.author}>さんの投稿は必要数のレビュワーによって承認されました。"
            )
        else:
            # 承認数が足りない場合は、通常のメッセージ更新のみ
            update_review_message(review_request)
            
    elif reaction == "review_reject":
        # 即座にリジェクト処理
        if not review_request.rejected:
            review_request.rejected = True
            review_request.add_rejection(user, time.strftime("%Y-%m-%d-%H:%M"))
            
            # リジェクトメッセージを送信
            app.client.chat_delete(channel=review_request.channel, ts=review_request.ts)
            reject_message = f"<@{review_request.author}>さんの投稿は <@{user}>さんによってリジェクトされました。"
            app.client.chat_postMessage(channel=review_request.channel, text=reject_message)
            review_request = None


@app.event("reaction_removed")
def handle_reaction_removed(event, logger):
    global review_request
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    
    if review_request is None or review_request.ts != ts:
        return

    # リジェクト済みまたは承認済みの場合はリアクション削除の効果を無効化
    if review_request.rejected or review_request.approved:
        return

    if reaction == "review_accept":
        review_request.remove_approval(user)
        update_review_message(review_request)
    elif reaction == "review_reject":
        review_request.remove_rejection(user)
        update_review_message(review_request)


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

    # SNSへの投稿を実行
    success = review_request.execute_post()
    
    if success:
        # 投稿成功メッセージを送信
        app.client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}>さんの投稿が{review_request.sns}で実行されました。"
        )
    else:
        # 投稿失敗メッセージを送信
        app.client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}>さんの投稿が{review_request.sns}で失敗しました。"
        )
    
    # 投稿後、レビューリクエストをクリア
    review_request = None


@flask_app.route("/review_form")
def review_form():
    user_id = request.args.get("user_id")
    channel_id = request.args.get("channel_id")
    if not user_id or not channel_id:
        return "Invalid parameters", 400
    
    # SNSアカウント情報を取得してテンプレートに渡す
    return render_template("review_form.html", 
                           user_id=user_id, 
                           channel_id=channel_id,
                           sns_accounts=SNS_ACCOUNTS)


@flask_app.route("/submit_review", methods=["POST"])
def submit_review():
    global review_request
    
    user_id = request.form.get("user_id")
    channel_id = request.form.get("channel_id")
    sns = request.form.get("sns")
    account = request.form.get("account")
    post_text = request.form.get("post_text")
    
    # 指定されたSNSが存在するか確認
    if sns not in SNS_ACCOUNTS:
        flash(f"指定されたSNS({sns})は設定されていません。")
        return redirect(url_for("review_form", user_id=user_id, channel_id=channel_id))
    
    # アカウントが正しいか確認
    if account not in SNS_ACCOUNTS.get(sns, []):
        flash(f"指定されたアカウント({account})は、{sns}の設定と一致しません。")
        return redirect(url_for("review_form", user_id=user_id, channel_id=channel_id))
    
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
    
    # 先に本文のメッセージだけで更新
    update_review_message(review_request)
    
    uploaded_images = []
    files = request.files.getlist("images")
    
    for file in files:
        if file and file.filename:
            try:
                temp_file_path = os.path.join("/tmp", file.filename)
                file.save(temp_file_path)
                
                # 直接メッセージに添付するための非公開アップロード
                upload_res = app.client.files_upload_v2(
                    file=temp_file_path,
                    title=file.filename,
                    # チャンネル指定なしでアップロード
                )
                
                os.remove(temp_file_path)
                
                if not upload_res["ok"]:
                    raise Exception(f"ファイルアップロードエラー: {upload_res['error']}")
                
                file_info = upload_res["file"]
                file_id = file_info["id"]
                permalink = file_info.get("permalink", "")
                
                # ファイルを公開URLにする
                try:
                    app.client.files_sharedPublicURL(file=file_id)
                except Exception as e:
                    logger.debug(f"公開URLへの変換エラー: {e}")
                
                # ファイルをチャンネルと紐付ける
                try:
                    app.client.files_shareFilePublicly(file=file_id, channels=channel_id)
                except Exception as e:
                    logger.error(f"ファイル共有エラー: {e}")
                
                uploaded_images.append((file_id, permalink))
                logger.debug(f"画像がアップロードされました: {file.filename}, file_id: {file_id}, permalink: {permalink}")
                
            except Exception as e:
                logger.error(f"画像のアップロードに失敗しました: {e}")
                flash(f"画像 {file.filename} のアップロードに失敗しました。レビューは作成されましたが、一部の画像は含まれていません。")
    
    # 全ての画像をアップロードした後、一度だけメッセージを更新
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
    print(f"利用可能なSNS: {list(SNS_ACCOUNTS.keys())}")
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