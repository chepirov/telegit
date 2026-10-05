"""
Telegram-бот для мониторинга репозиториев GitHub.

Функциональность:
- авторизация по токенам (Telegram Bot API + GitHub Personal Access Token);
- управление подписками на репозитории (/subscribe, /unsubscribe, /list);
- приём событий через GitHub Webhook (push, pull_request, issues)
  с проверкой подписи HMAC-SHA256;
- база данных SQLite для хранения подписок, чатов и фильтров событий;
- команды /summary, /readme, /deps (поиск зависимостей в репозитории);
- отправка уведомлений в читаемом виде.
"""

import os
import re
import hmac
import hashlib
import base64
import sqlite3
import logging
import asyncio
from contextlib import asynccontextmanager

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ---------------------------------------------------------------------------
# 1. Конфигурация и авторизация по токенам
# ---------------------------------------------------------------------------

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "github_bot.db")
PORT = int(os.getenv("PORT", "8000"))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не найден! Проверьте файл .env")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN не найден! Проверьте файл .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("github-bot")

bot = Bot(token=TELEGRAM_TOKEN,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Заголовки для запросов к GitHub API (авторизация по токену)
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
GITHUB_API = "https://api.github.com"

# Репозиторий задаётся в формате owner/repo или полной ссылкой
REPO_PATTERN = re.compile(
    r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)/?$"
)

# Известные типы событий и их человекочитаемые названия
KNOWN_EVENTS = {
    "push": "Push (коммиты)",
    "pull_request": "Pull Request",
    "issues": "Issue (задачи)",
}


def parse_repo(text: str) -> str | None:
    """Извлекает 'owner/repo' из ссылки или строки."""
    match = REPO_PATTERN.search(text.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


# ---------------------------------------------------------------------------
# 2. База данных SQLite: подписки, чаты, фильтры
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id     INTEGER PRIMARY KEY,
            username    TEXT,
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER NOT NULL,
            repo          TEXT NOT NULL,
            events_filter TEXT NOT NULL DEFAULT 'push,pull_request,issues',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, repo),
            FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
        )
    """)
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована: %s", DATABASE_PATH)


def register_chat(chat_id: int, username: str | None) -> None:
    """Автоматически регистрирует чат при первом обращении."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO chats (chat_id, username) VALUES (?, ?)",
        (chat_id, username),
    )
    conn.commit()
    conn.close()


def add_subscription(chat_id: int, repo: str, events: str) -> bool:
    """Добавляет подписку. Возвращает False, если такая уже есть."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO subscriptions (chat_id, repo, events_filter) "
            "VALUES (?, ?, ?)",
            (chat_id, repo, events),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_subscription(chat_id: int, repo: str) -> bool:
    """Удаляет подписку. Возвращает False, если её не было."""
    conn = get_db()
    cursor = conn.execute(
        "DELETE FROM subscriptions WHERE chat_id = ? AND repo = ?",
        (chat_id, repo),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def get_subscriptions(chat_id: int) -> list[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute(
        "SELECT repo, events_filter FROM subscriptions WHERE chat_id = ?",
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def get_subscribers(repo: str, event_type: str) -> list[int]:
    """Возвращает chat_id всех чатов, подписанных на repo и тип события."""
    conn = get_db()
    rows = conn.execute(
        "SELECT chat_id, events_filter FROM subscriptions WHERE repo = ?",
        (repo,),
    ).fetchall()
    conn.close()
    return [r["chat_id"] for r in rows
            if event_type in r["events_filter"].split(",")]


# ---------------------------------------------------------------------------
# 3. Модуль работы с GitHub API
# ---------------------------------------------------------------------------

async def github_get(path: str, params: dict | None = None) -> dict | list | None:
    """Выполняет GET-запрос к GitHub API с авторизацией по токену."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GITHUB_API}{path}",
                                headers=GITHUB_HEADERS, params=params)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("GitHub API %s -> %s", path, resp.status_code)
        return None


async def fetch_summary(repo: str) -> str:
    """Сводка по репозиторию: статистика и последние коммиты."""
    info = await github_get(f"/repos/{repo}")
    if not info:
        return f"❌ Репозиторий <b>{repo}</b> не найден или нет доступа."

    commits = await github_get(f"/repos/{repo}/commits",
                               params={"per_page": 5}) or []
    prs = await github_get(f"/repos/{repo}/pulls",
                           params={"state": "open", "per_page": 5}) or []
    issues = await github_get(f"/repos/{repo}/issues",
                              params={"state": "open", "per_page": 5}) or []
    # issues endpoint включает PR, отфильтруем их
    issues = [i for i in issues if "pull_request" not in i]

    lines = [
        f"📊 <b>Сводка: {repo}</b>",
        f"⭐ Звёзды: {info['stargazers_count']}   "
        f"🍴 Форки: {info['forks_count']}   "
        f"👀 Наблюдатели: {info['watchers_count']}",
        f"📝 Описание: {info.get('description') or '—'}",
        f"🌿 Ветка по умолчанию: {info['default_branch']}",
        "",
        f"🔀 <b>Открытые PR ({len(prs)}):</b>",
    ]
    lines += [f"  • #{p['number']} {p['title']} ({p['user']['login']})"
              for p in prs] or ["  — нет —"]
    lines.append(f"\n❗ <b>Открытые issue ({len(issues)}):</b>")
    lines += [f"  • #{i['number']} {i['title']}" for i in issues] or ["  — нет —"]
    lines.append("\n🕒 <b>Последние коммиты:</b>")
    lines += [
        f"  • <code>{c['sha'][:7]}</code> "
        f"{c['commit']['message'].splitlines()[0][:60]} "
        f"({c['commit']['author']['name']})"
        for c in commits
    ] or ["  — нет —"]
    return "\n".join(lines)


async def fetch_readme(repo: str) -> str:
    """Получает и декодирует README репозитория."""
    data = await github_get(f"/repos/{repo}/readme")
    if not data:
        return f"❌ README не найден в репозитории <b>{repo}</b>."
    content = base64.b64decode(data["content"]).decode("utf-8",
                                                       errors="replace")
    # Telegram ограничивает сообщение 4096 символами
    if len(content) > 3500:
        content = content[:3500] + "\n\n…(обрезано, полный текст на GitHub)"
    return (f"📖 <b>README: {repo}</b>\n"
            f"<a href=\"{data['html_url']}\">Открыть на GitHub</a>\n\n"
            f"{content}")


# Файлы, в которых обычно описаны зависимости
DEPENDENCY_FILES = [
    "requirements.txt", "pyproject.toml", "Pipfile",
    "package.json", "go.mod", "Cargo.toml", "pom.xml",
]


async def fetch_dependencies(repo: str) -> str:
    """Ищет файлы зависимостей в репозитории и выводит их содержимое."""
    found = []
    async with httpx.AsyncClient(timeout=15) as client:
        for filename in DEPENDENCY_FILES:
            resp = await client.get(f"{GITHUB_API}/repos/{repo}/contents/{filename}",
                                    headers=GITHUB_HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                content = base64.b64decode(data["content"]).decode(
                    "utf-8", errors="replace")
                found.append((filename, content))

    if not found:
        return (f"🔍 В репозитории <b>{repo}</b> не найдено файлов "
                f"зависимостей (искали: {', '.join(DEPENDENCY_FILES)}).")

    parts = [f"📦 <b>Зависимости: {repo}</b>"]
    for filename, content in found:
        if len(content) > 1500:
            content = content[:1500] + "\n…(обрезано)"
        parts.append(f"\n<b>📄 {filename}:</b>\n<pre>{content}</pre>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 4. Команды Telegram-бота
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_chat(message.chat.id, message.from_user.username
                  if message.from_user else None)
    await message.answer(
        "👋 Привет! Я бот-ассистент для мониторинга GitHub.\n\n"
        "<b>Подписки:</b>\n"
        "/subscribe owner/repo — подписаться на репозиторий\n"
        "/unsubscribe owner/repo — отписаться\n"
        "/list — мои подписки\n\n"
        "<b>Информация:</b>\n"
        "/summary owner/repo — сводка по репозиторию\n"
        "/readme owner/repo — показать README\n"
        "/deps owner/repo — найти зависимости\n\n"
        "Репозиторий можно указывать как <code>owner/repo</code> "
        "или полной ссылкой."
    )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    register_chat(message.chat.id, message.from_user.username
                  if message.from_user else None)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /subscribe owner/repo "
                             "[события через запятую]\n"
                             f"Доступные события: {', '.join(KNOWN_EVENTS)}")
        return
    parts = args[1].split()
    repo = parse_repo(parts[0])
    if not repo:
        await message.answer("❌ Не удалось распознать репозиторий. "
                             "Укажите в формате owner/repo.")
        return

    # Необязательный фильтр событий: /subscribe owner/repo push,issues
    events = "push,pull_request,issues"
    if len(parts) > 1:
        requested = [e.strip() for e in parts[1].split(",") if e.strip()]
        invalid = [e for e in requested if e not in KNOWN_EVENTS]
        if invalid:
            await message.answer(
                f"❌ Неизвестные события: {', '.join(invalid)}\n"
                f"Доступные: {', '.join(KNOWN_EVENTS)}")
            return
        events = ",".join(requested)

    if add_subscription(message.chat.id, repo, events):
        await message.answer(
            f"✅ Подписка оформлена!\nРепозиторий: <b>{repo}</b>\n"
            f"События: {', '.join(KNOWN_EVENTS[e] for e in events.split(','))}")
    else:
        await message.answer(f"ℹ️ Вы уже подписаны на <b>{repo}</b>.")


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    repo = parse_repo(args[1]) if len(args) > 1 else None
    if not repo:
        await message.answer("Использование: /unsubscribe owner/repo")
        return
    if remove_subscription(message.chat.id, repo):
        await message.answer(f"✅ Подписка на <b>{repo}</b> удалена.")
    else:
        await message.answer(f"ℹ️ Вы не были подписаны на <b>{repo}</b>.")


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    subs = get_subscriptions(message.chat.id)
    if not subs:
        await message.answer("У вас пока нет подписок. "
                             "Добавьте: /subscribe owner/repo")
        return
    lines = ["📋 <b>Ваши подписки:</b>"]
    for s in subs:
        events = ", ".join(KNOWN_EVENTS.get(e, e)
                           for e in s["events_filter"].split(","))
        lines.append(f"• <b>{s['repo']}</b> — {events}")
    await message.answer("\n".join(lines))


async def _repo_arg_or_reply(message: types.Message, usage: str) -> str | None:
    """Возвращает repo из аргумента, либо из единственной подписки."""
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        repo = parse_repo(args[1])
        if repo:
            return repo
        await message.answer("❌ Не удалось распознать репозиторий.")
        return None
    subs = get_subscriptions(message.chat.id)
    if len(subs) == 1:
        return subs[0]["repo"]
    await message.answer(f"Использование: {usage}")
    return None


@dp.message(Command("summary"))
async def cmd_summary(message: types.Message):
    repo = await _repo_arg_or_reply(message, "/summary owner/repo")
    if repo:
        await message.answer("⏳ Формирую сводку…")
        await message.answer(await fetch_summary(repo),
                             disable_web_page_preview=True)


@dp.message(Command("readme"))
async def cmd_readme(message: types.Message):
    repo = await _repo_arg_or_reply(message, "/readme owner/repo")
    if repo:
        await message.answer("⏳ Запрашиваю README…")
        await message.answer(await fetch_readme(repo),
                             disable_web_page_preview=True)


@dp.message(Command("deps"))
async def cmd_deps(message: types.Message):
    repo = await _repo_arg_or_reply(message, "/deps owner/repo")
    if repo:
        await message.answer("⏳ Ищу зависимости…")
        await message.answer(await fetch_dependencies(repo),
                             disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# 5. Webhook: приём событий GitHub и читаемые уведомления
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str | None) -> bool:
    """Проверка подлинности webhook по секрету (HMAC-SHA256)."""
    if not WEBHOOK_SECRET:
        return True  # секрет не задан — проверка отключена (dev-режим)
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def format_push(payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "?")
    pusher = payload.get("pusher", {}).get("name", "?")
    branch = payload.get("ref", "").replace("refs/heads/", "")
    commits = payload.get("commits", [])
    lines = [
        f"🚀 <b>Push в {repo}</b>",
        f"🌿 Ветка: <code>{branch}</code>   👤 Автор: {pusher}",
        f"📝 Коммитов: {len(commits)}",
    ]
    for c in commits[:5]:
        lines.append(
            f"  • <code>{c['id'][:7]}</code> "
            f"{c['message'].splitlines()[0][:70]}")
    if len(commits) > 5:
        lines.append(f"  …и ещё {len(commits) - 5}")
    return "\n".join(lines)


def format_pull_request(payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "?")
    pr = payload.get("pull_request", {})
    action = payload.get("action", "?")
    merged = " (слит 🎉)" if pr.get("merged") else ""
    return (
        f"🔀 <b>Pull Request {action}{merged} в {repo}</b>\n"
        f"#{pr.get('number')} {pr.get('title')}\n"
        f"👤 Автор: {pr.get('user', {}).get('login', '?')}\n"
        f"🌿 {pr.get('head', {}).get('ref', '?')} → "
        f"{pr.get('base', {}).get('ref', '?')}\n"
        f"<a href=\"{pr.get('html_url', '')}\">Открыть на GitHub</a>"
    )


def format_issue(payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "?")
    issue = payload.get("issue", {})
    action = payload.get("action", "?")
    return (
        f"❗ <b>Issue {action} в {repo}</b>\n"
        f"#{issue.get('number')} {issue.get('title')}\n"
        f"👤 Автор: {issue.get('user', {}).get('login', '?')}\n"
        f"<a href=\"{issue.get('html_url', '')}\">Открыть на GitHub</a>"
    )


EVENT_FORMATTERS = {
    "push": format_push,
    "pull_request": format_pull_request,
    "issues": format_issue,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    
    # ДОБАВЬТЕ ЭТУ СТРОКУ, чтобы сбросить конфликт с серверами Telegram:
    await bot.delete_webhook(drop_pending_updates=True)
    
    bot_task = asyncio.create_task(dp.start_polling(bot))
    logger.info("Бот и webhook-сервер запущены")
    yield
    bot_task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def github_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(body, signature):
        logger.warning("Webhook отклонён: неверная подпись")
        return Response(status_code=401)

    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event", "")
    repo = payload.get("repository", {}).get("full_name", "")

    logger.info("Webhook: событие=%s репозиторий=%s", event_type, repo)

    formatter = EVENT_FORMATTERS.get(event_type)
    if not formatter or not repo:
        return {"status": "ignored", "event": event_type}

    text = formatter(payload)
    sent = 0
    for chat_id in get_subscribers(repo, event_type):
        try:
            await bot.send_message(chat_id, text,
                                   disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            logger.error("Не удалось отправить в чат %s: %s", chat_id, e)

    return {"status": "success", "event": event_type, "notified": sent}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
