import base64
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Deque, Dict, List
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, jsonify, request
from openai import OpenAI

# =========================================================
# おたすけさん Ver1.1
# ・LINE向け短文返信
# ・最大3つの吹き出し
# ・ユーザーごとの直近会話を一時保持
# ・エラー時の返信／pushフォールバック
# ・Renderログ強化
#
# 注意：
# 会話履歴はサーバーのメモリ内にだけ保存します。
# Renderの再起動・再デプロイ・スリープ復帰などで消えることがあります。
# =========================================================

app = Flask(__name__)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = app.logger

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

MAX_HISTORY_MESSAGES = 8
MAX_REPLY_BUBBLES = 3
MAX_BUBBLE_CHARS = 900
EVENT_CACHE_LIMIT = 500

SYSTEM_PROMPT = """
【現在日時】
現在日時はシステムから渡された日本時間（Asia/Tokyo）を基準に判断してください。
日付・曜日・時刻・「今日」「明日」「昨日」「来週」「今月」などの質問は、必ず現在日時を基準に回答してください。
現在日時が分からない場合は推測で答えず、「現在日時を確認できません」と伝えてください。
あなたはLINE相談ボット「おたすけさん」です。
日本語で、親しみやすく、穏やかで、押しつけない口調で回答してください。

【基本姿勢】
・最初に相手の話を短く受け止める。
・「大丈夫ですか？」を安易に繰り返さない。
・分からないことを知っているように断言しない。
・個人情報や秘密情報をむやみに尋ねない。
・現在地や直前の話題など、会話履歴にある情報を自然に引き継ぐ。

【LINEでの返信ルール】
・1回の回答は短くする。
・原則として、次の順番で最大3つの短い吹き出しにする。
  1. 共感・受け止め
  2. 確認質問または要点
  3. 次にできる行動
・質問は一度に原則1つまで。
・詳しい説明は、相手が求めた場合に追加する。
・各吹き出しの区切りには、必ず半角の ||| を使う。
・箇条書きを多用せず、スマートフォンで読みやすくする。
・回答全体を必要以上に長くしない。

【安全上のルール】
・医療、介護、法律、お金など重要な相談では断定を避ける。
・緊急性や生命の危険が疑われる場合は、119番など適切な緊急窓口への連絡を優先する。
・自傷、他害、重大な犯罪などの危険がある場合は、安全確保と緊急支援を優先する。

【現在の機能上の制限】
・リアルタイムの天気、電車時刻、道路状況、店舗の営業状況などは、
  外部APIなしで正確に取得することはできない。
・ただし、現在日時はシステムから渡された日本時間を使って回答する。
・日付、曜日、現在時刻、「今日」「明日」「昨日」「来週」などは、
  システムから渡された現在日時を基準に答える。
・最新情報を取得できない場合は、推測で答えず、
  「現在は最新情報を直接確認できません」と短く伝える。
・ただし、直前に出た地名や話題は忘れず、会話として自然につなげる。
【出力例】
それはつらいですね。|||動くときに強く痛みますか？|||無理に動かさず、急な強い痛みやしびれがあれば医療機関への相談も検討してください。
""".strip()

# OpenAIクライアントは使い回す
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ユーザーごとの一時的な会話履歴
conversation_history: Dict[str, Deque[dict]] = defaultdict(
    lambda: deque(maxlen=MAX_HISTORY_MESSAGES)
)
history_lock = threading.Lock()

# LINEのWebhook再送による二重返信を避けるための簡易キャッシュ
processed_event_ids: Deque[str] = deque(maxlen=EVENT_CACHE_LIMIT)
processed_event_set = set()
event_lock = threading.Lock()


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


def get_conversation_key(event: dict) -> str:
    """ユーザー・グループ・ルームごとに会話を分ける。"""
    source = event.get("source", {})
    source_type = source.get("type", "unknown")

    if source_type == "user":
        return f"user:{source.get('userId', 'unknown')}"

    if source_type == "group":
        return (
            f"group:{source.get('groupId', 'unknown')}:"
            f"user:{source.get('userId', 'unknown')}"
        )

    if source_type == "room":
        return (
            f"room:{source.get('roomId', 'unknown')}:"
            f"user:{source.get('userId', 'unknown')}"
        )

    return "unknown"


def get_push_target(event: dict) -> str:
    """replyTokenが使えない場合にpush送信する宛先を取得する。"""
    source = event.get("source", {})
    return (
        source.get("userId")
        or source.get("groupId")
        or source.get("roomId")
        or ""
    )


def is_duplicate_event(event: dict) -> bool:
    """同じWebhookイベントの二重処理を避ける。"""
    event_id = event.get("webhookEventId")
    if not event_id:
        return False

    with event_lock:
        if event_id in processed_event_set:
            return True

        if len(processed_event_ids) >= EVENT_CACHE_LIMIT:
            old_id = processed_event_ids.popleft()
            processed_event_set.discard(old_id)

        processed_event_ids.append(event_id)
        processed_event_set.add(event_id)

    return False


def load_history(conversation_key: str) -> List[dict]:
    with history_lock:
        return list(conversation_history[conversation_key])


def save_history(conversation_key: str, role: str, content: str) -> None:
    with history_lock:
        conversation_history[conversation_key].append(
            {"role": role, "content": content}
        )


def clear_history(conversation_key: str) -> None:
    with history_lock:
        conversation_history.pop(conversation_key, None)


def split_long_text(text: str, limit: int = MAX_BUBBLE_CHARS) -> List[str]:
    """長すぎる文章を、文の切れ目を優先して分割する。"""
    text = text.strip()
    if not text:
        return []

    if len(text) <= limit:
        return [text]

    sentences = re.split(r"(?<=[。！？!?])", text)
    chunks: List[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(current) + len(sentence) <= limit:
            current += sentence
            continue

        if current:
            chunks.append(current.strip())
            current = ""

        while len(sentence) > limit:
            chunks.append(sentence[:limit].strip())
            sentence = sentence[limit:]

        current = sentence

    if current:
        chunks.append(current.strip())

    return chunks


def format_line_messages(answer: str) -> List[str]:
    """AIの回答を最大3つのLINE吹き出しへ整形する。"""
    answer = (answer or "").strip()
    if not answer:
        return ["うまく回答を作れませんでした。もう一度送ってください。"]

    raw_parts = [part.strip() for part in answer.split("|||") if part.strip()]

    # AIが区切りを守らなかった場合も読みやすく分割する
    if len(raw_parts) <= 1:
        raw_parts = split_long_text(answer)

    messages: List[str] = []
    for part in raw_parts:
        messages.extend(split_long_text(part))

    messages = [message[:MAX_BUBBLE_CHARS] for message in messages if message]

    if not messages:
        messages = ["うまく回答を作れませんでした。もう一度送ってください。"]

    # LINEは最大5件だが、おたすけさんでは最大3件に抑える
    if len(messages) > MAX_REPLY_BUBBLES:
        remaining = " ".join(messages[MAX_REPLY_BUBBLES - 1 :])
        messages = messages[: MAX_REPLY_BUBBLES - 1]
        messages.append(remaining[:MAX_BUBBLE_CHARS])

    return messages


def create_ai_reply(conversation_key: str, user_text: str) -> str:
    """直近の会話履歴を含めてOpenAI APIから返信を作る。"""
    if not openai_client:
        return (
            "ただいまAIの設定準備中です。|||"
            "少し時間をおいて、もう一度お試しください。"
        )

    history = load_history(conversation_key)
    api_input = history + [{"role": "user", "content": user_text}]

    logger.info(
        "STEP 3 OpenAIへ送信 conversation=%s history=%d model=%s",
        conversation_key,
        len(history),
        OPENAI_MODEL,
    )

    # 日本時間の現在日時
    now = datetime.now(ZoneInfo("Asia/Tokyo"))

    weekdays = [
        "月曜日",
        "火曜日",
        "水曜日",
        "木曜日",
        "金曜日",
        "土曜日",
        "日曜日",
    ]
    weekday = weekdays[now.weekday()]

    current_datetime = (
        f"{now.year}年{now.month}月{now.day}日（{weekday}） "
        f"{now.strftime('%H:%M')}"
    )

    instructions = (
        SYSTEM_PROMPT
        + "\n\n【現在日時】\n"
        + f"現在日時：{current_datetime}（日本時間）\n"
        + "「今日」「昨日」「明日」「曜日」「今月」「来月」「今年」などは、"
        + "この日時を基準に回答してください。"
    )

    response = openai_client.responses.create(
        model=OPENAI_MODEL,
        instructions=instructions,
        input=api_input,
        max_output_tokens=450,
    )

    reply = (response.output_text or "").strip()

    if not reply:
        raise RuntimeError("OpenAIから空の回答が返されました。")

    save_history(conversation_key, "user", user_text)
    save_history(conversation_key, "assistant", reply)

    return reply


def line_headers() -> dict:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")

    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def to_line_message_objects(texts: List[str]) -> List[dict]:
    return [{"type": "text", "text": text} for text in texts[:5]]


def reply_to_line(reply_token: str, texts: List[str]) -> None:
    """replyTokenを使ってLINEへ返信する。"""
    response = requests.post(
        LINE_REPLY_URL,
        headers=line_headers(),
        json={
            "replyToken": reply_token,
            "messages": to_line_message_objects(texts),
        },
        timeout=20,
    )

    if not response.ok:
        logger.error(
            "LINE reply失敗 status=%s body=%s",
            response.status_code,
            response.text[:1000],
        )

    response.raise_for_status()


def push_to_line(target: str, texts: List[str]) -> None:
    """replyTokenが期限切れ等の場合、push送信を試す。"""
    if not target:
        raise RuntimeError("push送信先を取得できませんでした。")

    response = requests.post(
        LINE_PUSH_URL,
        headers=line_headers(),
        json={
            "to": target,
            "messages": to_line_message_objects(texts),
        },
        timeout=20,
    )

    if not response.ok:
        logger.error(
            "LINE push失敗 status=%s body=%s",
            response.status_code,
            response.text[:1000],
        )

    response.raise_for_status()


def send_with_fallback(
    reply_token: str,
    push_target: str,
    texts: List[str],
) -> None:
    """通常返信に失敗した場合、push送信へ切り替える。"""
    try:
        reply_to_line(reply_token, texts)
        logger.info("STEP 5 LINE reply成功 bubbles=%d", len(texts))
    except Exception:
        logger.exception("LINE replyに失敗しました。push送信を試します。")
        push_to_line(push_target, texts)
        logger.info("STEP 5 LINE push成功 bubbles=%d", len(texts))


@app.get("/")
def health_check():
    return jsonify(
        status="ok",
        version="1.1",
        service="おたすけさんLINEボット",
        message="サーバーは正常に動いています。",
        openai_configured=bool(OPENAI_API_KEY),
        line_configured=bool(
            LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN
        ),
    )


def process_event_async(event: dict) -> None:
    """Webhookへの200応答後に、実際の返信処理を行う。"""
    try:
        if is_duplicate_event(event):
            logger.info("重複イベントのためスキップしました。")
            return

        if event.get("type") != "message":
            logger.info("message以外のイベントをスキップしました。")
            return

        message = event.get("message", {})
        if message.get("type") != "text":
            logger.info("テキスト以外のメッセージをスキップしました。")
            return

        push_target = get_push_target(event)
        conversation_key = get_conversation_key(event)
        user_text = (message.get("text") or "").strip()

        if not push_target or not user_text:
            logger.warning("push送信先または本文が空のためスキップしました。")
            return

        logger.info(
            "非同期処理開始 conversation=%s text_length=%d",
            conversation_key,
            len(user_text),
        )

        if user_text in {"会話をリセット", "履歴を消して", "リセット"}:
            clear_history(conversation_key)
            push_to_line(
                push_target,
                format_line_messages(
                    "分かりました。|||ここまでの会話をリセットしました。"
                ),
            )
            return

        answer = create_ai_reply(conversation_key, user_text)
        logger.info("AI回答生成成功 length=%d", len(answer))

        line_messages = format_line_messages(answer)
        push_to_line(push_target, line_messages)

        logger.info(
            "LINE push成功 conversation=%s bubbles=%d",
            conversation_key,
            len(line_messages),
        )

    except Exception:
        logger.exception("非同期返信処理でエラーが発生しました。")

        try:
            push_target = get_push_target(event)
            if push_target:
                push_to_line(
                    push_target,
                    [
                        "うまく処理できませんでした。",
                        "少し時間をおいて、もう一度送ってください。",
                    ],
                )
        except Exception:
            logger.exception("フォールバック送信にも失敗しました。")


@app.post("/callback")
def callback():
    started_at = time.time()
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    logger.info("STEP 1 Webhook受信 bytes=%d", len(raw_body))

    if not verify_line_signature(raw_body, signature):
        logger.warning("LINE署名の確認に失敗しました。")
        abort(400)

    payload = request.get_json(silent=True) or {}
    events = payload.get("events", [])

    logger.info("STEP 2 署名確認成功 events=%d", len(events))

    for event in events:
        threading.Thread(
            target=process_event_async,
            args=(event,),
            daemon=True,
        ).start()

    logger.info(
        "Webhook即時応答 elapsed=%.3fs events=%d",
        time.time() - started_at,
        len(events),
    )

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
