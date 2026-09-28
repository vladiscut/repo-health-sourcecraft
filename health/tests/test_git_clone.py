"""Unit-тесты для :mod:`health.git_clone`."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from health.git_clone import run
from health.models import Scan
from health.tests.helpers import make_profile, make_repo
from integrations.sourcecraft import SourceCraftError


class GitCloneRunTests(TestCase):
    def _scan(self, user=None):
        repo = make_repo(default_branch="main")
        return Scan.objects.create(
            repository=repo,
            triggered_by_user=user,
        )

    def test_clone_with_user_token(self):
        user = get_user_model().objects.create_user("owner", password="x")
        make_profile(user, sourcecraft_pat="user-pat-123")
        scan = self._scan(user)

        with patch("health.git_clone.SourceCraftGitClient") as client_cls:
            client = client_cls.return_value
            run(scan.pk)

        client_cls.assert_called_once_with(token="user-pat-123")
        client.clone.assert_called_once_with(
            scan.repository.org_slug,
            scan.repository.repo_slug,
            scan.repository.default_branch,
            scan.pk,
        )

    def test_no_clone_without_user(self):
        scan = self._scan(user=None)

        with patch("health.git_clone.SourceCraftGitClient") as client_cls:
            run(scan.pk)

        client_cls.assert_not_called()

    def test_missing_scan_raises(self):
        with self.assertRaises(Scan.DoesNotExist):
            run(999999)

    def test_clone_error_does_not_abort_scan_chain(self):
        user = get_user_model().objects.create_user("owner2", password="x")
        make_profile(user, sourcecraft_pat="user-pat-123")
        scan = self._scan(user)

        with patch("health.git_clone.SourceCraftGitClient") as client_cls:
            client_cls.return_value.clone.side_effect = SourceCraftError("clone fail")
            run(scan.pk)
