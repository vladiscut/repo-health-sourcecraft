import logging

from django.core.management.base import BaseCommand

from health.tasks import task_update_all_public_repos


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Запускает получение всех публичных репозиториев'

    def handle(self, *args, **options):
        task_update_all_public_repos.delay()
        logger.info("Задача поставлена в очередь")
