"""Тесты health/activity_scan.py."""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from health.activity_scan import (
    CATEGORY,
    _ActivityMetrics,
    _compute_metrics,
    _build_findings,
    _save_metric_samples,
    run,
)
from health.models import Finding, HealthScore, MetricSample, Scan
from health.tests.helpers import make_profile, make_repo
from integrations.sourcecraft import SourceCraftError


class ComputeMetricsTests(TestCase):
    def setUp(self):
        self.repo = make_repo(default_branch="main", last_updated=None)
        self.scan = Scan.objects.create(repository=self.repo)
        self.now = timezone.now()

    def _client(self, **kwargs):
        client = Mock()
        client.get_merge_requests.return_value = kwargs.get("mrs", [])
        client.get_releases.return_value = kwargs.get("releases", [])
        return client

    def test_no_commits_when_not_included(self):
        client = self._client()
        metrics = _compute_metrics(
            client, None, self.repo, self.now, scan_id=self.scan.pk, include_commits=False
        )
        self.assertIsNone(metrics.commits_30d)
        self.assertIn("не считаются", metrics.commits_fetch_error)

    def test_fetch_errors_recorded(self):
        client = Mock()
        client.get_merge_requests.side_effect = SourceCraftError("mrs fail")
        client.get_releases.side_effect = SourceCraftError("rel fail")
        metrics = _compute_metrics(
            client, None, self.repo, self.now, scan_id=self.scan.pk, include_commits=False
        )
        self.assertIn("merge_requests", metrics.fetch_errors)
        self.assertIn("releases", metrics.fetch_errors)
        self.assertIsNone(metrics.merge_requests_total)

    def test_last_activity_from_mrs(self):
        latest = self.now - timedelta(days=2)
        client = self._client(mrs=[{"updated_at": latest.isoformat()}])
        metrics = _compute_metrics(
            client, None, self.repo, self.now, scan_id=self.scan.pk, include_commits=False
        )
        self.assertEqual(metrics.last_activity_source, "merge_requests")
        self.assertAlmostEqual(metrics.last_activity_age_days, 2.0, places=1)

    def test_commits_from_git_client(self):
        git_client = Mock()
        git_client.get_commit_history.return_value = [self.now - timedelta(days=1)]
        client = self._client()
        metrics = _compute_metrics(
            client, git_client, self.repo, self.now, scan_id=self.scan.pk, include_commits=True
        )
        self.assertEqual(metrics.commits_30d, 1)
        self.assertIsNotNone(metrics.commit_frequency_week)
        self.assertEqual(metrics.last_activity_source, "commits")

    def test_commits_git_error(self):
        git_client = Mock()
        git_client.get_commit_history.side_effect = SourceCraftError("clone fail")
        client = self._client()
        metrics = _compute_metrics(
            client, git_client, self.repo, self.now, scan_id=self.scan.pk, include_commits=True
        )
        self.assertIsNone(metrics.commits_30d)
        self.assertIn("commits", metrics.fetch_errors)


class SaveMetricSamplesTests(TestCase):
    def test_saves_all_keys(self):
        repo = make_repo()
        scan = Scan.objects.create(repository=repo)
        metrics = _ActivityMetrics(
            commits_30d=5,
            commit_frequency_week=1.2,
            last_activity_at=timezone.now(),
            last_activity_age_days=1.0,
            last_activity_source="commits",
            merge_requests_total=2,
            merge_requests_30d=1,
            releases_total=4,
            releases_30d=1,
        )
        _save_metric_samples(scan, metrics)
        keys = set(
            MetricSample.objects.filter(scan=scan, category=CATEGORY)
            .values_list("metric_key", flat=True)
        )
        for key in (
            "activity_commits_30d",
            "activity_commit_frequency_week",
            "activity_last_activity_at",
            "activity_last_activity_age_days",
            "activity_last_activity_source",
            "activity_merge_requests_total",
            "activity_merge_requests_30d",
            "activity_releases_total",
            "activity_releases_30d",
        ):
            self.assertIn(key, keys)


class BuildFindingsTests(TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.scan = Scan.objects.create(repository=self.repo)

    def test_stale_activity_finding(self):
        metrics = _ActivityMetrics(
            last_activity_age_days=400.0,
            last_activity_source="commits",
            last_activity_at=timezone.now(),
        )
        _build_findings(self.scan, metrics, 50, include_commits=False)
        titles = set(
            Finding.objects.filter(scan=self.scan).values_list("title", flat=True)
        )
        self.assertIn("Проект давно не проявлял активность", titles)

    def test_no_findings_for_healthy(self):
        metrics = _ActivityMetrics(
            last_activity_age_days=1.0,
            releases_total=3,
            releases_30d=1,
            merge_requests_30d=2,
        )
        _build_findings(self.scan, metrics, 90, include_commits=False)
        self.assertEqual(Finding.objects.filter(scan=self.scan).count(), 0)

    def test_insufficient_data_finding(self):
        metrics = _ActivityMetrics()
        _build_findings(self.scan, metrics, None, include_commits=False)
        titles = set(
            Finding.objects.filter(scan=self.scan).values_list("title", flat=True)
        )
        self.assertIn("Недостаточно данных для оценки категории Activity", titles)


class RunActivityScanTests(TestCase):
    def test_user_scan_uses_user_token(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user("u", password="x")
        make_profile(user, sourcecraft_pat="user-tok")
        repo = make_repo(default_branch="main")
        scan = Scan.objects.create(
            repository=repo,
            triggered_by=Scan.TriggeredBy.USER,
            triggered_by_user=user,
        )
        with patch("health.activity_scan.SourceCraftClient") as client_cls, patch(
            "health.activity_scan.SourceCraftGitClient"
        ) as git_cls, patch(
            "health.activity_scan.run_activity_scan"
        ) as run_scan:
            run_scan.return_value = Mock(pk=1)
            client_cls.return_value.get_merge_requests.return_value = []
            git_cls.return_value.get_commit_history.return_value = []
            run(scan.id)
        client_cls.assert_called_once_with(token="user-tok")
        git_cls.assert_called_once_with(token="user-tok")

    def test_scheduled_scan_no_git_client(self):
        repo = make_repo(default_branch="main")
        scan = Scan.objects.create(
            repository=repo, triggered_by=Scan.TriggeredBy.SCHEDULE
        )
        with patch("health.activity_scan.SourceCraftClient") as client_cls, patch(
            "health.activity_scan.SourceCraftGitClient"
        ) as git_cls, patch(
            "health.activity_scan.run_activity_scan"
        ) as run_scan:
            run_scan.return_value = Mock(pk=1)
            run(scan.id)
        client_cls.assert_called_once_with(token=None)
        git_cls.assert_not_called()
