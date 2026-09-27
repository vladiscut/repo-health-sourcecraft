from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from health.models import Finding, HealthScore, MetricSample, Scan
from health.security_scan import run
from health.tests.helpers import make_profile, make_repo
from integrations.appsec import AppSecClientError
from integrations.sourcecraft import SourceCraftError


FINISHED = {
    "uuid": "01890f3e-7b5c-7cc2-bc6f-3f5d8c9a1e4a",
    "status": "FINISHED",
    "totalDefectGroups": 0,
}


def _group(**overrides) -> dict:
    data = {
        "uuid": "group-1",
        "severity": "critical",
        "scanner": "SCA",
        "status": "open",
        "transitive": False,
        "url": "https://appsec.sourcecraft.tech/groups/group-1",
    }
    data.update(overrides)
    return data


class SecurityScanTests(TestCase):
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

    def _client(self, latest, groups=None, latest_error=None, groups_error=None) -> Mock:
        client = Mock()

        def get_latest(_repo_id):
            if latest_error is not None:
                raise latest_error
            return latest

        def list_groups(_repo_id, scan_uuid=None):
            if groups_error is not None:
                raise groups_error
            return groups or []

        client.get_latest_scan.side_effect = get_latest
        client.list_defect_groups.side_effect = list_groups
        return client

    def test_finished_without_groups_is_a_high_score(self):
        scan = self._scan(self.user)
        client = self._client(FINISHED, groups=[])

        with patch("health.security_scan.AppSecClient", return_value=client):
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        sample = MetricSample.objects.get(
            scan=scan,
            metric_key="security_open_critical_count",
        )
        scan.refresh_from_db()

        self.assertEqual(score.total, 100)
        self.assertTrue(sample.is_available)
        self.assertEqual(sample.value, 0)
        self.assertEqual(scan.status, Scan.Status.RUNNING)
        client.list_defect_groups.assert_called_once_with("repo-1", scan_uuid=FINISHED["uuid"])

    def test_missing_scan_is_null_and_skips_groups(self):
        scan = self._scan(self.user)
        client = self._client(None)

        with patch("health.security_scan.AppSecClient", return_value=client):
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        sample = MetricSample.objects.get(scan=scan, metric_key="_category_unavailable")

        self.assertIsNone(score.total)
        self.assertFalse(sample.is_available)
        self.assertIn("нет скана", sample.error_reason)
        client.list_defect_groups.assert_not_called()

    def test_forbidden_latest_is_null_with_reason(self):
        scan = self._scan(self.user)
        client = self._client(
            None,
            latest_error=AppSecClientError("forbidden", status_code=403),
        )

        with patch("health.security_scan.AppSecClient", return_value=client):
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        sample = MetricSample.objects.get(scan=scan, metric_key="_category_unavailable")

        self.assertIsNone(score.total)
        self.assertIn("forbidden", sample.error_reason)
        client.list_defect_groups.assert_not_called()

    def test_missing_token_is_null_and_skips_client(self):
        scan = self._scan()

        with patch("health.security_scan.AppSecClient") as client_cls:
            score_id = run(scan.pk)

        score = HealthScore.objects.get(pk=score_id)
        self.assertIsNone(score.total)
        client_cls.assert_not_called()

    def test_transitive_critical_lowers_the_score(self):
        clean = self._scan(self.user)
        other = make_repo(
            org_slug="acme",
            repo_slug="demo-red",
            sourcecraft_id="repo-2",
            default_branch="main",
        )
        dirty = Scan.objects.create(
            repository=other,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.USER,
            triggered_by_user=self.user,
        )
        transitive = _group(uuid="tr-1", transitive=True, scanner="SCA", severity="critical")

        with patch(
            "health.security_scan.AppSecClient",
            return_value=self._client(FINISHED, groups=[]),
        ):
            clean_id = run(clean.pk)
        with patch(
            "health.security_scan.AppSecClient",
            return_value=self._client(FINISHED, groups=[transitive]),
        ):
            dirty_id = run(dirty.pk)

        clean_score = HealthScore.objects.get(pk=clean_id).total
        dirty_score = HealthScore.objects.get(pk=dirty_id).total
        finding = Finding.objects.get(scan=dirty, category=MetricSample.Category.SECURITY)

        self.assertGreaterEqual(clean_score - dirty_score, 15)
        self.assertEqual(finding.evidence_refs, ["https://appsec.sourcecraft.tech/groups/group-1"])

    def test_rate_limit_is_not_stored_as_missing_data(self):
        scan = self._scan(self.user)
        client = self._client(
            None,
            latest_error=SourceCraftError("slow down", status_code=429),
        )

        with patch("health.security_scan.AppSecClient", return_value=client):
            with self.assertRaises(SourceCraftError):
                run(scan.pk)

        self.assertFalse(HealthScore.objects.filter(scan=scan).exists())
