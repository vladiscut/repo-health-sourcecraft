from health.models import Scan
from integrations.git import SourceCraftGitClient


def run(scan_id: int) -> None:
    scan = Scan.objects.select_related("repository").get(pk=scan_id)

    if scan.triggered_by_user_id:
        token = scan.triggered_by_user.profile.sourcecraft_token
        git_client = SourceCraftGitClient(token=token)
        git_client.clone(
            scan.repository.org_slug,
            scan.repository.repo_slug,
            scan.repository.default_branch,
            scan_id,
        )
