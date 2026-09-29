from django.test import TestCase
from django.utils import timezone

from health.models import HealthScore, MetricSample, Repository, Scan
from health.repo_ordering import annotate_visible_score
from health.tests.helpers import make_repo


def _scan(repo, status, totals, completeness=None):
    scan = Scan.objects.create(
        repository=repo,
        status=status,
        triggered_by=Scan.TriggeredBy.SCHEDULE,
        finished_at=timezone.now(),
    )
    for category, total in totals.items():
        done = 1.0 if total is not None else 0.0
        if completeness and category in completeness:
            done = completeness[category]
        HealthScore.objects.create(
            scan=scan,
            category=category,
            total=total,
            weight_used=0.0,
            data_completeness=done,
        )
    return scan


class VisibleScoreTests(TestCase):
    def _ordered(self):
        return list(
            annotate_visible_score(Repository.objects.all())
            .order_by("-visible_score")
            .values_list("repo_slug", "visible_score")
        )

    def test_weights_completeness_and_ignores_older_and_partial(self):
        full = make_repo(org_slug="sort", repo_slug="full")
        thin = make_repo(org_slug="sort", repo_slug="thin")
        stale = make_repo(org_slug="sort", repo_slug="stale")
        covered = {
            MetricSample.Category.DOCS: 100,
            MetricSample.Category.ACTIVITY: 0,
            MetricSample.Category.ISSUES: 0,
            MetricSample.Category.CODE_HEALTH: 0,
        }
        _scan(full, Scan.Status.SUCCESS, covered)
        _scan(
            thin,
            Scan.Status.SUCCESS,
            covered,
            completeness={MetricSample.Category.DOCS: 0.1},
        )
        _scan(
            stale,
            Scan.Status.SUCCESS,
            {MetricSample.Category.DOCS: 100, MetricSample.Category.ACTIVITY: 100},
        )
        _scan(
            stale,
            Scan.Status.SUCCESS,
            {MetricSample.Category.ACTIVITY: 100},
        )
        _scan(
            stale,
            Scan.Status.PARTIAL,
            {
                MetricSample.Category.DOCS: 100,
                MetricSample.Category.ACTIVITY: 100,
                MetricSample.Category.ISSUES: 100,
                MetricSample.Category.CODE_HEALTH: 100,
            },
        )

        rows = dict(self._ordered())
        self.assertAlmostEqual(rows["full"], 100 * 0.15 / 0.65)
        self.assertAlmostEqual(rows["thin"], 1.5 / 0.515)
        self.assertIsNone(rows["stale"])
        self.assertGreater(rows["full"], rows["thin"])
