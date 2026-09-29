from django.test import TestCase
from django.utils import timezone

from health.models import Scan
from health.scan_display import scan_for_card
from health.tests.helpers import make_repo
from health.tests.test_views import _make_completed_scan


class ScanForCardTests(TestCase):
    def test_failed_falls_back_to_success(self):
        repo = make_repo()
        success = _make_completed_scan(repo)
        Scan.objects.create(
            repository=repo,
            status=Scan.Status.FAILED,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            finished_at=timezone.now(),
            error="timeout",
        )
        shown, notice = scan_for_card(repo)
        self.assertEqual(shown.id, success.id)
        self.assertIn("предыдущего успешного", notice)
        self.assertIn("timeout", notice)

    def test_partial_is_shown_as_is(self):
        repo = make_repo()
        partial = Scan.objects.create(
            repository=repo,
            status=Scan.Status.PARTIAL,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            finished_at=timezone.now(),
            error="Нет результата по категориям: docs",
        )
        shown, notice = scan_for_card(repo)
        self.assertEqual(shown.id, partial.id)
        self.assertIn("частичный", notice.lower())
