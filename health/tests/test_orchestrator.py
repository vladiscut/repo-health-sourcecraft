from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from health.models import Repository, Scan
from health.tasks import task_clear_repo_tree
from health.tests.helpers import make_profile, make_repo


class _ChainedTaskSpy:
    """Перехватывает список задач, переданных в chain(*tasks)."""

    def __init__(self):
        self.tasks = []

    def __call__(self, *tasks):
        self.tasks = list(tasks)
        chain_obj = Mock()
        chain_obj.set.return_value = chain_obj
        chain_obj.apply_async.return_value = None
        return chain_obj


def _task_name(task) -> str:
    """Возвращает имя celery-задачи из подписи (subtask)."""

    return task.task


class StartRepositoryScanOrderTests(TestCase):
    def test_user_scan_clone_first(self):
        from health.orchestrator import start_repository_scan

        user = get_user_model().objects.create_user("owner", password="x")
        make_profile(user, sourcecraft_pat="pat-1")
        repo = make_repo(default_branch="main", is_empty=False)
        spy = _ChainedTaskSpy()
        with patch("health.orchestrator.chain", spy), patch(
            "health.orchestrator._get_current_commit_hash", return_value="deadbeef"
        ):
            start_repository_scan(repo.pk, user_id=user.pk)
        names = [_task_name(t) for t in spy.tasks]
        self.assertTrue(names)
        self.assertEqual(names[0], "health.tasks.task_git_clone")

    def test_aggregate_last(self):
        from health.orchestrator import start_repository_scan

        repo = make_repo(default_branch="main", is_empty=False)
        spy = _ChainedTaskSpy()
        with patch("health.orchestrator.chain", spy), patch(
            "health.orchestrator._get_current_commit_hash", return_value="deadbeef"
        ):
            start_repository_scan(repo.pk)
        names = [_task_name(t) for t in spy.tasks]
        self.assertEqual(names[-1], "health.tasks.task_aggregate_scan")

    def test_scheduled_scan_skips_clone(self):
        from health.orchestrator import start_repository_scan

        repo = make_repo(default_branch="main", is_empty=False)
        spy = _ChainedTaskSpy()
        with patch("health.orchestrator.chain", spy), patch(
            "health.orchestrator._get_current_commit_hash", return_value="deadbeef"
        ):
            start_repository_scan(repo.pk)
        names = [_task_name(t) for t in spy.tasks]
        self.assertNotIn("health.tasks.task_git_clone", names)

    def test_prime_before_consumers_before_clear(self):
        from health.orchestrator import start_repository_scan

        repo = make_repo(default_branch="main", is_empty=False)
        spy = _ChainedTaskSpy()
        with patch("health.orchestrator.chain", spy), patch(
            "health.orchestrator._get_current_commit_hash", return_value="deadbeef"
        ):
            start_repository_scan(repo.pk)
        names = [_task_name(t) for t in spy.tasks]
        docs_i = names.index("health.tasks.task_docs_scan")
        code_i = names.index("health.tasks.task_code_health_scan")
        clear_i = names.index("health.tasks.task_clear_repo_tree")

        self.assertLess(docs_i, clear_i)
        self.assertLess(code_i, clear_i)


class EmptyRepositoryScanTests(TestCase):
    def test_repo_without_branch_stores_none_not_zero(self):
        from health.orchestrator import start_repository_scan

        repo = make_repo(default_branch="", is_empty=False)
        start_repository_scan(repo.pk)
        repo.refresh_from_db()
        scan = repo.latest_completed_scan()

        self.assertIsNone(repo.health_score)
        self.assertIsNotNone(scan)
        self.assertIsNone(scan.raw["health_score"])
        self.assertEqual(scan.scores.count(), 6)
        self.assertTrue(all(row.total is None for row in scan.scores.all()))

    def test_empty_flag_stores_none_not_zero(self):
        from health.orchestrator import start_repository_scan

        repo = make_repo(default_branch="main", is_empty=True)
        start_repository_scan(repo.pk)
        repo.refresh_from_db()
        self.assertIsNone(repo.health_score)
        self.assertIsNone(repo.latest_completed_scan().raw["health_score"])


class AggregatePreliminaryScoreTests(TestCase):
    def test_thin_coverage_hides_repo_cache(self):
        from health.models import HealthScore, MetricSample
        from health.orchestrator import aggregate_scan
        from health.scoring import CATEGORY_WEIGHTS

        repo = make_repo(default_branch="main", is_empty=False)
        scan = Scan.objects.create(
            repository=repo,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            commit_sha_at_analysis="abc",
        )
        for category in CATEGORY_WEIGHTS:
            total = 99 if category == MetricSample.Category.ACTIVITY else None
            HealthScore.objects.create(
                scan=scan,
                category=category,
                total=total,
                weight_used=0.0,
                data_completeness=1.0 if total is not None else 0.0,
            )

        result = aggregate_scan(scan.id)
        scan.refresh_from_db()
        repo.refresh_from_db()

        self.assertEqual(scan.raw["health_score"], 99)
        self.assertTrue(scan.raw["is_preliminary"])
        self.assertIsNone(repo.health_score)
        self.assertIsNone(result["health_score"])

    def test_partial_does_not_overwrite_repo_score(self):
        from health.models import HealthScore, MetricSample
        from health.orchestrator import aggregate_scan

        repo = make_repo(default_branch="main", is_empty=False, health_score=77)
        scan = Scan.objects.create(
            repository=repo,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            commit_sha_at_analysis="abc",
        )
        HealthScore.objects.create(
            scan=scan,
            category=MetricSample.Category.ACTIVITY,
            total=50,
            weight_used=0.0,
            data_completeness=1.0,
        )

        result = aggregate_scan(scan.id)
        scan.refresh_from_db()
        repo.refresh_from_db()

        self.assertEqual(scan.status, Scan.Status.PARTIAL)
        self.assertEqual(repo.health_score, 77)
        self.assertIsNone(result["health_score"])


class TreeCacheTaskTests(SimpleTestCase):

    def test_clear_deletes_cache(self):
        repo = Mock(default_branch="main", sourcecraft_id="sc-1")
        scan = Mock(repository=repo)
        with patch("health.models.Scan.objects") as scan_mgr, patch(
            "health.tree_cache.clear_repository_tree_cache"
        ) as clear:
            scan_mgr.select_related.return_value.filter.return_value.first.return_value = scan
            task_clear_repo_tree.run(1)
        clear.assert_called_once_with(repo)
