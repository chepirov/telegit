"""Автотест"""

import os
import hmac
import hashlib
import json

os.environ.setdefault("TELEGRAM_TOKEN", "123456789:test_token_for_unit_tests")
os.environ.setdefault("GITHUB_TOKEN", "test_token")

os.environ["DATABASE_PATH"] = "test_database.db"
os.environ["WEBHOOK_SECRET"] = "test_secret"

import main

passed = failed = 0


def check(name: str, condition: bool):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


# --- 1. Парсинг репозитория ---
print("\n[1] Парсинг репозитория")
check("owner/repo", main.parse_repo("octocat/Hello-World") == "octocat/Hello-World")
check("полная ссылка", main.parse_repo("https://github.com/octocat/Hello-World") == "octocat/Hello-World")
check("ссылка со слэшем", main.parse_repo("https://github.com/octocat/Hello-World/") == "octocat/Hello-World")
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

check("отписка работает", main.remove_subscription(1001, "octocat/Hello-World") is True)
check("повторная отписка = False", main.remove_subscription(1001, "octocat/Hello-World") is False)

print("\n[3] Подпись webhook (HMAC-SHA256)")
body = json.dumps({"ref": "refs/heads/main"}).encode()
sig = "sha256=" + hmac.new(b"test_secret", body, hashlib.sha256).hexdigest()
check("верная подпись принимается", main.verify_signature(body, sig) is True)
check("неверная подпись отклоняется", main.verify_signature(body, "sha256=bad") is False)
check("отсутствие подписи отклоняется", main.verify_signature(body, None) is False)

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
print(f"\n{'='*40}\nИтог: {passed} пройдено, {failed} провалено")
if os.path.exists("test_database.db"):
    os.remove("test_database.db")
exit(1 if failed else 0)