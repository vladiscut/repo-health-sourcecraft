from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from health.cicd_scan import run
from health.models import Finding, HealthScore, MetricSample, Scan
from health.tests.helpers import make_profile, make_repo
from integrations.sourcecraft import SourceCraftError


def _tree(*paths: str) -> list[dict]:
    return [{"path": path, "type": "file"} for path in paths]


def _run(status: str, slug: str, started, finished) -> dict:
    return {
        "slug": slug,
        "status": status,
        "dates": {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
        },
    }


class CicdScanTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ivan", password="x")
        make_profile(self.user, sourcecraft_pat="pat-secret")
        self.repo = make_repo(
            org_slug="acme",
            repo_slug="demo",
            sourcecraft_id="repo-1",
            default_branch="main",
        )

    def _scan(self, user=None) -> Scan:
        return Scan.objects.create(
            repository=self.repo,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.USER if user else Scan.TriggeredBy.SCHEDULE,
            triggered_by_user=user,
        )

    def _client(self, tree, runs=None, runs_error=None) -> Mock:
        client = Mock()
        client.get_repository_file_tree.return_value = tree

        def pipelines(_repo_id):
            if runs_error is not None:
                raise runs_error
            return runs or []

        client.get_ci_pipelines.side_effect = pipelines
        return client

    def test_missing_config_is_a_low_score_not_missing_data(self):
        scan = self._scan(self.user)
        client = self._client(_tree("README.md"))

        with patch("health.cicd_scan.SourceCraftClient", return_value=client):
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        config = MetricSample.objects.get(
            scan=scan,
            category=MetricSample.Category.CI_CD,
            metric_key="ci_config_present",
        )
        scan.refresh_from_db()

        self.assertIsNotNone(score.total)
        self.assertEqual(score.total, 15)
        self.assertTrue(config.is_available)
        self.assertFalse(config.value)
        self.assertEqual(scan.status, Scan.Status.RUNNING)
        client.get_ci_pipelines.assert_not_called()
        self.assertTrue(
            Finding.objects.filter(
                scan=scan,
                category=MetricSample.Category.CI_CD,
                evidence_refs=[".sourcecraft/ci.yaml"],
            ).exists()
        )

    def test_missing_token_is_null_and_skips_http(self):
        scan = self._scan()

        with patch("health.cicd_scan.SourceCraftClient") as client_cls:
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        sample = MetricSample.objects.get(
            scan=scan,
            category=MetricSample.Category.CI_CD,
            metric_key="_category_unavailable",
        )

        self.assertIsNone(score.total)
        self.assertFalse(sample.is_available)
        client_cls.assert_not_called()

    def test_failure_share_lowers_the_score(self):
        finished = timezone.now()
        started = finished - timedelta(minutes=5)
        green_runs = [
            _run("success", f"green-{index}", started, finished) for index in range(9)
        ]
        green_runs.append(_run("failed", "red-1", started, finished))
        red_runs = [
            _run("success" if index < 5 else "failed", f"run-{index}", started, finished)
            for index in range(10)
        ]

        green = self._scan(self.user)
        other = make_repo(
            org_slug="acme",
            repo_slug="demo-red",
            sourcecraft_id="repo-2",
            default_branch="main",
        )
        red = Scan.objects.create(
            repository=other,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.USER,
            triggered_by_user=self.user,
        )
        tree = _tree(".sourcecraft/ci.yaml", "README.md")

        with patch("health.cicd_scan.SourceCraftClient", return_value=self._client(tree, green_runs)):
            green_id = run(green.pk)
        with patch("health.cicd_scan.SourceCraftClient", return_value=self._client(tree, red_runs)):
            red_id = run(red.pk)

        green_score = HealthScore.objects.get(pk=green_id).total
        red_score = HealthScore.objects.get(pk=red_id).total
        red_finding = Finding.objects.get(scan=red, category=MetricSample.Category.CI_CD)

        self.assertGreaterEqual(green_score - red_score, 25)
        self.assertIn("/repos/acme/demo-red/cicd/runs/", red_finding.evidence_refs[0])

    def test_runs_not_found_keeps_config_score(self):
        scan = self._scan(self.user)
        client = self._client(
            _tree(".sourcecraft/ci.yaml"),
            runs_error=SourceCraftError("missing", status_code=404),
        )

        with patch("health.cicd_scan.SourceCraftClient", return_value=client):
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        rate = MetricSample.objects.get(scan=scan, metric_key="ci_success_rate")

        self.assertEqual(score.total, 40)
        self.assertFalse(rate.is_available)

    def test_forbidden_runs_are_missing_data(self):
        scan = self._scan(self.user)
        client = self._client(
            _tree(".sourcecraft/ci.yaml"),
            runs_error=SourceCraftError("forbidden", status_code=403),
        )

        with patch("health.cicd_scan.SourceCraftClient", return_value=client):
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        self.assertIsNone(score.total)
