"""Unit-тесты для :mod:`health.issues_scan`."""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from health.issues_scan import (
    _IssuesMetrics,
    _build_findings,
    _compute_metrics,
    _save_metric_samples,
    run,
    run_issues_scan,
)
from health.models import Finding, HealthScore, MetricSample, Scan
from health.tests.helpers import make_repo
from integrations.sourcecraft import SourceCraftError


class IssuesMetricsTests(SimpleTestCase):
    def test_defaults(self):
        m = _IssuesMetrics()
        self.assertEqual(m.total_count, 0)
        self.assertEqual(m.open_count, 0)
        self.assertEqual(m.closed_count, 0)
        self.assertEqual(m.created_30d, 0)
        self.assertEqual(m.closed_30d, 0)
        self.assertIsNone(m.close_rate_30d)
        self.assertEqual(m.stale_count, 0)
        self.assertIsNone(m.stale_ratio)
        self.assertIsNone(m.median_time_to_close_days)
        self.assertIsNone(m.median_first_response_hours)
        self.assertEqual(m.first_response_sample_size, 0)
        self.assertFalse(m.first_response_available)
        self.assertEqual(m.stale_issue_refs, [])


class ComputeMetricsTests(SimpleTestCase):
    def _client(self, comments=None):
        client = Mock()
        client.get_issue_events.return_value = comments or []
        return client

    def _issue(self, **kwargs):
        base = {
            "id": 1,
            "state": "open",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        base.update(kwargs)
        return base

    def test_counts_total_open_closed(self):
        now = timezone.now()
        issues = [
            self._issue(id=1, state="open"),
            self._issue(id=2, state="closed", closed_at="2024-01-10T00:00:00Z"),
            self._issue(id=3, state="resolved", closed_at="2024-01-11T00:00:00Z"),
        ]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertEqual(m.total_count, 3)
        self.assertEqual(m.open_count, 1)
        self.assertEqual(m.closed_count, 2)

    def test_created_and_closed_30d(self):
        now = timezone.now()
        recent = (now - timedelta(days=5)).isoformat()
        old = (now - timedelta(days=100)).isoformat()
        issues = [
            self._issue(id=1, created_at=recent, state="closed", closed_at=recent),
            self._issue(id=2, created_at=old, state="closed", closed_at=recent),
            self._issue(id=3, created_at=recent, state="open"),
        ]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertEqual(m.created_30d, 2)
        self.assertEqual(m.closed_30d, 2)

    def test_close_rate_30d(self):
        now = timezone.now()
        recent = (now - timedelta(days=1)).isoformat()
        issues = [
            self._issue(id=1, created_at=recent, state="closed", closed_at=recent),
            self._issue(id=2, created_at=recent, state="closed", closed_at=recent),
            self._issue(id=3, created_at=recent, state="open"),
            self._issue(id=4, created_at=recent, state="open"),
        ]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertAlmostEqual(m.close_rate_30d, 0.5)

    def test_stale_count_and_ratio(self):
        now = timezone.now()
        old_update = (now - timedelta(days=200)).isoformat()
        recent_update = (now - timedelta(days=1)).isoformat()
        issues = [
            self._issue(id=10, state="open", updated_at=old_update),
            self._issue(id=11, state="open", updated_at=recent_update),
        ]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertEqual(m.stale_count, 1)
        self.assertAlmostEqual(m.stale_ratio, 0.5)
        self.assertEqual(m.stale_issue_refs, ["10"])

    def test_median_time_to_close(self):
        now = timezone.now()
        issues = [
            self._issue(
                id=1,
                state="closed",
                created_at="2024-01-01T00:00:00Z",
                closed_at="2024-01-03T00:00:00Z",
            ),
            self._issue(
                id=2,
                state="closed",
                created_at="2024-01-01T00:00:00Z",
                closed_at="2024-01-05T00:00:00Z",
            ),
        ]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertAlmostEqual(m.median_time_to_close_days, 3.0)

    def test_median_first_response_and_sample(self):
        now = timezone.now()
        recent = (now - timedelta(days=1)).isoformat()
        issues = [self._issue(id=42, created_at=recent)]
        comments = [
            {"created_at": (now - timedelta(hours=4)).isoformat()},
        ]
        client = self._client(comments=comments)
        m = _compute_metrics(client, "repo", issues, now)
        self.assertTrue(m.first_response_available)
        self.assertEqual(m.first_response_sample_size, 1)
        self.assertGreater(m.median_first_response_hours, 0)

    def test_first_response_unavailable_without_events(self):
        now = timezone.now()
        issues = [self._issue(id=1)]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertFalse(m.first_response_available)
        self.assertIsNone(m.median_first_response_hours)

    def test_empty_issues_gives_zeros(self):
        now = timezone.now()
        m = _compute_metrics(self._client(), "repo", [], now)
        self.assertEqual(m.total_count, 0)
        self.assertIsNone(m.close_rate_30d)
        self.assertIsNone(m.stale_ratio)

    def test_missing_dates_do_not_break(self):
        now = timezone.now()
        issues = [{"id": 1, "state": "open"}]
        m = _compute_metrics(self._client(), "repo", issues, now)
        self.assertEqual(m.total_count, 1)
        self.assertEqual(m.open_count, 1)


class IssuesScanTests(TestCase):
    def _scan(self):
        repo = make_repo()
        return Scan.objects.create(repository=repo)

    def _client(self, issues=None):
        client = Mock()
        if issues is None:
            client.get_issues.side_effect = SourceCraftError("boom", status_code=500)
        else:
            client.get_issues.return_value = issues
        client.get_issue_events.return_value = []
        return client

    def test_fetch_error_writes_metric_sample(self):
        scan = self._scan()
        client = self._client()
        run_issues_scan(scan, client)
        sample = MetricSample.objects.get(
            scan=scan, category=MetricSample.Category.ISSUES,
            metric_key="issues_fetch_error",
        )
        self.assertFalse(sample.is_available)
        self.assertIn("boom", sample.error_reason)

    def test_fetch_error_writes_health_score_none(self):
        scan = self._scan()
        run_issues_scan(scan, self._client())
        hs = HealthScore.objects.get(scan=scan, category=MetricSample.Category.ISSUES)
        self.assertIsNone(hs.total)
        self.assertEqual(hs.data_completeness, 0.0)

    def test_success_creates_health_score(self):
        scan = self._scan()
        now = timezone.now()
        issues = [
            {
                "id": 1,
                "state": "closed",
                "created_at": (now - timedelta(days=2)).isoformat(),
                "updated_at": now.isoformat(),
                "closed_at": now.isoformat(),
            },
        ]
        run_issues_scan(scan, self._client(issues))
        hs = HealthScore.objects.get(scan=scan, category=MetricSample.Category.ISSUES)
        self.assertEqual(
            set(hs.raw_metrics.keys()),
            {
                "total_count", "open_count", "closed_count",
                "created_30d", "closed_30d", "close_rate_30d",
                "stale_count", "stale_ratio", "median_time_to_close_days",
                "median_first_response_hours", "submetric_scores",
            },
        )

    def test_success_writes_metric_samples(self):
        scan = self._scan()
        run_issues_scan(scan, self._client([]))
        keys = set(
            MetricSample.objects.filter(
                scan=scan, category=MetricSample.Category.ISSUES
            ).values_list("metric_key", flat=True)
        )
        self.assertIn("issues_total_count", keys)
        self.assertIn("issues_stale_ratio", keys)

    def test_build_findings_no_tracker(self):
        scan = self._scan()
        run_issues_scan(scan, self._client([]))
        self.assertTrue(
            Finding.objects.filter(
                scan=scan,
                category=MetricSample.Category.ISSUES,
                title="В репозитории не используется трекер issues",
            ).exists()
        )

    def test_build_findings_idempotent(self):
        scan = self._scan()
        client = self._client([])
        run_issues_scan(scan, client)
        first = Finding.objects.filter(
            scan=scan, category=MetricSample.Category.ISSUES
        ).count()
        run_issues_scan(scan, client)
        second = Finding.objects.filter(
            scan=scan, category=MetricSample.Category.ISSUES
        ).count()
        self.assertEqual(first, second)

    def test_run_returns_pk(self):
        scan = self._scan()
        with patch("health.issues_scan.SourceCraftClient") as client_cls:
            client = client_cls.return_value
            client.get_issues.return_value = []
            client.get_issue_events.return_value = []
            pk = run(scan.pk)
        self.assertEqual(pk, HealthScore.objects.get(pk=pk).pk)
