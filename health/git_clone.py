import logging

from health.models import Scan
from integrations.git import SourceCraftGitClient

logger = logging.getLogger(__name__)


def run(scan_id: int) -> None:
    scan = Scan.objects.select_related(
        "triggered_by_user", "repository"
    ).get(pk=scan_id)

    if not scan.triggered_by_user_id:
        return

    token = scan.triggered_by_user.profile.sourcecraft_token
    git_client = SourceCraftGitClient(token=token)
    try:
        git_client.clone(
            scan.repository.org_slug,
            scan.repository.repo_slug,
            scan.repository.default_branch,
            scan_id,
        )
    except Exception:
        # Клон нужен docs/code/activity, но его сбой не должен обрывать
        # цепочку: иначе Scan остаётся RUNNING до таймаута зависших.
        logger.exception(
            "git clone не удался для scan=%s, анализ продолжится без клона",
            scan_id,
        )
