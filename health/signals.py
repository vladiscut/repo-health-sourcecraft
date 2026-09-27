"""Сигналы жизненного цикла сканов: очистка каталога клона."""

import logging
import shutil

from django.db.models.signals import post_save
from django.dispatch import receiver

from health.models import Scan
from integrations.git import get_scan_repo_dir

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Scan)
def cleanup_scan_clone_dir(sender, instance: Scan, **kwargs) -> None:
    """Удаляет каталог клона скана при переходе в терминальный статус.

    Устойчив к отсутствию каталога и любым ошибкам очистки: логирует
    и не выбрасывает исключение, чтобы не ломать сохранение Scan.
    """

    if instance.status not in (Scan.Status.SUCCESS, Scan.Status.FAILED):
        return

    repo_path = get_scan_repo_dir(instance.id)

    try:
        shutil.rmtree(repo_path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(
            "Не удалось удалить каталог клона scan=%s (%s): %s",
            instance.id,
            repo_path,
            exc,
        )
