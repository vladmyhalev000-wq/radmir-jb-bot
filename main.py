import os
import re
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiohttp import web
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", "5952642946"))

FORUM_URL = os.getenv(
    "FORUM_URL",
    "https://forum.radmir.games/forums/Жалобы-на-игроков-не-состоящих-во-фракциях.237/"
)

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
PAGES_TO_CHECK = int(os.getenv("PAGES_TO_CHECK", "2"))
MAX_TOPICS = int(os.getenv("MAX_TOPICS", "25"))

CLOSED_PREFIXES = ("ОДОБРЕНО", "ОТКАЗАНО")
ADMIN_WORDS = ("Администратор", "Главный администратор", "Зам. главного администратора", "SERVER 06")

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
        day = int(m.group(1))
        mon_txt = m.group(2).lower()
        year = int(m.group(3))
        hour = int(m.group(4) or 0)
        minute = int(m.group(5) or 0)

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


def forum_page_url(page_num):
    if page_num <= 1:
        return FORUM_URL
    return FORUM_URL.rstrip("/") + f"/page-{page_num}"


def clean_title(title):
    title = " ".join(title.split())
    title = re.sub(r"^(ОДОБРЕНО|ОТКАЗАНО|В РАССМОТРЕНИИ)\s*", "", title, flags=re.I)
    return title.strip(" -|")


async def get_html(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.wait_for_timeout(3000)

    for _ in range(3):
        try:
            return await page.content()
        except Exception:
            await page.wait_for_timeout(2000)

    return await page.content()


def parse_forum_html(html):
    soup = BeautifulSoup(html, "html.parser")
    topics = {}

    for row in soup.select(".structItem--thread, .structItem"):
        text = row.get_text(" ", strip=True)
        upper = text.upper()

        if any(p in upper for p in CLOSED_PREFIXES):
            continue

        a = row.select_one('a[href*="/threads/"]')
        if not a:
            continue

        title = clean_title(a.get_text(" ", strip=True))
        href = a.get("href", "")

        if not title or "Правила подачи" in title:
            continue

        if href.startswith("/"):
            url = "https://forum.radmir.games" + href
        elif href.startswith("http"):
            url = href
        else:
            url = "https://forum.radmir.games/" + href

        url = url.split("?")[0].rstrip("/")
        created_at = parse_relative_time(text)

        topics[url] = {
            "title": title,
            "url": url,
            "created_at": created_at,
        }

    # fallback если классы отличаются
    if not topics:
        for a in soup.select('a[href*="/threads/"]'):
            title = clean_title(a.get_text(" ", strip=True))
            href = a.get("href", "")

            if not title or "Правила подачи" in title:
                continue

            parent = a
            for _ in range(8):
                parent = parent.parent
                if not parent:
                    break
                txt = parent.get_text(" ", strip=True)
                if "Ответы:" in txt and "Просмотры:" in txt:
                    if any(p in txt.upper() for p in CLOSED_PREFIXES):
                        break

                    if href.startswith("/"):
                        url = "https://forum.radmir.games" + href
                    elif href.startswith("http"):
                        url = href
                    else:
                        url = "https://forum.radmir.games/" + href

                    url = url.split("?")[0].rstrip("/")
                    topics[url] = {
                        "title": title,
                        "url": url,
                        "created_at": parse_relative_time(txt),
                    }
                    break

    return list(topics.values())[:MAX_TOPICS]


def is_topic_closed(soup):
    page_text = soup.get_text(" ", strip=True).upper()[:3000]
    return any(p in page_text for p in CLOSED_PREFIXES)


def is_admin_message(block):
    text = block.get_text(" ", strip=True)
    return any(word.lower() in text.lower() for word in ADMIN_WORDS)


def message_time(block):
    time_el = block.select_one("time")
    if time_el:
        if time_el.get("data-time"):
            try:
                return datetime.fromtimestamp(int(time_el["data-time"]))
            except Exception:
                pass

        dt = parse_relative_time(time_el.get_text(" ", strip=True))
        if dt:
            return dt

    return parse_relative_time(block.get_text(" ", strip=True))


def parse_topic_html(html, fallback_title, url):
    soup = BeautifulSoup(html, "html.parser")

    if is_topic_closed(soup):
        return None

    h = soup.select_one("h1, .p-title-value")
    title = clean_title(h.get_text(" ", strip=True)) if h else fallback_title

    admin_times = []
    all_times = []

    for msg in soup.select("article.message, .message"):
        dt = message_time(msg)
        if dt:
            all_times.append(dt)

        if dt and is_admin_message(msg):
            admin_times.append(dt)

    if admin_times:
        return {
            "title": title,
            "url": url,
            "base_time": max(admin_times),
            "from_admin": True,
        }

    if all_times:
        return {
            "title": title,
            "url": url,
            "base_time": min(all_times),
            "from_admin": False,
        }

    return None


async def collect_complaints_async(debug=False):
    complaints = []
    seen = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
            locale="ru-RU",
            viewport={"width": 1366, "height": 768},
        )

        for n in range(1, PAGES_TO_CHECK + 1):
            try:
                html = await get_html(page, forum_page_url(n))
                for t in parse_forum_html(html):
                    seen[t["url"]] = t
            except Exception as e:
                logging.exception("Forum page error: %s", e)

        for t in list(seen.values())[:MAX_TOPICS]:
            try:
                html = await get_html(page, t["url"])
                info = parse_topic_html(html, t["title"], t["url"])
                if info:
                    complaints.append(info)
            except Exception as e:
                logging.exception("Topic error %s: %s", t.get("url"), e)

        await browser.close()

    complaints.sort(key=lambda x: x["base_time"])
    return complaints, list(seen.values())


async def notify(text):
    if bot:
        await bot.send_message(TELEGRAM_ID, text, disable_web_page_preview=True)


async def check_once():
    conn = db()
    cur = conn.cursor()

    complaints, _ = await collect_complaints_async()

    for c in complaints:
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
        try:
            await check_once()
        except Exception as e:
            logging.exception("Checker loop error: %s", e)
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
        complaints, _ = await collect_complaints_async()

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

        await message.answer("\n".join(parts), disable_web_page_preview=True)
        return

    if message.text == "/debug":
        await message.answer("Проверяю, какие темы вижу...")
        complaints, topics = await collect_complaints_async(debug=True)

        if not topics:
            await message.answer("Debug: тем на странице не вижу. Возможно, форум блокирует браузер Render.")
            return

        msg = f"Debug: вижу тем: {len(topics)}, в работе: {len(complaints)}\n"
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
