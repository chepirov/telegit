import os
import sys
import hmac
import hashlib
import json
import base64
import asyncio
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("TELEGRAM_TOKEN", "123456789:test_token_for_unit_tests")
os.environ.setdefault("GITHUB_TOKEN", "test_token")

os.environ["DATABASE_PATH"] = "test_database.db"
os.environ["WEBHOOK_SECRET"] = "test_secret"

import main  # noqa: E402

passed = failed = 0


def check(name: str, condition: bool):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}")


def run(coro):
    return asyncio.run(coro)

class FakeMessage:
    """Имитация aiogram.types.Message: запоминает все ответы бота."""

    def __init__(self, text: str, chat_id: int = 1001, user_id: int = 1001):
        self.text = text
        self.chat = type("Chat", (), {"id": chat_id})()
        self.from_user = type("User", (), {"username": "tester",
                                          "id": user_id})()
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    @property
    def last(self) -> str:
        return self.answers[-1] if self.answers else ""


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        return self._data


class FakeBadRequest(main.TelegramBadRequest):
    """Ошибка Telegram «can't parse entities» без зависимости от
    сигнатуры конструктора aiogram."""

    def __init__(self):
        Exception.__init__(self, "can't parse entities")
        self.message = "can't parse entities"


class FakeBot:
    """Имитация Bot: запоминает отправки, умеет «ломаться» на HTML."""

    def __init__(self, fail_on_html=False, fail_always=False):
        self.calls: list[dict] = []
        self.fail_on_html = fail_on_html
        self.fail_always = fail_always

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        if self.fail_always:
            raise RuntimeError("chat not found")
        if self.fail_on_html and "parse_mode" not in kwargs:
            raise FakeBadRequest()


class FakeRequest:
    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    async def body(self):
        return self._body


def signed_request(payload, event: str, secret: str = "test_secret"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return FakeRequest(body, {"X-Hub-Signature-256": sig,
                              "X-GitHub-Event": event})
print("\n[1] Парсинг репозитория")
check("owner/repo", main.parse_repo("octocat/Hello-World") == "octocat/Hello-World")
check("полная ссылка", main.parse_repo("https://github.com/octocat/Hello-World") == "octocat/Hello-World")
check("ссылка со слэшем", main.parse_repo("https://github.com/octocat/Hello-World/") == "octocat/Hello-World")
check("суффикс .git убирается", main.parse_repo("https://github.com/octocat/Hello-World.git") == "octocat/Hello-World")
check("мусор отклоняется", main.parse_repo("просто текст") is None)

print("\n[2] База данных")
if os.path.exists("test_database.db"):
    os.remove("test_database.db")

main.init_db()
main.register_chat(1001, "test_user")
check("чат зарегистрирован", True)

check("подписка добавлена", main.add_subscription(1001, "octocat/Hello-World", "push,issues") is True)
check("дубликат отклонён", main.add_subscription(1001, "octocat/Hello-World", "push") is False)

subs = main.get_subscriptions(1001)
check("подписка читается", len(subs) == 1 and subs[0]["repo"] == "octocat/Hello-World")
check("фильтр сохранён", subs[0]["events_filter"] == "push,issues")

check("подписчик на push найден", main.get_subscribers("octocat/Hello-World", "push") == [1001])
check("подписчик на issues найден", main.get_subscribers("octocat/Hello-World", "issues") == [1001])
check("pull_request отфильтрован", main.get_subscribers("octocat/Hello-World", "pull_request") == [])
check("чужой репозиторий пуст", main.get_subscribers("other/repo", "push") == [])

check("фильтр обновляется", main.update_subscription(1001, "octocat/Hello-World", "pull_request") is True)
check("новый фильтр применён", main.get_subscribers("octocat/Hello-World", "pull_request") == [1001]
      and main.get_subscribers("octocat/Hello-World", "push") == [])
check("обновление несуществующей подписки = False", main.update_subscription(1001, "other/repo", "push") is False)
check("has_subscribers: есть", main.has_subscribers("octocat/Hello-World") is True)

check("отписка работает", main.remove_subscription(1001, "octocat/Hello-World") is True)
check("повторная отписка = False", main.remove_subscription(1001, "octocat/Hello-World") is False)
check("has_subscribers: нет", main.has_subscribers("octocat/Hello-World") is False)

print("\n[3] Подпись webhook (HMAC-SHA256)")
body = json.dumps({"ref": "refs/heads/main"}).encode()
sig = "sha256=" + hmac.new(b"test_secret", body, hashlib.sha256).hexdigest()
check("верная подпись принимается", main.verify_signature(body, sig) is True)
check("неверная подпись отклоняется", main.verify_signature(body, "sha256=bad") is False)
check("отсутствие подписи отклоняется", main.verify_signature(body, None) is False)

_saved_secret = main.WEBHOOK_SECRET
main.WEBHOOK_SECRET = ""
empty_sig = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
check("пустой секрет: запрос без подписи отклоняется", main.verify_signature(body, None) is False)
check("пустой секрет: подпись от пустого ключа отклоняется", main.verify_signature(body, empty_sig) is False)

# Секрета в .env нет -> бот сам создаёт его в базе; чужие запросы всё равно не проходят.
db_secret = main.get_webhook_secret()
check("без секрета в .env: секрет создан в базе", len(db_secret) >= 32)
check("секрет из базы стабилен между вызовами", main.get_webhook_secret() == db_secret)
db_sig = "sha256=" + hmac.new(db_secret.encode(), body, hashlib.sha256).hexdigest()
check("подпись секретом из базы принимается", main.verify_signature(body, db_sig) is True)
main.WEBHOOK_SECRET = _saved_secret
check("секрет из .env имеет приоритет", main.get_webhook_secret() == "test_secret")

print("\n[4] Форматирование уведомлений")
push_text = main.format_push({
    "ref": "refs/heads/main",
    "pusher": {"name": "ivan"},
    "repository": {"full_name": "octocat/Hello-World"},
    "commits": [{"id": "abc1234567", "message": "fix bug"}],
})
check("push содержит репозиторий", "octocat/Hello-World" in push_text)
check("push содержит автора", "ivan" in push_text)
check("push содержит sha коммита", "abc1234" in push_text)

pr_text = main.format_pull_request({
    "action": "opened",
    "repository": {"full_name": "octocat/Hello-World"},
    "pull_request": {"number": 42, "title": "Add feature",
                     "user": {"login": "ivan"},
                     "head": {"ref": "dev"}, "base": {"ref": "main"},
                     "html_url": "https://github.com/octocat/Hello-World/pull/42"},
})
check("PR содержит номер", "#42" in pr_text)
check("PR содержит ветки", "dev" in pr_text and "main" in pr_text)

issue_text = main.format_issue({
    "action": "opened",
    "repository": {"full_name": "octocat/Hello-World"},
    "issue": {"number": 7, "title": "Bug", "user": {"login": "anna"},
              "html_url": "https://github.com/octocat/Hello-World/issues/7"},
})
check("issue содержит заголовок", "Bug" in issue_text)

print("\n[5] Экранирование HTML (<, > и &)")
evil_push = main.format_push({
    "ref": "refs/heads/feat<x>",
    "pusher": {"name": "a&b"},
    "repository": {"full_name": "o/r"},
    "commits": [{"id": "abcdef012", "message": "fix <script> & more"},
                {"id": "1234567890", "message": ""}],
})
check("push: < и > экранированы", "fix &lt;script&gt; &amp; more" in evil_push)
check("push: сырой <script> отсутствует", "<script>" not in evil_push)
check("push: автор с & экранирован", "a&amp;b" in evil_push)
check("push: пустое сообщение коммита не падает", "1234567" in evil_push)

evil_pr = main.format_pull_request({
    "action": "opened", "repository": {"full_name": "o/r"},
    "pull_request": {"number": 1, "title": "Vector<int> & co", "user": {"login": "x"},
                     "head": {"ref": "a"}, "base": {"ref": "b"},
                     "html_url": 'https://x/"onmouseover="y'},
})
check("PR: заголовок экранирован", "Vector&lt;int&gt; &amp; co" in evil_pr)
check("PR: кавычки в href экранированы", '"onmouseover="' not in evil_pr)

evil_issue = main.format_issue({
    "action": "opened", "repository": {"full_name": "o/r"},
    "issue": {"number": 2, "title": "a < b > c", "user": {"login": "x"},
              "html_url": "https://x/2"},
})
check("issue: заголовок экранирован", "a &lt; b &gt; c" in evil_issue)

print("\n[6] Сводка, README, зависимости")


async def fake_github_get(path, params=None):
    if path.endswith("/readme"):
        text = "# Title\n\nif a < b && c > d: <div>" + "x" * 5000
        return {"content": base64.b64encode(text.encode()).decode(),
                "html_url": "https://github.com/o/r/blob/main/README.md"}
    if path == "/repos/o/r":
        return {"stargazers_count": 5, "forks_count": 1, "watchers_count": 5,
                "description": "A <cool> & fast lib", "default_branch": "main"}
    if path.endswith("/commits"):
        return [{"sha": "abcdef123456",
                 "commit": {"message": "fix <b>\nbody", "author": {"name": "Ann & Bob"}}}]
    if path.endswith("/pulls"):
        return [{"number": 3, "title": "PR <1>", "user": {"login": "u"}}]
    if path.endswith("/issues"):
        return [{"number": 4, "title": "Issue & <2>"},
                {"number": 3, "title": "this is a PR", "pull_request": {}}]
    return None


_orig_get = main.github_get
main.github_get = fake_github_get

summary = run(main.fetch_summary("o/r"))
check("summary: описание экранировано", "A &lt;cool&gt; &amp; fast lib" in summary)
check("summary: заголовок PR экранирован", "PR &lt;1&gt;" in summary)
check("summary: PR не попадают в issue", "this is a PR" not in summary)
check("summary: автор коммита экранирован", "Ann &amp; Bob" in summary)

readme = run(main.fetch_readme("o/r"))
check("readme: <, > и & экранированы", "a &lt; b &amp;&amp; c &gt; d: &lt;div&gt;" in readme)
check("readme: влезает в лимит Telegram", len(readme) <= 4096)
check("readme: помечен как обрезанный", "обрезано" in readme)

main.github_get = _orig_get


async def fake_github_request(method, path, **kwargs):
    if method == "GET" and path.endswith("/contents/requirements.txt"):
        text = "httpx>=0.27\nfoo<2 & bar\n" + "# pad\n" * 2000
        return FakeResponse(200, {"content": base64.b64encode(text.encode()).decode()})
    return FakeResponse(404, {})


_orig_request = main.github_request
main.github_request = fake_github_request

deps = run(main.fetch_dependencies("o/r"))
check("deps: найден один файл", len(deps) == 1 and "requirements.txt" in deps[0])
check("deps: < и & экранированы", "foo&lt;2 &amp; bar" in deps[0])
check("deps: сообщение влезает в лимит Telegram", all(len(m) <= 4096 for m in deps))

main.github_request = _orig_request

print("\n[7] Webhook в репозитории GitHub")
_saved_url = main.PUBLIC_URL

main.PUBLIC_URL = ""
ok, note = run(main.ensure_github_webhook("o/r"))
check("без адреса: webhook не создаётся, есть подсказка про /seturl", ok is False and "/seturl" in note)

check("normalize: слэш и /webhook в конце убираются",
      main.normalize_public_url("https://a.ngrok.app/webhook/") == "https://a.ngrok.app")
check("normalize: localhost отклоняется", main.normalize_public_url("http://localhost:8000") is None)
check("normalize: 127.0.0.1 отклоняется", main.normalize_public_url("http://127.0.0.1:8000") is None)
check("normalize: не-ссылка отклоняется", main.normalize_public_url("просто текст") is None)
check("normalize: ftp отклоняется", main.normalize_public_url("ftp://a.com") is None)

main.set_setting("public_url", "https://from-db.example")
check("адрес берётся из базы (без PUBLIC_URL в .env)",
      main.webhook_target_url() == "https://from-db.example/webhook")
main.set_setting("public_url", "")
check("пустой адрес в базе = не задан", main.webhook_target_url() is None)

main.PUBLIC_URL = "https://example.ngrok.app/"
check("адрес webhook собирается без двойного слэша",
      main.webhook_target_url() == "https://example.ngrok.app/webhook")

calls = []


def make_request_stub(list_response, post_response=None):
    async def stub(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return list_response
        if method == "POST":
            return post_response
        return FakeResponse(204, None)
    return stub


calls.clear()
main.github_request = make_request_stub(FakeResponse(200, []), FakeResponse(201, {"id": 1}))
ok, note = run(main.ensure_github_webhook("o/r"))
post = [c for c in calls if c[0] == "POST"]
check("webhook создаётся (POST /hooks)", ok is True and len(post) == 1 and post[0][1] == "/repos/o/r/hooks")
cfg = post[0][2]["json"]["config"] if post else {}
check("в webhook передан адрес, секрет и json", cfg.get("url") == "https://example.ngrok.app/webhook"
      and cfg.get("secret") == "test_secret" and cfg.get("content_type") == "json")
check("webhook слушает push/pull_request/issues",
      set(post[0][2]["json"]["events"]) == {"push", "pull_request", "issues"} if post else False)

calls.clear()
existing = [{"id": 9, "config": {"url": "https://example.ngrok.app/webhook"}}]
main.github_request = make_request_stub(FakeResponse(200, existing))
ok, note = run(main.ensure_github_webhook("o/r"))
check("существующий webhook не дублируется", ok is True and not [c for c in calls if c[0] == "POST"])

main.github_request = make_request_stub(FakeResponse(404, {}))
ok, note = run(main.ensure_github_webhook("o/r"))
check("нет прав/репозитория: понятное сообщение", ok is False and "вручную" in note)

calls.clear()
main.github_request = make_request_stub(FakeResponse(200, existing))
main.add_subscription(1001, "o/r", "push")
note = run(main.release_github_webhook("o/r"))
check("есть подписчики: webhook не удаляется", note is None and not [c for c in calls if c[0] == "DELETE"])
main.remove_subscription(1001, "o/r")
note = run(main.release_github_webhook("o/r"))
check("подписчиков нет: webhook удаляется", note is not None and [c for c in calls if c[0] == "DELETE"])

main.github_request = _orig_request
main.PUBLIC_URL = _saved_url

print("\n[8] Команды бота")


async def fake_ensure(repo):
    return True, "Webhook ок."


async def fake_release(repo):
    return None


main.ensure_github_webhook = fake_ensure
main.release_github_webhook = fake_release

m = FakeMessage("/seturl", user_id=7)
run(main.cmd_seturl(m))
check("/seturl без аргумента: подсказка", "Использование" in m.last)
check("пустой /seturl не делает пользователя админом", main.get_setting("admin_id") is None)

m = FakeMessage("/seturl http://localhost:8000", user_id=7)
run(main.cmd_seturl(m))
check("/seturl: localhost отклоняется", "Не похоже" in m.last and main.get_setting("public_url") in (None, ""))

m = FakeMessage("/seturl https://abc.ngrok.app", user_id=7)
run(main.cmd_seturl(m))
check("/seturl: адрес сохранён", main.get_setting("public_url") == "https://abc.ngrok.app")
check("/seturl: первый успешный вызов делает админом", main.get_setting("admin_id") == "7")
check("/seturl: ответ показывает адрес webhook", "https://abc.ngrok.app/webhook" in m.last)

m = FakeMessage("/seturl https://evil.example", user_id=8)
run(main.cmd_seturl(m))
check("/seturl: чужой пользователь получает отказ", "администратору" in m.last
      and main.get_setting("public_url") == "https://abc.ngrok.app")

main.add_subscription(1003, "x/y", "push")
m = FakeMessage("/seturl https://abc.ngrok.app", user_id=7)
run(main.cmd_seturl(m))
check("/seturl: для уже оформленных подписок webhook донастраивается",
      "1 из 1" in m.last)
main.remove_subscription(1003, "x/y")

m = FakeMessage("/subscribe")
run(main.cmd_subscribe(m))
check("/subscribe без аргумента: подсказка", "Использование" in m.last)

m = FakeMessage("/subscribe просто текст")
run(main.cmd_subscribe(m))
check("/subscribe с мусором: ошибка разбора", "Не удалось распознать" in m.last)

m = FakeMessage("/subscribe o/r foo")
run(main.cmd_subscribe(m))
check("/subscribe с неизвестным событием отклоняется", "Неизвестные события" in m.last
      and main.get_subscriptions(1001) == [])

m = FakeMessage("/subscribe https://github.com/o/r")
run(main.cmd_subscribe(m))
check("/subscribe создаёт подписку", "Подписка оформлена" in m.last)
check("/subscribe сообщает про webhook", "Webhook ок." in m.last)
check("фильтр по умолчанию - все события",
      main.get_subscriptions(1001)[0]["events_filter"] == "push,pull_request,issues")

m = FakeMessage("/subscribe o/r")
run(main.cmd_subscribe(m))
check("повторный /subscribe без фильтра: 'уже подписаны'", "уже подписаны" in m.last)

m = FakeMessage("/subscribe o/r push,issues,push")
run(main.cmd_subscribe(m))
check("повторный /subscribe с фильтром обновляет его", "обновлён" in m.last
      and main.get_subscriptions(1001)[0]["events_filter"] == "push,issues")

m = FakeMessage("/list")
run(main.cmd_list(m))
check("/list показывает репозиторий", "o/r" in m.last)

m = FakeMessage("/unsubscribe")
run(main.cmd_unsubscribe(m))
check("/unsubscribe без аргумента: подсказка", "Использование" in m.last)

m = FakeMessage("/unsubscribe o/r")
run(main.cmd_unsubscribe(m))
check("/unsubscribe удаляет подписку", "удалена" in m.last and main.get_subscriptions(1001) == [])

m = FakeMessage("/unsubscribe o/r")
run(main.cmd_unsubscribe(m))
check("повторный /unsubscribe: 'не были подписаны'", "не были подписаны" in m.last)

m = FakeMessage("/list")
run(main.cmd_list(m))
check("/list без подписок: подсказка", "нет подписок" in m.last)

main.github_get = fake_github_get
m = FakeMessage("/summary o/r")
run(main.cmd_summary(m))
check("/summary отвечает сводкой", "Сводка: o/r" in m.last)
m = FakeMessage("/readme o/r")
run(main.cmd_readme(m))
check("/readme отвечает README", "README: o/r" in m.last)
main.github_get = _orig_get

main.github_request = fake_github_request
m = FakeMessage("/deps o/r")
run(main.cmd_deps(m))
check("/deps отвечает списком зависимостей", any("requirements.txt" in a for a in m.answers))
main.github_request = _orig_request

m = FakeMessage("/summary")
run(main.cmd_summary(m))
check("/summary без аргумента и без подписок: подсказка", "Использование" in m.last)

print("\n[9] Webhook-эндпоинт и доставка")
main.add_subscription(1001, "o/r", "push,pull_request,issues")
main.add_subscription(1002, "o/r", "issues")
main.register_chat(1002, "second")

push_payload = {
    "ref": "refs/heads/main", "pusher": {"name": "ivan"},
    "repository": {"full_name": "o/r"},
    "commits": [{"id": "abc1234567", "message": "fix <b> & stuff"}],
}

fake_bot = FakeBot()
main.bot = fake_bot
resp = run(main.github_webhook(signed_request(push_payload, "push")))
check("push доставлен только подписанному чату",
      resp.get("notified") == 1 and [c["chat_id"] for c in fake_bot.calls] == [1001])
check("в Telegram ушёл экранированный текст",
      "fix &lt;b&gt; &amp; stuff" in fake_bot.calls[0]["text"])

fake_bot = FakeBot()
main.bot = fake_bot
issue_payload = {"action": "opened", "repository": {"full_name": "o/r"},
                 "issue": {"number": 1, "title": "T", "user": {"login": "u"}, "html_url": "https://x/1"}}
resp = run(main.github_webhook(signed_request(issue_payload, "issues")))
check("issue доставлен обоим подписчикам", resp.get("notified") == 2)

fake_bot = FakeBot()
main.bot = fake_bot
resp = run(main.github_webhook(signed_request(push_payload, "push", secret="wrong")))
check("неверная подпись: 401, ничего не отправлено",
      getattr(resp, "status_code", None) == 401 and fake_bot.calls == [])

main.WEBHOOK_SECRET = ""
req = FakeRequest(json.dumps(push_payload).encode(), {"X-GitHub-Event": "push"})
resp = run(main.github_webhook(req))
check("пустой секрет: POST без подписи отклоняется (401)",
      getattr(resp, "status_code", None) == 401 and fake_bot.calls == [])
main.WEBHOOK_SECRET = _saved_secret

bad_body = b"not json"
bad_sig = "sha256=" + hmac.new(b"test_secret", bad_body, hashlib.sha256).hexdigest()
resp = run(main.github_webhook(FakeRequest(bad_body, {"X-Hub-Signature-256": bad_sig, "X-GitHub-Event": "push"})))
check("битый JSON: 400", getattr(resp, "status_code", None) == 400)

resp = run(main.github_webhook(signed_request({"zen": "x", "repository": {"full_name": "o/r"}}, "ping")))
check("неподдерживаемое событие (ping) игнорируется", resp.get("status") == "ignored")

fake_bot = FakeBot(fail_on_html=True)
main.bot = fake_bot
resp = run(main.github_webhook(signed_request(push_payload, "push")))
retry = fake_bot.calls[-1] if fake_bot.calls else {}
check("ошибка разметки: повтор обычным текстом доставляется",
      resp.get("notified") == 1 and len(fake_bot.calls) == 2 and retry.get("parse_mode", "x") is None)
check("в запасной версии нет разметки бота, текст сохранён",
      "<code>" not in retry.get("text", "<code>") and "Push в o/r" in retry.get("text", "")
      and "fix <b> & stuff" in retry.get("text", ""))

fake_bot = FakeBot(fail_always=True)
main.bot = fake_bot
resp = run(main.github_webhook(signed_request(push_payload, "push")))
check("сбой доставки не роняет эндпоинт", resp.get("notified") == 0 and resp.get("failed") == 1)

print(f"\n{'=' * 40}\nИтог: {passed} пройдено, {failed} провалено")
if os.path.exists("test_database.db"):
    os.remove("test_database.db")
sys.exit(1 if failed else 0)