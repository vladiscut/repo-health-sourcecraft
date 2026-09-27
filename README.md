# Repo Health

Сервис оценки здоровья открытых репозиториев SourceCraft.
Стек: Django, шаблоны, DRF, Postgres, Redis, Celery.

## Как поднять локально

Нужен Docker. В `.env` — токен SourceCraft, иначе каталог не приедет.

```powershell
cd G:\NextDevProjects\repo-health
copy .env.example .env
# в .env: SOURCECRAFT_API_TOKEN=<PAT команды>
# для /me/: YANDEX_CLIENT_ID и YANDEX_CLIENT_SECRET приложения Я ID
docker compose up --build
```

Открыть http://127.0.0.1:8002/

- `/` — публичный список, по 50 репо, фильтр по языку, сортировка по Score / рейтингу
- `/repos/<org>/<repo>/` — карточка, заготовка выгрузки Markdown/PDF
- `/me/` — личные репозитории после входа через Я ID
- `/admin/` — Django admin
- `/api/v1/repos/` — JSON, тоже по 50

Первый запуск: Celery worker при старте ставит `task_update_all_public_repos`. Каталог копится пачками (~2000 за проход), всего в SourceCraft больше 27 тысяч. Страницу списка можно открывать сразу — она больше не грузит всю таблицу.

Остановить: `Ctrl+C` в том же терминале или `docker compose down`. База на диске остаётся (`postgres-repo-health-volume`).

## Тесты

Без Docker, SQLite in-memory:

```powershell
python manage.py test health --settings=core.test_settings -v2
```

## Что сейчас работает

- Обход публичных репо через `GET /repos` (Discover)
- Список с пагинацией, поиском, фильтром по языку, сортировкой по Score / рейтингу SourceCraft / дате / имени
- Карточка с метаданными, реакциями 👍❤️💎 и заготовкой выгрузки Markdown/PDF (пока пустой файл)
- Вход через Я ID и страница `/me/`: PAT SourceCraft, список своих репо, кнопка запуска анализа (очередь, сбор метрик ещё заглушка)

## Чего нет и чего не делать

- Score почти у всех «нет данных»: скан метрик (`task_scan_user_repository`) ещё заглушка. Кнопка анализа ставит задачу, но оценку не посчитает.
- Не гоняйте массовый scan на 27k репо — сожжёте лимит API (100 rps), пользы нет.
- CI/CD и AppSec в публичном рейтинге не смотрим — только личный кабинет (`/me/`).
- Отчёт Markdown/PDF — заготовка, файл пока пустой.
- `seed_demo` больше не запускается при старте. Файл команды может лежать в репо, для локального стенда не нужен.

## Как устроен проект

| Путь | Зачем |
|---|---|
| `health/views.py` | HTML: публичный список, карточка, выгрузка |
| `health/account.py` | Я ID и `/me/` |
| `templates/health/` | Шаблоны |
| `health/services.py` | Маппинг JSON SourceCraft → `Repository` |
| `health/tasks.py` | Celery: обход каталога; scan — TODO |
| `integrations/sourcecraft.py` | HTTP-клиент API |
| `integrations/yandex.py` | OAuth Я ID |
| `health/scoring.py` | Формула Score (пока не подключена к scan) |
| `health/models.py` | Repository, Scan, MetricSample, HealthScore, Finding, Profile |
| `core/` | settings, urls, celery |
| `docker-compose.yml` | web, postgres, redis, worker, beat |

Два контура: `/` публичный, `/me/` личный. Для `/me/` нужен Я ID и PAT SourceCraft.

Юрий ведёт ingest (клиент, services, discover-таски). Список и карточка — витрина, её можно править не пересекаясь с ним.

## Если что-то не открывается

1. `docker compose ps` — все пять сервисов `running` / `healthy`.
2. Нет репо на `/` — нет токена или worker ещё не отходил. Логи: `docker compose logs celery-repo-health`.
3. Страница тормозит — убедитесь, что это свежий код с пагинацией (в подзаголовке есть число репозиториев и блок «50»).
