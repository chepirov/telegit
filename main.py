import os
import re
import html
import json
import hmac
import hashlib
import base64
import secrets
import sqlite3
import logging
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
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

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
GITHUB_API = "https://api.github.com"

REPO_PATTERN = re.compile(
    r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)/?$"
)

KNOWN_EVENTS = {
    "push": "Push (коммиты)",
    "pull_request": "Pull Request",
    "issues": "Issue (задачи)",
}
DEFAULT_EVENTS = ",".join(KNOWN_EVENTS)

TELEGRAM_LIMIT = 4096

def parse_repo(text: str) -> str | None:
    """Извлекает 'owner/repo' из ссылки или строки."""
    match = REPO_PATTERN.search(text.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2).removesuffix('.git')}"

def esc(value) -> str:
    """Экранирует <, > и & для вставки в текст с parse_mode=HTML."""
    return html.escape(str(value), quote=False)

def esc_attr(value) -> str:
    """Экранирование для значения атрибута (href), включая кавычки."""
    return html.escape(str(value), quote=True)

def first_line(text: str | None, limit: int) -> str:
    """Первая строка текста (безопасно для пустых сообщений), не длиннее limit."""
    lines = (text or "").splitlines()
    return lines[0][:limit] if lines else ""

def fit_escaped(text: str, limit: int) -> str:
    """Экранирует текст так, чтобы результат не превышал limit символов.

    Обрезается исходный текст (до экранирования), поэтому сущности вида
    &amp; никогда не разрезаются пополам.
    """
    text = text[:limit]
    result = esc(text)
    while len(result) > limit and text:
        text = text[:int(len(text) * 0.8)]
        result = esc(text)
    return result

def to_plain(text: str) -> str:
    """HTML-разметка -> обычный текст (запасной вариант отправки)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

def update_subscription(chat_id: int, repo: str, events: str) -> bool:
    """Меняет фильтр событий существующей подписки.

    Возвращает False, если такой подписки нет.
    """
    conn = get_db()
    cursor = conn.execute(
        "UPDATE subscriptions SET events_filter = ? "
        "WHERE chat_id = ? AND repo = ?",
        (events, chat_id, repo),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated

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

def has_subscribers(repo: str) -> bool:
    """Есть ли у репозитория хотя бы одна подписка (на любые события)."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM subscriptions WHERE repo = ? LIMIT 1", (repo,)
    ).fetchone()
    conn.close()
    return row is not None

def get_all_repos() -> list[str]:
    """Все репозитории, на которые есть хотя бы одна подписка."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT repo FROM subscriptions ORDER BY repo").fetchall()
    conn.close()
    return [r["repo"] for r in rows]

def get_setting(key: str) -> str | None:
    """Читает значение из служебной таблицы settings."""
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None

def set_setting(key: str, value: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()

def get_webhook_secret() -> str:
    """Секрет для подписи webhook.

    Если WEBHOOK_SECRET задан в .env - используется он. Иначе секрет один раз
    генерируется и хранится в базе, так что править .env не требуется.
    """
    if WEBHOOK_SECRET:
        return WEBHOOK_SECRET
    value = get_setting("webhook_secret")
    if not value:
        value = secrets.token_hex(32)
        set_setting("webhook_secret", value)
        logger.info("Секрет webhook сгенерирован и сохранён в базе")
    return value

def get_public_url() -> str:
    """Публичный адрес сервера: PUBLIC_URL из .env (если есть) или из /seturl."""
    return (PUBLIC_URL or get_setting("public_url") or "").rstrip("/")

def normalize_public_url(raw: str) -> str | None:
    """Приводит введённый адрес к виду https://host[:port] или None."""
    url = raw.strip().rstrip("/")
    url = url.removesuffix("/webhook").rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if (parsed.hostname in ("localhost", "0.0.0.0")
            or parsed.hostname.startswith("127.")):
        return None          # GitHub не достучится до локального адреса
    return url

async def github_request(method: str, path: str, **kwargs) -> httpx.Response:
    """Низкоуровневый запрос к GitHub API с авторизацией по токену."""
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.request(method, f"{GITHUB_API}{path}",
                                    headers=GITHUB_HEADERS, **kwargs)

async def github_get(path: str, params: dict | None = None) -> dict | list | None:
    """GET-запрос к GitHub API. Возвращает JSON или None при ошибке."""
    try:
        resp = await github_request("GET", path, params=params)
    except httpx.HTTPError as e:
        logger.warning("GitHub API %s недоступен: %s", path, e)
        return None
    if resp.status_code == 200:
        return resp.json()
    logger.warning("GitHub API %s -> %s", path, resp.status_code)
    return None

def webhook_target_url() -> str | None:
    """Адрес, на который GitHub будет присылать события."""
    base = get_public_url()
    return f"{base}/webhook" if base else None

async def ensure_github_webhook(repo: str) -> tuple[bool, str]:
    """Гарантирует, что в репозитории создан webhook на наш сервер.

    Возвращает (успех, пояснение для пользователя в HTML).
    Нужен токен с правом управления webhook и доступ admin к репозиторию.
    """
    url = webhook_target_url()
    if not url:
        return False, ("Публичный адрес сервера не задан, поэтому webhook "
                       "не создан. Администратор бота может задать его "
                       "командой /seturl https://ваш-адрес (см. README).")
    try:
        resp = await github_request("GET", f"/repos/{repo}/hooks",
                                    params={"per_page": 100})
        if resp.status_code == 200:
            for hook in resp.json():
                if hook.get("config", {}).get("url") == url:
                    return True, "Webhook в репозитории уже настроен."
        elif resp.status_code in (403, 404):
            return False, ("Не удалось настроить webhook: репозиторий не "
                           "найден или у токена нет прав управлять webhook "
                           "(нужен доступ admin). Добавьте webhook вручную "
                           "(см. README).")
        else:
            return False, (f"GitHub вернул {resp.status_code} при проверке "
                           f"webhook. Добавьте его вручную (см. README).")

        body = {
            "name": "web",
            "active": True,
            "events": list(KNOWN_EVENTS),
            "config": {
                "url": url,
                "content_type": "json",
                "secret": get_webhook_secret(),
                "insecure_ssl": "0",
            },
        }
        resp = await github_request("POST", f"/repos/{repo}/hooks", json=body)
        if resp.status_code == 201:
            return True, "Webhook в репозитории создан автоматически."
        if resp.status_code == 422 and "already exists" in resp.text.lower():
            return True, "Webhook в репозитории уже настроен."
        logger.warning("Создание webhook %s -> %s %s",
                       repo, resp.status_code, resp.text[:200])
        return False, (f"GitHub отказал в создании webhook "
                       f"(код {resp.status_code}). Добавьте его вручную "
                       f"(см. README).")
    except httpx.HTTPError as e:
        logger.warning("GitHub недоступен при создании webhook: %s", e)
        return False, "GitHub API недоступен — webhook не создан."

async def release_github_webhook(repo: str) -> str | None:
    """Удаляет наш webhook из репозитория, если подписчиков не осталось."""
    url = webhook_target_url()
    if not url or has_subscribers(repo):
        return None
    try:
        resp = await github_request("GET", f"/repos/{repo}/hooks",
                                    params={"per_page": 100})
        if resp.status_code != 200:
            return None
        for hook in resp.json():
            if hook.get("config", {}).get("url") == url:
                resp = await github_request(
                    "DELETE", f"/repos/{repo}/hooks/{hook['id']}")
                if resp.status_code == 204:
                    return "Webhook в репозитории удалён."
        return None
    except httpx.HTTPError as e:
        logger.warning("Не удалось удалить webhook %s: %s", repo, e)
        return None

async def fetch_summary(repo: str) -> str:
    """Сводка по репозиторию: статистика и последние коммиты."""
    info = await github_get(f"/repos/{repo}")
    if not info:
        return f"Репозиторий <b>{esc(repo)}</b> не найден или нет доступа."

    commits = await github_get(f"/repos/{repo}/commits",
                               params={"per_page": 5}) or []
    prs = await github_get(f"/repos/{repo}/pulls",
                           params={"state": "open", "per_page": 5}) or []
    issues = await github_get(f"/repos/{repo}/issues",
                              params={"state": "open", "per_page": 5}) or []
    issues = [i for i in issues if "pull_request" not in i]

    lines = [
        f"<b>Сводка: {esc(repo)}</b>",
        f"Звёзды: {info['stargazers_count']}   "
        f"Форки: {info['forks_count']}   "
        f"Наблюдатели: {info['watchers_count']}",
        f"Описание: {esc(info.get('description') or '—')}",
        f"Ветка по умолчанию: {esc(info['default_branch'])}",
        "",
        f"<b>Открытые PR ({len(prs)}):</b>",
    ]
    lines += [f"  • #{p['number']} {esc(p['title'])} "
              f"({esc(p['user']['login'])})"
              for p in prs] or ["  — нет —"]
    lines.append(f"\n<b>Открытые issue ({len(issues)}):</b>")
    lines += [f"  • #{i['number']} {esc(i['title'])}"
              for i in issues] or ["  — нет —"]
    lines.append("\n<b>Последние коммиты:</b>")
    lines += [
        f"  • <code>{esc(c['sha'][:7])}</code> "
        f"{esc(first_line(c['commit']['message'], 60))} "
        f"({esc((c['commit'].get('author') or {}).get('name', '?'))})"
        for c in commits
    ] or ["  — нет —"]
    return "\n".join(lines)[:TELEGRAM_LIMIT]

README_LIMIT = 3500

async def fetch_readme(repo: str) -> str:
    """Получает и декодирует README репозитория."""
    data = await github_get(f"/repos/{repo}/readme")
    if not data:
        return f"README не найден в репозитории <b>{esc(repo)}</b>."
    content = base64.b64decode(data["content"]).decode("utf-8",
                                                       errors="replace")
    body = fit_escaped(content, README_LIMIT)
    if len(content) > README_LIMIT:
        body += "\n\n…(обрезано, полный текст на GitHub)"
    return (f"<b>README: {esc(repo)}</b>\n"
            f"<a href=\"{esc_attr(data['html_url'])}\">Открыть на GitHub</a>"
            f"\n\n{body}")

DEPENDENCY_FILES = [
    "requirements.txt", "pyproject.toml", "Pipfile",
    "package.json", "go.mod", "Cargo.toml", "pom.xml",
]
DEPS_LIMIT = 3000

async def fetch_dependencies(repo: str) -> list[str]:
    """Ищет файлы зависимостей в репозитории.

    Возвращает список сообщений (по одному на файл), чтобы каждое
    укладывалось в лимит Telegram в 4096 символов.
    """
    found = []
    try:
        for filename in DEPENDENCY_FILES:
            resp = await github_request(
                "GET", f"/repos/{repo}/contents/{filename}")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "content" in data:
                    content = base64.b64decode(data["content"]).decode(
                        "utf-8", errors="replace")
                    found.append((filename, content))
    except httpx.HTTPError as e:
        logger.warning("GitHub недоступен при поиске зависимостей: %s", e)
        return ["GitHub API недоступен, попробуйте позже."]

    if not found:
        return [f"В репозитории <b>{esc(repo)}</b> не найдено файлов "
                f"зависимостей (искали: {esc(', '.join(DEPENDENCY_FILES))})."]

    messages = []
    for filename, content in found:
        body = fit_escaped(content, DEPS_LIMIT)
        if len(content) > DEPS_LIMIT:
            body += "\n…(обрезано)"
        messages.append(f"<b>Зависимости {esc(repo)} — "
                        f"📄 {esc(filename)}:</b>\n<pre>{body}</pre>")
    return messages

async def safe_send(chat_id: int, text: str) -> bool:
    """Отправляет сообщение; при ошибке разметки повторяет обычным текстом."""
    for _ in range(2):
        try:
            try:
                await bot.send_message(chat_id, text,
                                       disable_web_page_preview=True)
            except TelegramBadRequest as e:
                logger.warning("Ошибка разметки для чата %s: %s. "
                               "Повтор обычным текстом.", chat_id, e)
                await bot.send_message(chat_id, to_plain(text),
                                       parse_mode=None,
                                       disable_web_page_preview=True)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.error("Не удалось отправить в чат %s: %s", chat_id, e)
            return False
    return False

async def safe_answer(message: types.Message, text: str) -> None:
    """Ответ на команду с тем же запасным вариантом без разметки."""
    try:
        await message.answer(text, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        logger.warning("Ошибка разметки в ответе: %s", e)
        await message.answer(to_plain(text), parse_mode=None,
                             disable_web_page_preview=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_chat(message.chat.id, message.from_user.username
                  if message.from_user else None)
    await message.answer(
        "Привет! Я бот-ассистент для мониторинга GitHub.\n\n"
        "<b>Подписки:</b>\n"
        "/subscribe owner/repo [события] — подписаться или изменить "
        "фильтр событий\n"
        "/unsubscribe owner/repo — отписаться\n"
        "/list — мои подписки\n\n"
        "<b>Информация:</b>\n"
        "/summary owner/repo — сводка по репозиторию\n"
        "/readme owner/repo — показать README\n"
        "/deps owner/repo — найти зависимости\n\n"
        "Репозиторий можно указывать как <code>owner/repo</code> "
        "или полной ссылкой.\n"
        "События: <code>push</code>, <code>pull_request</code>, "
        "<code>issues</code> (через запятую, без пробелов).\n\n"
        "<b>Администратору:</b>\n"
        "/seturl https://адрес — публичный адрес сервера, чтобы бот сам "
        "создавал webhook в репозиториях"
    )

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    register_chat(message.chat.id, message.from_user.username
                  if message.from_user else None)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await safe_answer(message,
                          "Использование: /subscribe owner/repo "
                          "[события через запятую]\n"
                          f"Доступные события: {', '.join(KNOWN_EVENTS)}")
        return
    parts = args[1].split()
    repo = parse_repo(parts[0])
    if not repo:
        await safe_answer(message, "Не удалось распознать репозиторий. "
                                   "Укажите в формате owner/repo.")
        return

    events = DEFAULT_EVENTS
    events_given = len(parts) > 1
    if events_given:
        requested = list(dict.fromkeys(      # убираем дубли, сохраняя порядок
            e.strip() for e in parts[1].split(",") if e.strip()))
        invalid = [e for e in requested if e not in KNOWN_EVENTS]
        if invalid or not requested:
            await safe_answer(
                message,
                f"Неизвестные события: {esc(', '.join(invalid) or '—')}\n"
                f"Доступные: {', '.join(KNOWN_EVENTS)}")
            return
        events = ",".join(requested)

    events_text = ", ".join(KNOWN_EVENTS[e] for e in events.split(","))
    if add_subscription(message.chat.id, repo, events):
        head = (f"Подписка оформлена!\nРепозиторий: <b>{esc(repo)}</b>\n"
                f"События: {events_text}")
    elif events_given:
        update_subscription(message.chat.id, repo, events)
        head = (f"Фильтр событий обновлён.\nРепозиторий: <b>{esc(repo)}</b>\n"
                f"События: {events_text}")
    else:
        head = (f"Вы уже подписаны на <b>{esc(repo)}</b>.\n"
                f"Чтобы изменить фильтр: /subscribe {esc(repo)} "
                f"push,issues")
    _, note = await ensure_github_webhook(repo)
    await safe_answer(message, f"{head}\n\n{note}")

@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    repo = parse_repo(args[1]) if len(args) > 1 else None
    if not repo:
        await safe_answer(message, "Использование: /unsubscribe owner/repo")
        return
    if remove_subscription(message.chat.id, repo):
        text = f"Подписка на <b>{esc(repo)}</b> удалена."
        note = await release_github_webhook(repo)
        if note:
            text += f"\n{note}"
        await safe_answer(message, text)
    else:
        await safe_answer(message,
                          f"Вы не были подписаны на <b>{esc(repo)}</b>.")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    subs = get_subscriptions(message.chat.id)
    if not subs:
        await safe_answer(message, "У вас пока нет подписок. "
                                   "Добавьте: /subscribe owner/repo")
        return
    lines = ["<b>Ваши подписки:</b>"]
    for s in subs:
        events = ", ".join(KNOWN_EVENTS.get(e, e)
                           for e in s["events_filter"].split(","))
        lines.append(f"• <b>{esc(s['repo'])}</b> — {esc(events)}")
    await safe_answer(message, "\n".join(lines))

async def _repo_arg_or_reply(message: types.Message, usage: str) -> str | None:
    """Возвращает repo из аргумента, либо из единственной подписки."""
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        repo = parse_repo(args[1])
        if repo:
            return repo
        await safe_answer(message, "Не удалось распознать репозиторий.")
        return None
    subs = get_subscriptions(message.chat.id)
    if len(subs) == 1:
        return subs[0]["repo"]
    await safe_answer(message, f"Использование: {usage}")
    return None

@dp.message(Command("summary"))
async def cmd_summary(message: types.Message):
    repo = await _repo_arg_or_reply(message, "/summary owner/repo")
    if repo:
        await safe_answer(message, "Формирую сводку…")
        await safe_answer(message, await fetch_summary(repo))

@dp.message(Command("readme"))
async def cmd_readme(message: types.Message):
    repo = await _repo_arg_or_reply(message, "/readme owner/repo")
    if repo:
        await safe_answer(message, "Запрашиваю README…")
        await safe_answer(message, await fetch_readme(repo))

@dp.message(Command("deps"))
async def cmd_deps(message: types.Message):
    repo = await _repo_arg_or_reply(message, "/deps owner/repo")
    if repo:
        await safe_answer(message, "Ищу зависимости…")
        for part in await fetch_dependencies(repo):
            await safe_answer(message, part)

@dp.message(Command("seturl"))
async def cmd_seturl(message: types.Message):
    """Задаёт публичный адрес сервера (вместо переменной PUBLIC_URL в .env).

    Доступна администратору: им становится тот, кто первым успешно
    задал адрес. Иначе любой мог бы направить webhook на свой сервер.
    """
    user_id = str(message.from_user.id) if message.from_user else ""
    admin = get_setting("admin_id")
    if not user_id or (admin is not None and admin != user_id):
        await safe_answer(message, "Команда доступна только администратору бота.")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        current = get_public_url()
        shown = f"<code>{esc(current)}</code>" if current else "не задан"
        await safe_answer(message,
                          "Использование: /seturl https://ваш-адрес\n"
                          "Например, адрес из ngrok или cloudflared.\n"
                          f"Сейчас: {shown}")
        return

    url = normalize_public_url(args[1])
    if not url:
        await safe_answer(message,
                          "Не похоже на публичный адрес. Нужен http(s)-адрес, "
                          "доступный из интернета (не localhost).")
        return

    set_setting("public_url", url)
    if admin is None:
        set_setting("admin_id", user_id)

    lines = [f"Публичный адрес сохранён:\n<code>{esc(webhook_target_url())}</code>"]
    if PUBLIC_URL:
        lines.append("В .env задан PUBLIC_URL — он имеет приоритет над /seturl.")
    repos = get_all_repos()
    if repos:
        failed = []
        for repo in repos:
            ok, _ = await ensure_github_webhook(repo)
            if not ok:
                failed.append(repo)
        lines.append(f"Webhook настроен в репозиториях: "
                     f"{len(repos) - len(failed)} из {len(repos)}.")
        if failed:
            lines.append("Не удалось: " + esc(", ".join(failed))
                         + " — проверьте права токена (нужен admin).")
    else:
        lines.append("Теперь /subscribe будет сам создавать webhook "
                     "в репозитории.")
    await safe_answer(message, "\n".join(lines))

def verify_signature(body: bytes, signature: str | None) -> bool:
    """Проверка подлинности webhook по секрету (HMAC-SHA256).

    Секрет есть всегда (из .env или сгенерированный и сохранённый в базе),
    поэтому запрос без верной подписи отклоняется в любом случае.
    """
    secret = get_webhook_secret()
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

def format_push(payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "?")
    pusher = payload.get("pusher", {}).get("name", "?")
    ref = payload.get("ref", "")
    branch = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    commits = payload.get("commits", [])
    lines = [
        f"<b>Push в {esc(repo)}</b>",
        f"Ветка: <code>{esc(branch)}</code>   👤 Автор: {esc(pusher)}",
        f"Коммитов: {len(commits)}",
    ]
    for c in commits[:5]:
        lines.append(
            f"  • <code>{esc(c.get('id', '')[:7])}</code> "
            f"{esc(first_line(c.get('message'), 70))}")
    if len(commits) > 5:
        lines.append(f"  …и ещё {len(commits) - 5}")
    return "\n".join(lines)

def format_pull_request(payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "?")
    pr = payload.get("pull_request", {})
    action = payload.get("action", "?")
    merged = " (слит 🎉)" if pr.get("merged") else ""
    return (
        f"<b>Pull Request {esc(action)}{merged} в {esc(repo)}</b>\n"
        f"#{esc(pr.get('number'))} {esc(pr.get('title'))}\n"
        f"Автор: {esc(pr.get('user', {}).get('login', '?'))}\n"
        f"{esc(pr.get('head', {}).get('ref', '?'))} → "
        f"{esc(pr.get('base', {}).get('ref', '?'))}\n"
        f"<a href=\"{esc_attr(pr.get('html_url', ''))}\">Открыть на GitHub</a>"
    )

def format_issue(payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "?")
    issue = payload.get("issue", {})
    action = payload.get("action", "?")
    return (
        f"<b>Issue {esc(action)} в {esc(repo)}</b>\n"
        f"#{esc(issue.get('number'))} {esc(issue.get('title'))}\n"
        f"Автор: {esc(issue.get('user', {}).get('login', '?'))}\n"
        f"<a href=\"{esc_attr(issue.get('html_url', ''))}\">"
        f"Открыть на GitHub</a>"
    )

EVENT_FORMATTERS = {
    "push": format_push,
    "pull_request": format_pull_request,
    "issues": format_issue,
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    get_webhook_secret()          # создаст секрет в базе, если его нет в .env
    if not get_public_url():
        logger.info("Публичный адрес не задан: администратор может задать "
                    "его командой /seturl в Telegram")
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

    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    if not isinstance(payload, dict):
        return Response(status_code=400)

    event_type = request.headers.get("X-GitHub-Event", "")
    repo = payload.get("repository", {}).get("full_name", "")

    logger.info("Webhook: событие=%s репозиторий=%s", event_type, repo)

    formatter = EVENT_FORMATTERS.get(event_type)
    if not formatter or not repo:
        return {"status": "ignored", "event": event_type}

    text = formatter(payload)
    sent = failed = 0
    for chat_id in get_subscribers(repo, event_type):
        if await safe_send(chat_id, text):
            sent += 1
        else:
            failed += 1

    return {"status": "success", "event": event_type,
            "notified": sent, "failed": failed}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)