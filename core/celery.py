import os

from celery import Celery
from celery.signals import task_postrun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_postrun.connect
def _close_db_connections_after_task(**kwargs) -> None:
    """Закрывает соединение Django с БД по завершении каждой задачи.

    Обязательно для воркеров на gevent-пуле
    Без явного закрытия упирается в лимит открытых файлов процесса

    close_old_connections() при стандартном для Django CONN_MAX_AGE=0
    закрывает соединение безусловно, а не только "протухшие" — то есть
    после каждой задачи соединение гарантированно освобождается.
    """

    from django.db import close_old_connections

    close_old_connections()


app.conf.task_routes = {
    # получение всех публичных репозиторией
    "health.tasks.task_update_all_public_repos": {"queue": "analysis.scheduled"},
    # сканирование для каждого публичного репозитория
    "health.tasks.task_scan_all_public_repositories": {"queue": "analysis.scheduled"},
    # скан категории ISSUES
    "health.tasks.task_issues_scan": {"queue": "analysis.scheduled"},
    # скан категории ISSUES
    "health.tasks.task_docs_scan": {"queue": "analysis.scheduled"},
    # скан категории DOCS
    "health.tasks.task_cicd_scan": {"queue": "analysis.scheduled"},
    # скан категории SECURITY
    "health.tasks.task_security_scan": {"queue": "analysis.scheduled"},
    # скан категории ACTIVITY
    "health.tasks.task_activity_scan": {"queue": "analysis.scheduled"},
    # скан категории CODE_HEALTH
    "health.tasks.task_code_health_scan": {"queue": "analysis.scheduled"},
    # cчитает score по всем категориям
    "health.tasks.task_aggregate_scan": {"queue": "analysis.scheduled"},
    # снимает зависшие сканы
    "health.tasks.task_reap_stale_scans": {"queue": "analysis.scheduled"},
    # проверка и запуск скана
    "health.tasks.task_check_and_scan_repository": {"queue": "analysis.scheduled"},
    # скан репозитория пользователя
    "health.tasks.task_scan_user_repository": {"queue": "analysis.user"},
}
