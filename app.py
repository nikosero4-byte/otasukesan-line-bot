import base64
import hashlib
import hmac
import logging
import os

import requests
from flask import Flask, abort, jsonify, request
from openai import OpenAI

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

SYSTEM_PROMPT = """
あなたはLINE相談ボット「おたすけさん」です。
日本語で、親しみやすく、落ち着いた口調で回答してください。
最初に相手の話を受け止め、要点を整理し、次に取れる行動を分かりやすく示してください。
回答はLINEで読みやすい長さにし、必要以上に長くしないでください。
医療・介護・法律・お金など重要な相談では、断定せず、専門家や公的窓口への確認も案内してください。
緊急性や生命の危険が疑われる場合は、119番など適切な緊急窓口への連絡を優先して案内してください。
個人情報や秘密情報をむやみに尋ねないでください。
現在は試作版で、会話内容を長期記憶する機能はありません。
""".strip()


def verify_line_signature(body: bytes, signature: str) -> bool:
    """LINEから届いたWebhookかを署名で確認する。"""
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def create_ai_reply(user_text: str) -> str:
    """OpenAI APIで返信文を作る。"""
    if not OPENAI_API_KEY:
        return "ただいまAIの設定準備中です。少し時間をおいて、もう一度お試しください。"

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=user_text,
        max_output_tokens=500,
    )
    reply = (response.output_text or "").strip()

    if not reply:
        return "うまく回答を作れませんでした。少し言い方を変えて、もう一度送ってください。"

    # LINEのテキスト上限に余裕を持たせる
    return reply[:4500]


def reply_to_line(reply_token: str, text: str) -> None:
    """LINE Messaging APIで返信する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")

    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=15,
    )
    response.raise_for_status()


@app.get("/")
def health_check():
    return jsonify(
        status="ok",
        service="おたすけさんLINEボット",
        message="サーバーは正常に動いています。",
    )


@app.post("/callback")
def callback():
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(raw_body, signature):
        app.logger.warning("LINE署名の確認に失敗しました。")
        abort(400)

    payload = request.get_json(silent=True) or {}

    # LINE Developersの「検証」では events が空の場合がある
    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        reply_token = event.get("replyToken")
        user_text = (message.get("text") or "").strip()
        if not reply_token or not user_text:
            continue

        try:
            answer = create_ai_reply(user_text)
            reply_to_line(reply_token, answer)
        except Exception:
            app.logger.exception("返信処理でエラーが発生しました。")
            # LINEには200を返し、同じWebhookの再送が繰り返されるのを避ける

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
