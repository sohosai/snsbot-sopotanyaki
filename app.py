import os
import time
import threading
import logging
import datetime
import re
import json
import requests
import uuid
import base64
import jwt  
from io import BytesIO
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from dotenv import load_dotenv
from urllib.parse import urlencode  
load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SIGNING_SECRET = os.environ.get("SIGNING_SECRET")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
JWT_SECRET = os.environ.get("JWT_SECRET", "super_secret_key")  # JWT secret key
JWT_EXPIRES_IN = int(os.environ.get("JWT_EXPIRES_IN", "3600"))  # Expiration time in seconds (default 1 hour)

REVIEWER_IDS = [uid for uid in os.environ.get("REVIEWER_IDS", "").split(",") if uid.strip()]
REQUIRED_APPROVALS = int(os.environ.get("REQUIRED_APPROVALS", "1"))

current_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(current_dir, 'templates')
static_dir = os.path.join(current_dir, 'static')
uploads_dir = os.path.join(current_dir, 'uploads')

if not os.path.exists(template_dir):
    os.makedirs(template_dir)
    print(f"テンプレートディレクトリを作成しました: {template_dir}")

if not os.path.exists(uploads_dir):
    os.makedirs(uploads_dir)
    print(f"アップロードディレクトリを作成しました: {uploads_dir}")

flask_app = Flask(__name__, 
                 template_folder=template_dir,
                 static_folder=static_dir)
flask_app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev_secret_key")

app = App(token=SLACK_BOT_TOKEN, signing_secret=SIGNING_SECRET)

# レビューリクエストをIDベースで保存する辞書
review_requests = {}

# JWT関連の関数
def generate_jwt_token(payload):
    """
    JWTトークンを生成する関数
    Args:
        payload: トークンに含めるデータ (dict)
    Returns:
        str: エンコードされたJWTトークン
    """
    expiration = datetime.datetime.utcnow() + datetime.timedelta(seconds=JWT_EXPIRES_IN)
    payload["exp"] = expiration
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_jwt_token(token):
    """
    JWTトークンを検証する関数
    Args:
        token: 検証するJWTトークン
    Returns:
        dict: デコードされたペイロードまたはNone（無効な場合）
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.error("JWTトークンの有効期限が切れています")
        return None
    except jwt.InvalidTokenError:
        logger.error("無効なJWTトークンです")
        return None

# セキュアなURLを生成する関数
def generate_secure_url(base_url, path, params=None):
    """
    JWT認証付きのURLを生成する
    Args:
        base_url: ベースとなるURL
        path: パス部分
        params: クエリパラメータ (dict, optional)
    Returns:
        str: 生成されたURL
    """
    # パラメータがない場合は空のdictで初期化
    if params is None:
        params = {}
    
    # JWTトークンを生成
    token = generate_jwt_token(params)
    
    # URLを構築
    if not base_url.endswith('/'):
        base_url += '/'
    
    if path.startswith('/'):
        path = path[1:]
    
    url = f"{base_url}{path}?token={token}"
    return url

# SNSアカウント情報をJSONから読み込む
def load_sns_accounts():
    try:
        sns_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sns.json')
        with open(sns_file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"SNSアカウント情報の読み込みに失敗しました: {e}")
        return {"Twitter": ["公式アカウント", "部門アカウント"], 
                "Facebook": ["公式ページ"], 
                "Instagram": ["公式アカウント"]}

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
        images: 画像パスのリスト（オプション）
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
    def __init__(self, author, sns, account, text, channel, request_id=None):
        self.request_id = request_id if request_id else str(uuid.uuid4())
        self.author = author
        self.sns = sns
        self.account = account
        self.text = text
        self.images = []  # 画像ファイル名のリスト
        self.channel = channel
        self.ts = None  # Slackメッセージのタイムスタンプ（投稿時に設定）
        self.approvals = {}
        self.rejections = {}
        self.approved = False
        self.rejected = False
        self.created_at = datetime.datetime.now()

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
            
    def add_image(self, filename):
        """画像ファイル名を追加する"""
        self.images.append(filename)
            
    def clear_images(self):
        """画像リストをクリアする"""
        self.images = []
        
    def execute_post(self):
        """実際にSNSに投稿する"""
        image_paths = [os.path.join(uploads_dir, img) for img in self.images]
        return push_sns(self.sns, self.account, self.text, image_paths)


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
    
    # プレビューページへのリンクを追加（JWT認証付き）
    base_url = os.environ.get("BASE_URL", "http://localhost:5000/")
    preview_url = generate_secure_url(base_url, f"preview/{review.request_id}", {"request_id": review.request_id})
    
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": description_text.strip()}
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*投稿内容をプレビュー:*\n{preview_url}"}
        }
    ]
                
    return blocks


def update_review_message(review: ReviewRequest):
    # レビューメッセージが存在しない場合は新規作成
    if not review.ts:
        reviewer_mentions = ' '.join(f'<@{uid}>' for uid in REVIEWER_IDS if uid.strip())
        if reviewer_mentions:
            review_message = f"<@{review.author}>さんの投稿レビューが {reviewer_mentions} に届いています。"
        else:
            review_message = f"<@{review.author}>さんの投稿レビューが届いています。レビュワーが設定されていません。"
        
        blocks = build_review_blocks(review)
        
        response = app.client.chat_postMessage(
            channel=review.channel,
            text=review_message,
            blocks=blocks
        )
        review.ts = response["ts"]
    else:
        # 既存メッセージの更新
        blocks = build_review_blocks(review)
        try:
            app.client.chat_update(
                channel=review.channel,
                ts=review.ts,
                text="レビュー内容を更新しました",
                blocks=blocks
            )
        except Exception as e:
            logger.error(f"メッセージ更新エラー: {e}")


@app.command("/review")
def handle_review_command(ack, body, logger):
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    
    base_url = os.environ.get("BASE_URL", "http://localhost:5000/")
    
    # JWTトークン付きのURLを生成
    params = {
        "user_id": user_id,
        "channel_id": channel_id
    }
    review_url = generate_secure_url(base_url, "review_form", params)
    
    app.client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f"レビュー申請フォームを開いてください: {review_url}"
    )


@app.event("reaction_added")
def handle_reaction_added(event, logger):
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    channel = item.get("channel")
    
    # tsに一致するレビューリクエストを探す
    for request_id, review in review_requests.items():
        if review.ts == ts and review.channel == channel:
            if reaction == "review_accept":
                # レビューが却下済みの場合は何もしない
                if review.rejected:
                    return
                    
                review.add_approval(user, time.strftime("%Y-%m-%d-%H:%M"))
                
                # 必要な承認数に達した場合すぐに承認
                if len(review.approvals) >= REQUIRED_APPROVALS and not review.approved:
                    review.approved = True
                    
                    # まずレビューメッセージを更新
                    update_review_message(review)
                    
                    # 次に承認通知を送信
                    app.client.chat_postMessage(
                        channel=review.channel,
                        text=f"<@{review.author}>さんの投稿は必要数のレビュワーによって承認されました。"
                    )
                else:
                    # 承認数が足りない場合は、通常のメッセージ更新のみ
                    update_review_message(review)
                    
            elif reaction == "review_reject":
                # 即座にリジェクト処理
                if not review.rejected:
                    review.rejected = True
                    review.add_rejection(user, time.strftime("%Y-%m-%d-%H:%M"))
                    
                    # リジェクトメッセージを送信
                    reject_message = f"<@{review.author}>さんの投稿は <@{user}>さんによってリジェクトされました。"
                    app.client.chat_postMessage(channel=review.channel, text=reject_message)
                    
                    # レビューリクエストの削除
                    del review_requests[request_id]
            break


@app.event("reaction_removed")
def handle_reaction_removed(event, logger):
    reaction = event.get("reaction")
    user = event.get("user")
    item = event.get("item", {})
    ts = item.get("ts")
    channel = item.get("channel")
    
    # tsに一致するレビューリクエストを探す
    for request_id, review in review_requests.items():
        if review.ts == ts and review.channel == channel:
            # リジェクト済みまたは承認済みの場合はリアクション削除の効果を無効化
            if review.rejected or review.approved:
                return

            if reaction == "review_accept":
                review.remove_approval(user)
                update_review_message(review)
            elif reaction == "review_reject":
                review.remove_rejection(user)
                update_review_message(review)
            break


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
    ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]

    # ユーザーが承認済みの投稿リクエストを探す
    for request_id, review in list(review_requests.items()):
        if review.author == user_id and review.approved:
            # SNSへの投稿を実行
            success = review.execute_post()
            
            if success:
                # 投稿成功メッセージを送信
                app.client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}>さんの投稿が{review.sns}で実行されました。"
                )
            else:
                # 投稿失敗メッセージを送信
                app.client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}>さんの投稿が{review.sns}で失敗しました。"
                )
            
            # 投稿後、レビューリクエストを削除
            del review_requests[request_id]
            return
    
    # 該当する承認済み投稿がない場合
    app.client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text="該当する承認済みの投稿が見つかりません。"
    )


# JWTトークンの検証を行うデコレータ
def require_jwt_auth(f):
    """
    JWTトークンの検証を行うデコレータ
    """
    def decorated_function(*args, **kwargs):
        token = request.args.get('token')
        if not token:
            return "認証が必要です", 401
        
        payload = verify_jwt_token(token)
        if not payload:
            return "無効なトークンまたは期限切れです", 401
        
        # JWTペイロードからリクエストパラメータを設定
        for key, value in payload.items():
            if key != 'exp':  # expは有効期限なので除外
                request.jwt_data = getattr(request, 'jwt_data', {})
                request.jwt_data[key] = value
        
        return f(*args, **kwargs)
    
    # FlaskでデコレータをMETHOD名に合わせて設定
    decorated_function.__name__ = f.__name__
    return decorated_function


@flask_app.route("/review_form")
@require_jwt_auth
def review_form():
    # ユーザーIDとチャンネルIDをJWTペイロードから取得
    user_id = request.jwt_data.get("user_id")
    channel_id = request.jwt_data.get("channel_id")
    
    if not user_id or not channel_id:
        return "Invalid parameters", 400
    
    # SNSアカウント情報を取得してテンプレートに渡す
    return render_template("review_form.html", 
                           user_id=user_id, 
                           channel_id=channel_id,
                           sns_accounts=SNS_ACCOUNTS)


@flask_app.route("/submit_review", methods=["POST"])
@require_jwt_auth
def submit_review():
    global review_requests
    
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
    
    # 新しいレビューリクエストの作成
    review = ReviewRequest(
        author=user_id,
        sns=sns,
        account=account,
        text=post_text,
        channel=channel_id
    )
    
    # アップロードされた画像の処理
    files = request.files.getlist("images")
    
    for file in files:
        if file and file.filename:
            try:
                # 一意のファイル名を生成
                filename = f"{review.request_id}_{uuid.uuid4()}_{file.filename}"
                file_path = os.path.join(uploads_dir, filename)
                
                # ファイルを保存
                file.save(file_path)
                logger.debug(f"画像ファイルを保存しました: {file_path}")
                
                # レビューリクエストに画像を追加
                review.add_image(filename)
                
            except Exception as e:
                logger.error(f"画像の保存に失敗しました: {e}")
                flash(f"画像 {file.filename} のアップロードに失敗しました。")
    
    # レビューリクエストを保存
    review_requests[review.request_id] = review
    
    # Slackにメッセージを投稿
    update_review_message(review)
    
    # 送信完了画面に遷移
    return render_template("submission_success.html")

@flask_app.route("/preview/<request_id>")
@require_jwt_auth
def preview_post(request_id):
    # JWTトークンからリクエストIDを検証
    token_request_id = request.jwt_data.get("request_id")
    
    if token_request_id != request_id:
        return "不正なアクセスです", 403
        
    if request_id not in review_requests:
        return "投稿が見つかりません", 404
    
    review = review_requests[request_id]
    
    # 画像取得用の新しいトークンを生成（request_idを含める）
    image_token = generate_jwt_token({"request_id": request_id})
    
    return render_template("preview.html", 
                          review=review,
                          request_id=request_id,
                          image_token=image_token)

@flask_app.route("/image/<request_id>/<filename>")
@require_jwt_auth
def get_image(request_id, filename):
    """画像をダウンロードするエンドポイント"""
    # JWTトークンからリクエストIDを検証
    token_request_id = request.jwt_data.get("request_id")
    
    if token_request_id != request_id:
        return "不正なアクセスです", 403
        
    if request_id not in review_requests:
        return "投稿が見つかりません", 404
    
    review = review_requests[request_id]
    
    # リクエストに関連する画像か確認
    if filename not in review.images:
        return "画像が見つかりません", 404
    
    file_path = os.path.join(uploads_dir, filename)
    if not os.path.exists(file_path):
        return "ファイルが見つかりません", 404
    
    return send_file(file_path)


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
    print(f"JWT有効期限: {JWT_EXPIRES_IN}秒")
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