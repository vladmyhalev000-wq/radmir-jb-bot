import os
import re
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
PAGES_TO_CHECK = int(os.getenv("PAGES_TO_CHECK", "1"))
MAX_TOPICS = int(os.getenv("MAX_TOPICS", "20"))

CLOSED_PREFIXES = ("ОДОБРЕНО", "ОТКАЗАНО")
ADMIN_WORDS = ("Администратор", "Главный администратор", "Зам. главного администратора", "SERVER 06")

# Разделы форума.
SECTIONS = {
    "ne_sost": {
        "name": "ЖБ на не сост",
        "url": "https://forum.radmir.games/forums/Жалобы-на-игроков-не-состоящих-во-фракциях.237/",
    },
    "oprova_crime": {
        "name": "Опры от крим. структур",
        "url": "https://forum.radmir.games/forums/Запрос-опровержений-от-криминальных-структур.1509/",
    },
    "mafia_bands": {
        "name": "ЖБ на мафии/банды",
        "url": "https://forum.radmir.games/forums/Жалобы-на-игроков-состоящих-в-мафиях-бандах.238/",
    },
}

# Кому какие разделы присылать автоматически.
# Тебе пока оставил все 3 раздела. Если хочешь только "не сост" — убери остальные ключи.
USER_SECTIONS = {
    5952642946: ["ne_sost", "oprova_crime", "mafia_bands"],
    1819044320: ["oprova_crime", "mafia_bands"],
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
DB_PATH = "bot.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            section_key TEXT,
            user_id INTEGER,
            url TEXT,
            title TEXT,
            last_base_time TEXT,
            sent_22 INTEGER DEFAULT 0,
            sent_23 INTEGER DEFAULT 0,
            sent_24 INTEGER DEFAULT 0,
            PRIMARY KEY(section_key, user_id, url)
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


def forum_page_url(section_key, page_num):
    base_url = SECTIONS[section_key]["url"]
    if page_num <= 1:
        return base_url
    return base_url.rstrip("/") + f"/page-{page_num}"


def clean_title(title):
    title = " ".join(title.split())
    title = re.sub(r"^(ОДОБРЕНО|ОТКАЗАНО|В РАССМОТРЕНИИ)\s*", "", title, flags=re.I)
    return title.strip(" -|")


async def get_html(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)

    for _ in range(3):
        try:
            return await page.content()
        except Exception:
            await page.wait_for_timeout(2000)

    return await page.content()


def make_absolute_url(href):
    if href.startswith("/"):
        url = "https://forum.radmir.games" + href
    elif href.startswith("http"):
        url = href
    else:
        url = "https://forum.radmir.games/" + href

    url = url.split("?")[0].rstrip("/")
    m = re.search(r"(\d+)$", url)
    if m:
        return "https://forum.radmir.games/threads/" + m.group(1)
    return url


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

        url = make_absolute_url(href)
        topics[url] = {
            "title": title,
            "url": url,
            "created_at": parse_relative_time(text),
        }

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

                    url = make_absolute_url(href)
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


async def collect_complaints_async(section_key):
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
                html = await get_html(page, forum_page_url(section_key, n))
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
                elif t.get("created_at"):
                    complaints.append({
                        "title": t["title"],
                        "url": t["url"],
                        "base_time": t["created_at"],
                        "from_admin": False,
                    })
            except Exception as e:
                logging.exception("Topic error %s: %s", t.get("url"), e)
                if t.get("created_at"):
                    complaints.append({
                        "title": t["title"],
                        "url": t["url"],
                        "base_time": t["created_at"],
                        "from_admin": False,
                    })

        await browser.close()

    complaints.sort(key=lambda x: x["base_time"])
    return complaints, list(seen.values())


def build_complaints_message(section_key, complaints):
    section_name = SECTIONS[section_key]["name"]

    if not complaints:
        return f"📂 {section_name}\n\nЖалоб в рассмотрении не найдено."

    parts = [f"📂 {section_name}\n📋 Жалобы в рассмотрении:"]
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

    return "\n".join(parts)


def sections_keyboard(prefix):
    buttons = []
    for key, section in SECTIONS.items():
        buttons.append([InlineKeyboardButton(text=section["name"], callback_data=f"{prefix}:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def notify(user_id, text):
    if bot:
        await bot.send_message(user_id, text, disable_web_page_preview=True)


async def check_section_for_user(user_id, section_key):
    conn = db()
    cur = conn.cursor()

    complaints, _ = await collect_complaints_async(section_key)
    section_name = SECTIONS[section_key]["name"]

    for c in complaints:
        base_time = c["base_time"]
        base_iso = base_time.isoformat(timespec="seconds")

        cur.execute(
            "SELECT last_base_time, sent_22, sent_23, sent_24 FROM topics WHERE section_key=? AND user_id=? AND url=?",
            (section_key, user_id, c["url"]),
        )
        row = cur.fetchone()

        if not row:
            sent_22 = sent_23 = sent_24 = 0
            cur.execute(
                "INSERT INTO topics(section_key,user_id,url,title,last_base_time,sent_22,sent_23,sent_24) VALUES(?,?,?,?,?,?,?,?)",
                (section_key, user_id, c["url"], c["title"], base_iso, 0, 0, 0),
            )
        else:
            old_base, sent_22, sent_23, sent_24 = row
            if old_base != base_iso:
                sent_22 = sent_23 = sent_24 = 0
                cur.execute(
                    "UPDATE topics SET title=?, last_base_time=?, sent_22=0, sent_23=0, sent_24=0 WHERE section_key=? AND user_id=? AND url=?",
                    (c["title"], base_iso, section_key, user_id, c["url"]),
                )

        conn.commit()
        hours = (datetime.now() - base_time).total_seconds() / 3600

        if hours >= 24 and not sent_24:
            await notify(user_id, f"🚨 ЖБ просрочена\n📂 {section_name}\n\n{c['title']}\n{c['url']}")
            cur.execute(
                "UPDATE topics SET sent_24=1 WHERE section_key=? AND user_id=? AND url=?",
                (section_key, user_id, c["url"]),
            )
        elif hours >= 23 and not sent_23:
            await notify(user_id, f"⚠️ Через 1 час просрок ЖБ\n📂 {section_name}\n\n{c['title']}\n{c['url']}")
            cur.execute(
                "UPDATE topics SET sent_23=1 WHERE section_key=? AND user_id=? AND url=?",
                (section_key, user_id, c["url"]),
            )
        elif hours >= 22 and not sent_22:
            await notify(user_id, f"⚠️ Через 2 часа просрок ЖБ\n📂 {section_name}\n\n{c['title']}\n{c['url']}")
            cur.execute(
                "UPDATE topics SET sent_22=1 WHERE section_key=? AND user_id=? AND url=?",
                (section_key, user_id, c["url"]),
            )

        conn.commit()

    conn.close()


async def check_all_users():
    for user_id, sections in USER_SECTIONS.items():
        for section_key in sections:
            try:
                await check_section_for_user(user_id, section_key)
            except Exception as e:
                logging.exception("Auto check error user=%s section=%s: %s", user_id, section_key, e)


async def checker_loop():
    while True:
        try:
            await check_all_users()
        except Exception as e:
            logging.exception("Checker loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


@dp.message()
async def commands(message: types.Message):
    text = (message.text or "").strip()

    if text == "/start":
        await message.answer(
            "Бот работает.\n\n"
            "Команды:\n"
            "/sections — выбрать раздел\n"
            "/check — выбрать раздел и проверить\n"
            "/list — выбрать раздел и посмотреть время\n"
            "/debug — выбрать раздел и проверить видимость тем\n"
            "/mysections — мои авто-разделы\n"
            "/id — узнать свой ID"
        )
        return

    if text in ("/sections", "/check", "/list"):
        await message.answer("Выбери раздел:", reply_markup=sections_keyboard("list"))
        return

    if text in ("/debug", "/scan"):
        await message.answer("Выбери раздел для debug:", reply_markup=sections_keyboard("debug"))
        return

    if text == "/mysections":
        user_id = message.chat.id
        sections = USER_SECTIONS.get(user_id, [])
        if not sections:
            await message.answer("За тобой пока не закреплены авто-разделы.")
            return
        names = [SECTIONS[s]["name"] for s in sections if s in SECTIONS]
        await message.answer("Твои авто-разделы:\n" + "\n".join(f"- {n}" for n in names))
        return

    if text == "/id":
        await message.answer(str(message.chat.id))
        return


@dp.callback_query()
async def callbacks(call: CallbackQuery):
    try:
        action, section_key = call.data.split(":", 1)
    except Exception:
        await call.answer("Ошибка кнопки")
        return

    if section_key not in SECTIONS:
        await call.answer("Раздел не найден")
        return

    if action == "list":
        await call.message.answer(f"Проверяю раздел: {SECTIONS[section_key]['name']}...")
        complaints, _ = await collect_complaints_async(section_key)
        await call.message.answer(build_complaints_message(section_key, complaints), disable_web_page_preview=True)
        await call.answer()
        return

    if action == "debug":
        await call.message.answer(f"Debug раздела: {SECTIONS[section_key]['name']}...")
        complaints, topics = await collect_complaints_async(section_key)

        if not topics:
            await call.message.answer("Debug: тем не вижу. Возможно, форум долго грузится или блокирует Render.")
            await call.answer()
            return

        msg = f"Debug: вижу тем: {len(topics)}, в работе: {len(complaints)}\n"
        for t in topics[:10]:
            msg += f"\n- {t['title']}\n{t['url']}\n"

        await call.message.answer(msg, disable_web_page_preview=True)
        await call.answer()
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
