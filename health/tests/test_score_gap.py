from django.test import TestCase
from django.utils import timezone

from health.models import Finding, HealthScore, MetricSample, Scan
from health.orchestrator import _scale_finding_impacts
from health.tests.helpers import make_repo


class ScoreGapTests(TestCase):
    def test_missing_submetric_gets_a_finding_and_pluses_sum_to_100(self):
        repo = make_repo()
        scan = Scan.objects.create(
            repository=repo,
            status=Scan.Status.SUCCESS,
            finished_at=timezone.now(),
            raw={"health_score": 70},
        )
        HealthScore.objects.create(
            scan=scan,
            category=MetricSample.Category.ACTIVITY,
            total=0,
            weight_used=1.0,
            data_completeness=1.0,
            raw_metrics={
                "include_commits": False,
                "submetric_scores": {
                    "recent_activity": 0.0,
                    "merge_requests": 0.0,
                    "releases": 0.0,
                },
            },
        )
        by_category = {row.category: row for row in scan.scores.all()}
        _scale_finding_impacts(scan, by_category)

        findings = list(Finding.objects.filter(scan=scan))
        titles = {item.title for item in findings}
        self.assertIn("Активность репозитория снижается", titles)
        self.assertIn("Мало merge requests за последние 30 дней", titles)
        self.assertIn("Мало релизов за последние 30 дней", titles)
        self.assertEqual(sum(item.estimated_score_impact for item in findings), 100)

    def test_existing_finding_is_not_duplicated(self):
        repo = make_repo()
        scan = Scan.objects.create(
            repository=repo,
            status=Scan.Status.SUCCESS,
            finished_at=timezone.now(),
        )
        HealthScore.objects.create(
            scan=scan,
            category=MetricSample.Category.DOCS,
            total=85,
            weight_used=1.0,
            data_completeness=1.0,
            raw_metrics={
                "submetric_scores": {
                    "readme_quality": 100.0,
                    "license": 100.0,
                    "local_run": 100.0,
                    "build_test": 100.0,
                    "contributing_codeowners": 0.0,
                    "structure_extras": 100.0,
                },
            },
        )
        Finding.objects.create(
            scan=scan,
            category=MetricSample.Category.DOCS,
            severity=Finding.Severity.LOW,
            title="Нет CONTRIBUTING и CODEOWNERS",
            detail="Нет файлов.",
            recommendation="Добавьте файлы.",
            estimated_score_impact=15,
        )
        by_category = {row.category: row for row in scan.scores.all()}
        _scale_finding_impacts(scan, by_category)

        self.assertEqual(Finding.objects.filter(scan=scan).count(), 1)
        finding = Finding.objects.get(scan=scan)
        self.assertEqual(finding.estimated_score_impact, 15)
