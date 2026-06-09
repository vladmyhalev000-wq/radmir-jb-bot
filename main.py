import os
import re
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", "5952642946"))

FORUM_URL = os.getenv(
    "FORUM_URL",
    "https://forum.radmir.games/forums/%D0%96%D0%B0%D0%BB%D0%BE%D0%B1%D1%8B-%D0%BD%D0%B0-%D0%B8%D0%B3%D1%80%D0%BE%D0%BA%D0%BE%D0%B2-%D0%BD%D0%B5-%D1%81%D0%BE%D1%81%D1%82%D0%BE%D1%8F%D1%89%D0%B8%D1%85-%D0%B2%D0%BE-%D1%84%D1%80%D0%B0%D0%BA%D1%86%D0%B8%D1%8F%D1%85.237/"
)

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
PAGES_TO_CHECK = int(os.getenv("PAGES_TO_CHECK", "2"))
MAX_TOPICS = int(os.getenv("MAX_TOPICS", "25"))

CLOSED_PREFIXES = ("ОДОБРЕНО", "ОТКАЗАНО")
ADMIN_WORDS = ("Администратор", "Главный администратор", "Зам. главного администратора", "SERVER 06")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
DB_PATH = "bot.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            url TEXT PRIMARY KEY,
            title TEXT,
            last_base_time TEXT,
            sent_22 INTEGER DEFAULT 0,
            sent_23 INTEGER DEFAULT 0,
            sent_24 INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def parse_relative_time(text):
    now = datetime.now()
    text = " ".join(text.split())

    if "Только что" in text:
        return now

    m = re.search(r"(\d+)\s*мин", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    m = re.search(r"(\d+)\s*ч", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    m = re.search(r"Сегодня в\s*(\d{1,2}):(\d{2})", text)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)

    m = re.search(r"Вчера в\s*(\d{1,2}):(\d{2})", text)
    if m:
        d = now - timedelta(days=1)
        return d.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)

    m = re.search(r"(\d{1,2})\s+([А-Яа-я]+)\s+(\d{4})(?:\s*в\s*(\d{1,2}):(\d{2}))?", text)
    months = {"янв":1,"фев":2,"мар":3,"апр":4,"мая":5,"май":5,"июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12}
    if m:
        day = int(m.group(1)); mon_txt = m.group(2).lower(); year = int(m.group(3))
        hour = int(m.group(4) or 0); minute = int(m.group(5) or 0)
        for k, v in months.items():
            if mon_txt.startswith(k):
                return datetime(year, v, day, hour, minute)

    return None


def format_left(base_time):
    left = timedelta(hours=24) - (datetime.now() - base_time)
    total_minutes = int(left.total_seconds() // 60)

    if total_minutes <= 0:
        overdue = abs(total_minutes)
        return f"🚨 ПРОСРОК уже {overdue // 60}ч {overdue % 60}м"

    return f"⏳ До просрока: {total_minutes // 60}ч {total_minutes % 60}м"


def get_pages():
    urls = [FORUM_URL]
    for p in range(2, PAGES_TO_CHECK + 1):
        urls.append(FORUM_URL.rstrip("/") + f"/page-{p}")
    return urls


def clean_title(title):
    title = " ".join(title.split())
    title = re.sub(r"^(ОДОБРЕНО|ОТКАЗАНО|В РАССМОТРЕНИИ)\s*", "", title, flags=re.I)
    return title.strip(" -|")


def parse_forum_page(html):
    soup = BeautifulSoup(html, "html.parser")
    topics = {}

    for a in soup.select('a[href*="/threads/"]'):
        href = a.get("href", "")
        title = clean_title(a.get_text(" ", strip=True))

        if not title or "/threads/" not in href:
            continue

        bad_titles = ("вперёд", "назад", "следующая", "предыдущая")
        if title.lower() in bad_titles:
            continue

        if "Правила подачи" in title:
            continue

        url = urljoin(FORUM_URL, href.split("?")[0]).rstrip("/")
        topics[url] = {"title": title, "url": url, "created_at": None}

    return list(topics.values())[:MAX_TOPICS]


def is_topic_closed(soup):
    h = soup.select_one("h1, .p-title-value")
    head_text = h.get_text(" ", strip=True).upper() if h else ""

    # Берём именно верхнюю часть страницы, чтобы не словить "одобрено" внутри старых сообщений.
    page_text = soup.get_text(" ", strip=True).upper()[:2500]

    return any(p in head_text or p in page_text for p in CLOSED_PREFIXES)


def get_topic_title(soup, fallback):
    h = soup.select_one("h1, .p-title-value")
    if h:
        t = clean_title(h.get_text(" ", strip=True))
        if t:
            return t
    if soup.title and soup.title.string:
        t = clean_title(soup.title.string.replace("| RADMIR RP", "").replace("| RADMIR", ""))
        if t:
            return t
    return fallback


def is_admin_message(block):
    text = block.get_text(" ", strip=True)
    return any(word.lower() in text.lower() for word in ADMIN_WORDS)


def message_time(block):
    # XenForo часто хранит timestamp в data-time.
    time_el = block.select_one("time")
    if time_el:
        if time_el.get("data-time"):
            try:
                return datetime.fromtimestamp(int(time_el["data-time"]))
            except Exception:
                pass

        txt = time_el.get_text(" ", strip=True)
        dt = parse_relative_time(txt)
        if dt:
            return dt

    return parse_relative_time(block.get_text(" ", strip=True))


def get_topic_info(topic_url, fallback_title):
    html = fetch(topic_url)
    soup = BeautifulSoup(html, "html.parser")

    if is_topic_closed(soup):
        return None

    title = get_topic_title(soup, fallback_title)

    admin_times = []
    all_times = []

    for msg in soup.select("article.message, .message"):
        dt = message_time(msg)
        if dt:
            all_times.append(dt)

        if is_admin_message(msg) and dt:
            admin_times.append(dt)

    last_admin = max(admin_times) if admin_times else None

    if last_admin:
        return {
            "title": title,
            "url": topic_url,
            "base_time": last_admin,
            "from_admin": True
        }

    # Если админ не отвечал — считаем от создания темы/первого сообщения.
    if all_times:
        return {
            "title": title,
            "url": topic_url,
            "base_time": min(all_times),
            "from_admin": False
        }

    return None


def collect_complaints():
    topics = {}
    for page in get_pages():
        try:
            for t in parse_forum_page(fetch(page)):
                topics[t["url"]] = t
        except Exception as e:
            logging.exception("Forum page error %s: %s", page, e)

    complaints = []
    for t in list(topics.values())[:MAX_TOPICS]:
        try:
            info = get_topic_info(t["url"], t["title"])
            if info:
                complaints.append(info)
        except Exception as e:
            logging.exception("Topic error %s: %s", t.get("url"), e)

    complaints.sort(key=lambda x: x["base_time"])
    return complaints


async def notify(text):
    if bot:
        await bot.send_message(TELEGRAM_ID, text, disable_web_page_preview=True)


async def check_once():
    conn = db()
    cur = conn.cursor()

    for c in collect_complaints():
        base_time = c["base_time"]
        base_iso = base_time.isoformat(timespec="seconds")

        cur.execute("SELECT last_base_time, sent_22, sent_23, sent_24 FROM topics WHERE url=?", (c["url"],))
        row = cur.fetchone()

        if not row:
            sent_22 = sent_23 = sent_24 = 0
            cur.execute(
                "INSERT INTO topics(url,title,last_base_time,sent_22,sent_23,sent_24) VALUES(?,?,?,?,?,?)",
                (c["url"], c["title"], base_iso, 0, 0, 0)
            )
        else:
            old_base, sent_22, sent_23, sent_24 = row
            if old_base != base_iso:
                sent_22 = sent_23 = sent_24 = 0
                cur.execute(
                    "UPDATE topics SET title=?, last_base_time=?, sent_22=0, sent_23=0, sent_24=0 WHERE url=?",
                    (c["title"], base_iso, c["url"])
                )

        conn.commit()
        hours = (datetime.now() - base_time).total_seconds() / 3600

        if hours >= 24 and not sent_24:
            await notify(f"🚨 ЖБ просрочена:\n{c['title']}\n{c['url']}")
            cur.execute("UPDATE topics SET sent_24=1 WHERE url=?", (c["url"],))
        elif hours >= 23 and not sent_23:
            await notify(f"⚠️ Через 1 час просрок ЖБ:\n{c['title']}\n{c['url']}")
            cur.execute("UPDATE topics SET sent_23=1 WHERE url=?", (c["url"],))
        elif hours >= 22 and not sent_22:
            await notify(f"⚠️ Через 2 часа просрок ЖБ:\n{c['title']}\n{c['url']}")
            cur.execute("UPDATE topics SET sent_22=1 WHERE url=?", (c["url"],))

        conn.commit()

    conn.close()


async def checker_loop():
    while True:
        await check_once()
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


@dp.message()
async def commands(message: types.Message):
    if message.text == "/start":
        await message.answer("Бот работает. Команды: /check, /list, /debug, /id")
        return

    if message.text == "/check":
        await message.answer("Проверяю форум...")
        await check_once()
        await message.answer("Проверка завершена.")
        return

    if message.text == "/list":
        await message.answer("Собираю список ЖБ...")
        complaints = collect_complaints()

        if not complaints:
            await message.answer("Жалоб в рассмотрении не найдено.")
            return

        parts = ["📋 Жалобы в рассмотрении:"]
        for i, c in enumerate(complaints[:20], start=1):
            source = "от последнего ответа админа" if c["from_admin"] else "от создания темы"
            parts.append(
                f"\n{i}. {c['title']}\n"
                f"{format_left(c['base_time'])}\n"
                f"Счёт: {source}\n"
                f"{c['url']}"
            )

        if len(complaints) > 20:
            parts.append(f"\nПоказаны первые 20 из {len(complaints)}.")

        await message.answer("\n".join(parts), disable_web_page_preview=True)
        return

    if message.text == "/debug":
        topics = []
        for page in get_pages():
            try:
                topics.extend(parse_forum_page(fetch(page)))
            except Exception as e:
                await message.answer(f"Ошибка страницы: {e}")
                return

        if not topics:
            await message.answer("Debug: ссылок на темы не найдено.")
            return

        msg = "Debug: найденные темы:\n"
        for t in topics[:10]:
            msg += f"\n- {t['title']}\n{t['url']}\n"
        await message.answer(msg, disable_web_page_preview=True)
        return

    if message.text == "/id":
        await message.answer(str(message.chat.id))
        return


async def handle_ping(request):
    return web.Response(text="OK")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Add it in Render Environment.")

    app = web.Application()
    app.router.add_get("/", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(checker_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
