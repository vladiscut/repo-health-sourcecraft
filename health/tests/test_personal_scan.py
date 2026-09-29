from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from health.access_check import (
    TOKEN_MISSING_TEXT,
    run_personal_access_check,
)
from health.models import Scan, UserRepositoryAccess
from health.orchestrator import fix_stale_scans
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
            default_branch="main",
            is_empty=False,
        )
        self.access = grant_access(self.user, self.repo)
        self.access.status = UserRepositoryAccess.Status.CHECKING
        self.access.checking_since = timezone.now()
        self.access.save(update_fields=["status", "checking_since"])

    @patch("health.orchestrator.check_and_scan_repository")
    @patch("health.access_check.SourceCraftClient")
    def test_access_granted_starts_scan_without_placeholder(self, client_cls, start):
        client_cls.return_value.get_repository.return_value = {"id": "private-1"}

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "ok")
        self.assertFalse(Scan.objects.filter(repository=self.repo).exists())
        start.assert_called_once_with(
            self.repo.id,
            force=True,
            user_id=self.user.id,
        )
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, UserRepositoryAccess.Status.GRANTED)
        self.assertEqual(self.access.check_error, "")
        client_cls.assert_called_once_with(token="pat-ok")

    @patch("health.orchestrator.chain", return_value=Mock())
    @patch("health.orchestrator._get_current_commit_hash", return_value="abc")
    @patch("health.access_check.SourceCraftClient")
    def test_granted_access_creates_scan_in_orchestrator(
        self, client_cls, _commit_hash, _chain
    ):
        client_cls.return_value.get_repository.return_value = {"id": "private-1"}

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "ok")
        scan = Scan.objects.get(repository=self.repo)
        self.assertEqual(scan.status, Scan.Status.RUNNING)
        self.assertEqual(scan.triggered_by, Scan.TriggeredBy.USER)
        self.assertEqual(scan.triggered_by_user_id, self.user.id)
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, UserRepositoryAccess.Status.GRANTED)

    @patch("health.access_check.SourceCraftClient")
    def test_denied_access_drops_row_and_does_not_create_scan(self, client_cls):
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "forbidden",
            status_code=403,
        )

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "denied")
        self.assertFalse(Scan.objects.filter(repository=self.repo).exists())
        self.assertFalse(
            UserRepositoryAccess.objects.filter(
                user=self.user,
                repository=self.repo,
            ).exists()
        )

    @patch("health.access_check.SourceCraftClient")
    def test_missing_token_keeps_access_and_explains(self, client_cls):
        self.user.profile.sourcecraft_pat = ""
        self.user.profile.save(update_fields=["sourcecraft_pat"])

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "denied")
        client_cls.assert_not_called()
        self.assertFalse(Scan.objects.filter(repository=self.repo).exists())
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, UserRepositoryAccess.Status.GRANTED)
        self.assertEqual(self.access.check_error, TOKEN_MISSING_TEXT)

    @patch("health.access_check.SourceCraftClient")
    def test_outage_leaves_access_checking(self, client_cls):
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "unavailable",
            status_code=503,
        )

        outcome = run_personal_access_check(self.repo.id, self.user.id)

        self.assertEqual(outcome, "unavailable")
        self.assertFalse(Scan.objects.filter(repository=self.repo).exists())
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, UserRepositoryAccess.Status.CHECKING)
        self.assertEqual(self.access.check_error, "")

    def test_stale_access_check_is_released(self):
        self.access.checking_since = timezone.now() - timedelta(days=2)
        self.access.save(update_fields=["checking_since"])

        fix_stale_scans()

        self.access.refresh_from_db()
        self.assertEqual(self.access.status, UserRepositoryAccess.Status.GRANTED)
        self.assertIn("зависла", self.access.check_error)
