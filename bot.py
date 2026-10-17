"""Автономный новостной бот: RSS -> DeepSeek -> Telegram.
Запускается GitHub Actions каждые ~10 минут (см. run.yml)."""
import os
import re
import json
import html
import hashlib
import datetime as dt

import requests
import feedparser

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]  # например @my_channel
ADMIN = os.environ["ADMIN_ID"]  # ваш числовой ID
DS_KEY = os.environ["DEEPSEEK_API_KEY"]

MAX_POSTS_PER_DAY = 8      # лимит публикаций в сутки (по МСК)
CANDIDATES_PER_RUN = 4     # сколько новостей проверяем за один запуск
MAX_AGE_HOURS = 4          # старше этого возраста новости не берём
PENDING_TTL_HOURS = 6      # сколько ждёт решения модерации
STATE_FILE = "state.json"

# Источники (название, адрес RSS). Нерабочие бот просто пропускает.
FEEDS = [
    ("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/section-economics.xml"),
    ("Интерфакс", "https://www.interfax.ru/rss.asp"),
    ("Ведомости", "https://www.vedomosti.ru/rss/news"),
    ("Onliner", "https://www.onliner.by/feed"),
    ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147"),
]

SYSTEM = """Ты редактор экономического Telegram-канала для читателей 18-25 лет из России и Беларуси. Тебе дают одну новость. Верни ТОЛЬКО json-объект (JSON) с полями:
skip (bool), reason (строка), controversial (bool), breaking (bool), title (строка), text (строка), hashtags (список из 2-4 слов).

Правила:
1. skip=true, если новость не про экономику, финансы, бизнес, рынки, цены, работу, налоги; если она про политику, войну и военные темы, криминал, казино и ставки; если тема уже есть в списке недавних заголовков; если в тексте мало фактов.
2. Пиши только по фактам из текста новости. Ничего не выдумывай и не добавляй цифр, которых нет в источнике.
3. Стиль: коротко, по делу, простым языком для молодёжи, 1-3 уместных эмодзи. Примерно в одной новости из пяти можно добавить лёгкую шутку, но не про трагедии и потери людей.
4. Если новость поворотная (резкое изменение курса валют, ключевой ставки, цен на нефть, крупное решение регулятора), добавь в конце 1-2 предложения, начиная со слов «Что дальше:», с осторожным предположением и причинами. Не давай финансовых советов.
5. Длина text: срочная новость 1-2 предложения, обычная до 450 знаков, поворотная до 700.
6. breaking=true для срочных новостей, которые только что произошли и могут быть неполными.
7. controversial=true, если это слухи, непроверенные заявления, санкции, спорные решения властей, громкие обвинения или прогнозы, которые нельзя подать как факт.
8. title до 80 знаков. Хэштеги без символа #, одним словом.
Если skip=true, остальные поля можно оставить пустыми."""


def now():
    return dt.datetime.now(dt.timezone.utc)


def safe(err):
    text = str(err)
    for secret in (TOKEN, DS_KEY):
        text = text.replace(secret, "***")
    return text


# ---------- состояние ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen", [])
    s.setdefault("recent", [])
    s.setdefault("pending", {})
    s.setdefault("offset", 0)
    s.setdefault("day", "")
    s.setdefault("count", 0)
    s.setdefault("intro", False)
    return s


def save_state(s):
    s["seen"] = s["seen"][-3000:]
    s["recent"] = s["recent"][-20:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


# ---------- Telegram ----------
def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def publish(text):
    tg("sendMessage", chat_id=CHANNEL, text=text, parse_mode="HTML",
       disable_web_page_preview=True)


def handle_callbacks(state):
    """Обрабатываем нажатия кнопок модерации, накопившиеся с прошлого запуска."""
    updates = tg("getUpdates", offset=state["offset"], timeout=0,
                 allowed_updates=["callback_query"])
    for u in updates:
        state["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or str(cq["from"]["id"]) != str(ADMIN):
            continue
        action, _, pid = cq.get("data", "").partition(":")
        post = state["pending"].pop(pid, None)
        if post and action == "ok":
            publish(post["text"])
            state["count"] += 1
            answer = "Опубликовано"
        elif post and action == "no":
            answer = "Отклонено"
        else:
            answer = "Уже обработано или устарело"
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
            msg = cq["message"]
            tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
               message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
        except Exception:
            pass


def expire_pending(state):
    limit = now() - dt.timedelta(hours=PENDING_TTL_HOURS)
    for pid in list(state["pending"]):
        if dt.datetime.fromisoformat(state["pending"][pid]["ts"]) < limit:
            del state["pending"][pid]


# ---------- сбор новостей ----------
def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:900]


def collect(state):
    seen = set(state["seen"])
    items, ok, bad = [], [], []
    limit = now() - dt.timedelta(hours=MAX_AGE_HOURS)
    for name, url in FEEDS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            if not feed.entries:
                raise ValueError("пусто")
        except Exception:
            bad.append(name)
            continue
        ok.append(name)
        for e in feed.entries[:15]:
            key = e.get("id") or e.get("link") or e.get("title", "")
            uid = hashlib.sha1(key.encode()).hexdigest()[:16]
            if uid in seen:
                continue
            t = e.get("published_parsed") or e.get("updated_parsed")
            ts = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else now()
            if ts < limit:
                continue
            items.append({
                "id": uid, "name": name, "ts": ts,
                "title": clean(e.get("title", "")),
                "summary": clean(e.get("summary", "")),
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items, ok, bad


# ---------- DeepSeek ----------
def ask_ai(c, recent):
    prompt = (
        f"Источник: {c['name']}\nЗаголовок: {c['title']}\nТекст: {c['summary']}\n\n"
        f"Недавние заголовки (не повторяй темы): {json.dumps(recent, ensure_ascii=False)}"
    )
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DS_KEY}"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def compose(res, source):
    title = html.escape(res["title"].strip())
    body = html.escape(res["text"].strip())
    tags = " ".join(
        "#" + re.sub(r"[^\w]", "", str(t)) for t in res.get("hashtags", [])[:4]
        if re.sub(r"[^\w]", "", str(t))
    )
    mark = "⚡️ " if res.get("breaking") else ""
    note = "\n<i>Пока не подтверждено</i>" if res.get("breaking") else ""
    return (f"{mark}<b>{title}</b>\n\n{body}{note}\n\n"
            f"{html.escape(tags)}\n<i>Источник: {html.escape(source)}</i>")


# ---------- главный цикл ----------
def main():
    state = load_state()
    today = (now() + dt.timedelta(hours=3)).strftime("%Y-%m-%d")
    if state["day"] != today:
        state["day"], state["count"] = today, 0

    handle_callbacks(state)
    expire_pending(state)
    items, ok, bad = collect(state)

    if not state["intro"]:
        tg("sendMessage", chat_id=ADMIN, text=(
            "✅ Бот запущен.\n"
            f"Источников работает: {len(ok)} из {len(FEEDS)}.\n"
            f"Не открылись: {', '.join(bad) or 'все открылись'}."))
        state["intro"] = True

    for c in items[:CANDIDATES_PER_RUN]:
        if state["count"] >= MAX_POSTS_PER_DAY:
            break
        state["seen"].append(c["id"])
        try:
            res = ask_ai(c, state["recent"])
        except Exception as e:
            print("Ошибка DeepSeek:", safe(e))
            continue
        if res.get("skip") or not res.get("title") or not res.get("text"):
            print("Пропуск:", c["title"], "|", res.get("reason"))
            save_state(state)
            continue

        text = compose(res, c["name"])
        state["recent"].append(res["title"])
        if res.get("controversial"):
            tg("sendMessage", chat_id=ADMIN, text="🕵️ На модерацию:\n\n" + text,
               parse_mode="HTML", disable_web_page_preview=True,
               reply_markup={"inline_keyboard": [[
                   {"text": "✅ Опубликовать", "callback_data": f"ok:{c['id']}"},
                   {"text": "❌ Отклонить", "callback_data": f"no:{c['id']}"},
               ]]})
            state["pending"][c["id"]] = {"text": text, "ts": now().isoformat()}
        else:
            publish(text)
            state["count"] += 1
        save_state(state)
        break  # не больше одного поста за запуск

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        try:
            tg("sendMessage", chat_id=ADMIN, text=("⚠️ Сбой бота: " + safe(err))[:3500])
        except Exception:
            pass
        raise
