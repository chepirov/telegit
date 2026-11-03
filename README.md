# telegit — GitHub-ассистент в Telegram

Telegram-бот, который следит за репозиториями GitHub: присылает уведомления
о `push`, `pull request` и `issue`, а по запросу показывает сводку по
репозиторию, его README и файлы зависимостей.

**Стек:** Python 3.10+, aiogram 3 (Telegram), FastAPI + uvicorn (приём webhook),
httpx (GitHub REST API), SQLite.

## Как это работает

```
GitHub --webhook POST /webhook--> FastAPI --> SQLite (кто подписан) --> Telegram
Telegram <--команды--> aiogram --> GitHub REST API (сводка, README, зависимости)
```

Бот и веб-сервер запускаются одним процессом (`python main.py`).
Подписки, чаты и фильтры событий хранятся в SQLite.

## Установка

```bash
git clone https://github.com/chepirov/telegit.git
cd telegit
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
```

В `requirements.txt` должны быть: `aiogram` (версия 3.x), `fastapi`, `uvicorn`,
`httpx`, `python-dotenv`.

## Настройка (файл `.env`)

Создайте файл `.env` рядом с `main.py`:

```env
TELEGRAM_TOKEN=123456789:AA...      # токен бота от @BotFather
GITHUB_TOKEN=ghp_...                # токен GitHub (см. ниже)
```

Больше в `.env` ничего добавлять не нужно. Необязательные переменные:

| Переменная       | По умолчанию    | Назначение                                                                 |
|------------------|-----------------|----------------------------------------------------------------------------|
| `DATABASE_PATH`  | `github_bot.db` | путь к файлу SQLite                                                        |
| `PORT`           | `8000`          | порт веб-сервера                                                           |
| `WEBHOOK_SECRET` | создаётся сам   | секрет подписи webhook; если не задан, бот один раз генерирует его и хранит в базе |
| `PUBLIC_URL`     | —               | публичный адрес сервера; вместо него можно использовать команду `/seturl`  |

### Токен GitHub

Бот создаёт webhook в репозитории, поэтому токену нужны права на управление
webhook:

* classic-токен: права `repo` и `admin:repo_hook`;
* fine-grained-токен: доступ к нужным репозиториям и разрешения
  *Webhooks: Read and write*, *Contents: Read*, *Issues: Read*,
  *Pull requests: Read*, *Metadata: Read*.

Аккаунт, которому принадлежит токен, должен иметь доступ **admin** к
репозиториям, на которые вы подписываетесь. Для чужих репозиториев webhook
создать нельзя — тогда работают только `/summary`, `/readme` и `/deps`.

## Запуск

```bash
python main.py
```

Бот начнёт принимать команды в Telegram, а сервер — слушать `0.0.0.0:8000`.
Проверка, что сервер жив: `http://localhost:8000/health` вернёт `{"status": "ok"}`.

## Подключение webhook (чтобы приходили уведомления)

GitHub должен достучаться до вашего сервера из интернета. Для локального
запуска используйте туннель, например ngrok:

```bash
ngrok http 8000
```

Скопируйте выданный адрес вида `https://abc123.ngrok-free.app` и отправьте
боту в Telegram:

```
/seturl https://abc123.ngrok-free.app
```

После этого:

* `/subscribe owner/repo` сам создаёт webhook в репозитории (адрес
  `…/webhook`, формат JSON, события push / pull_request / issues, подпись
  HMAC-SHA256);
* для подписок, оформленных раньше, webhook создаётся сразу после `/seturl`;
* `/unsubscribe` удаляет webhook, когда подписчиков у репозитория не осталось.

Команду `/seturl` может выполнять только администратор — тот, кто первым
успешно задал адрес. Это защита от подмены адреса, на который уходят события.
Если адрес туннеля изменился (бесплатный ngrok меняет его при каждом запуске),
повторите `/seturl` с новым адресом.

### Ручная настройка webhook (запасной вариант)

Если у токена нет прав admin, добавьте webhook вручную:
*Settings → Webhooks → Add webhook*:

* Payload URL: `https://ваш-адрес/webhook`
* Content type: `application/json`
* Secret: значение `WEBHOOK_SECRET` из `.env`. Для ручного режима эту
  переменную нужно добавить в `.env` самостоятельно: секрет, который бот
  создаёт автоматически, предназначен для webhook, созданных самим ботом
* Events: *Pushes*, *Pull requests*, *Issues*

Запросы без верной подписи сервер отклоняет (HTTP 401) всегда.

## Команды бота

| Команда                           | Действие                                                       |
|-----------------------------------|----------------------------------------------------------------|
| `/start`                          | справка                                                        |
| `/subscribe owner/repo [события]` | подписаться; повторный вызов с событиями меняет фильтр         |
| `/unsubscribe owner/repo`         | отписаться                                                     |
| `/list`                           | мои подписки и фильтры                                         |
| `/summary [owner/repo]`           | описание, звёзды, открытые PR и issue, последние коммиты       |
| `/readme [owner/repo]`            | README репозитория                                             |
| `/deps [owner/repo]`              | файлы зависимостей (requirements.txt, package.json, go.mod и др.) |
| `/seturl https://адрес`           | публичный адрес сервера (только администратор)                 |

Репозиторий можно указать как `owner/repo` или ссылкой
`https://github.com/owner/repo`. Если у чата одна подписка, репозиторий в
`/summary`, `/readme` и `/deps` можно не указывать.

События (через запятую, без пробелов): `push`, `pull_request`, `issues`.

Примеры:

```
/subscribe octocat/Hello-World                      # все события
/subscribe octocat/Hello-World push,issues          # только push и issues
/subscribe octocat/Hello-World pull_request         # сменить фильтр
/summary octocat/Hello-World
```

## База данных

SQLite, схема создаётся при первом запуске:

* `chats` — чаты, обращавшиеся к боту;
* `subscriptions` — подписки (чат, репозиторий, фильтр событий `events_filter`);
* `settings` — служебные значения (публичный адрес, администратор, секрет
  webhook, если он не задан в `.env`).

## Тесты

```bash
python test_bot.py
```

Сеть, настоящий Telegram и токены не нужны: обращения к GitHub и Telegram
подменяются заглушками. Тесты проверяют разбор репозитория, базу данных,
подпись webhook, форматирование и экранирование уведомлений, `/summary`,
`/readme`, `/deps`, создание и удаление webhook в репозитории, все команды
бота, приём события на `/webhook` и доставку (в том числе запасную отправку
обычным текстом при ошибке разметки). Код возврата `0` — все проверки пройдены.

## Структура

```
main.py          бот, веб-сервер, работа с БД и GitHub API
test_bot.py      автотесты
requirements.txt зависимости
README.md        эта инструкция
```
