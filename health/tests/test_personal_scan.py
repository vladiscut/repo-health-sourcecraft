from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from health.access_check import (
    ACCESS_DENIED_TEXT,
    run_personal_access_check,
)
from health.models import Scan, UserRepositoryAccess
from health.tests.helpers import grant_access, make_profile, make_repo
from integrations.sourcecraft import SourceCraftError


class PersonalAccessCheckTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("owner", password="x")
        make_profile(self.user, sourcecraft_pat="pat-ok")
        self.repo = make_repo(
            org_slug="hidden",
            repo_slug="secret",
            visibility="private",
            sourcecraft_id="private-1",
        )
        grant_access(self.user, self.repo)
        self.scan = Scan.objects.create(
            repository=self.repo,
            status=Scan.Status.CHECKING,
            triggered_by=Scan.TriggeredBy.USER,
            triggered_by_user=self.user,
        )

    @patch("health.orchestrator.check_and_scan_repository")
    @patch("health.access_check.SourceCraftClient")
    def test_access_granted_moves_scan_to_queue(self, client_cls, start):
        client_cls.return_value.get_repository.return_value = {"id": "private-1"}

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "ok")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Scan.Status.PENDING)
        start.assert_called_once_with(
            self.repo.id,
            force=True,
            user_id=self.user.id,
        )
        client_cls.assert_called_once_with(token="pat-ok")

    @patch("health.access_check.SourceCraftClient")
    def test_denied_access_fails_scan_and_drops_row(self, client_cls):
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "forbidden",
            status_code=403,
        )

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "denied")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Scan.Status.FAILED)
        self.assertEqual(self.scan.error, ACCESS_DENIED_TEXT)
        self.assertFalse(
            UserRepositoryAccess.objects.filter(
                user=self.user,
                repository=self.repo,
            ).exists()
        )

    @patch("health.access_check.SourceCraftClient")
    def test_outage_leaves_scan_checking(self, client_cls):
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "unavailable",
            status_code=503,
        )

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "unavailable")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Scan.Status.CHECKING)
        self.assertTrue(
            UserRepositoryAccess.objects.filter(
                user=self.user,
                repository=self.repo,
            ).exists()
        )
