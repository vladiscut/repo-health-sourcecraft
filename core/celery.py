import os

from celery import Celery
from celery.signals import task_postrun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_postrun.connect
def _close_db_connections_after_task(**kwargs) -> None:
    from django.db import close_old_connections

    close_old_connections()


USER_QUEUE_NAME = "analysis.user"
SCHEDULE_QUEUE_NAME = "analysis.scheduled"

app.conf.task_routes = {
    "health.tasks.task_update_all_public_repos": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_scan_all_public_repositories": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_issues_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_docs_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_cicd_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_security_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_activity_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_code_health_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_aggregate_scan": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_reap_stale_scans": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_reap_orphan_clone_dirs": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_check_and_scan_repository": {"queue": SCHEDULE_QUEUE_NAME},
    "health.tasks.task_confirm_access_and_scan": {"queue": USER_QUEUE_NAME},
}
