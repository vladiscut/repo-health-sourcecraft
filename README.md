# Repo Health

Веб-сервис оценки здоровья репозиториев SourceCraft. Считает **Repo Health Score** от 0 до 100, объясняет балл по шести категориям и отдаёт приоритизированные рекомендации.

Стек: Django, шаблоны, Django REST framework, Postgres, Redis, Celery.

Методика: [docs/scoring.md](docs/scoring.md). Архитектура, API и ограничения: [docs/architecture.md](docs/architecture.md).

## Как поднять

Нужен Docker. Скопируйте `.env.example` в `.env` и укажите `SOURCECRAFT_API_TOKEN` — без него каталог публичных репозиториев не загрузится. Для личного кабинета `/me/` нужны `YANDEX_CLIENT_ID` и `YANDEX_CLIENT_SECRET`.

```powershell
copy .env.example .env
docker compose up --build
```

Откройте http://127.0.0.1:8002/

| Адрес | Что там |
|---|---|
| `/` | Публичный рейтинг, по 50 репозиториев на страницу |
| `/repos/<org>/<repo>/` | Карточка: Score, категории, рекомендации, выгрузка Markdown |
| `/me/` | Вход через Я ID, PAT SourceCraft, свои репозитории |
| `/api/v1/repos/` | JSON публичного каталога, тоже по 50 |
| `/admin/` | Django admin |

Остановить: `Ctrl+C` или `docker compose down`. База остаётся в томе `postgres-repo-health-volume`.

Первый запуск, если таблица репозиториев пустая, ставит обход каталога. Страницу списка можно открыть сразу: она не грузит всю таблицу.

## Два контура

Публичный рейтинг считает документацию, активность, issues и состояние кода. CI/CD и Security для чужого репозитория не запрашиваются: на карточке они «Нет данных», их вес переходит к категориям, где балл есть.

После входа через Я ID и сохранения PAT кнопка «Запустить анализ» на своём репозитории считает все шесть категорий, включая CI и AppSec. Закрытый репозиторий в публичный список не попадает.

Лайки в формулу Score не входят. Они остаются колонкой реакций в рейтинге.

## Расписание

Celery beat (`celery-beat-repo-health`):

| Задача | Когда |
|---|---|
| Обход каталога публичных репозиториев | каждые 12 часов, в 00:00 и 12:00 |
| Плановый пересчёт Score | каждые 6 часов, в :30 |
| Снятие зависших сканов | каждые 20 минут |
| Удаление осиротевших каталогов клонов | каждый час |

Повторный плановый скан пропускает репозиторий, если хеш ветки по умолчанию не изменился и уже есть завершённый скан. Зависшим считается скан, который дольше `SCAN_STALE_TIMEOUT_MINUTES` остаётся в ожидании или в работе.

Проверить beat: `docker compose logs celery-beat-repo-health`.

Очереди разделены. Плановый обход слушает `celery-scheduled-repo-health` (`analysis.scheduled`). Нажатие «Запустить анализ» на своём репозитории уходит в `celery-user-repo-health` (`analysis.user`).

## Тесты

Без Docker, SQLite в памяти:

```powershell
python manage.py test health integrations --settings=core.test_settings -v2
```

## Стенд

```powershell
docker compose -f docker-compose-server-prod.yml up -d --build
```

Web слушает gunicorn и публикует порт только на `127.0.0.1`. В `.env` стенда: `DEBUG=False`, `VIRTUAL_HOSTS`, `ALLOWED_HOSTS` и `YANDEX_REDIRECT_URI=https://<домен>/auth/yandex/callback/`. HTTPS на этой ВМ поднимается отдельным reverse proxy: контейнера Caddy или nginx в репозитории нет.

## Если страница пустая

1. `docker compose ps` — web, postgres, pgbouncer, redis, оба воркера и beat в состоянии running.
2. Нет репозиториев на `/` — пустой `SOURCECRAFT_API_TOKEN` или каталог ещё не дошёл. Логи: `docker compose logs celery-scheduled-repo-health`.
3. На карточке «Нет данных» по CI и Security у чужого публичного репозитория — так и задумано, пока анализ не запущен владельцем со своим PAT.
