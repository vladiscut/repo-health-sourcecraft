"""Тесты очистки каталогов клонов: post_save-сигнал и сборщик."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from health.models import Scan
from health.orchestrator import fix_stale_scans
from health.tests.helpers import make_repo


class ScanCloneCleanupSignalTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _patch_root(self):
        def fake_dir(scan_id):
            return Path(self.tmp, str(scan_id))

        return patch("health.signals.get_scan_repo_dir", side_effect=fake_dir)

    def test_success_deletes_clone_dir(self):
        repo = make_repo()
        scan = Scan.objects.create(repository=repo)
        clone_dir = Path(self.tmp, str(scan.id))
        clone_dir.mkdir(parents=True, exist_ok=True)
        (clone_dir / "file.txt").write_text("data")

        with self._patch_root():
            scan.status = Scan.Status.SUCCESS
            scan.save(update_fields=["status"])

        self.assertFalse(clone_dir.exists())

    def test_failed_deletes_clone_dir(self):
        repo = make_repo()
        scan = Scan.objects.create(repository=repo)
        clone_dir = Path(self.tmp, str(scan.id))
        clone_dir.mkdir(parents=True, exist_ok=True)

        with self._patch_root():
            scan.status = Scan.Status.FAILED
            scan.save(update_fields=["status"])

        self.assertFalse(clone_dir.exists())

    def test_running_keeps_clone_dir(self):
        repo = make_repo()
        scan = Scan.objects.create(repository=repo)
        clone_dir = Path(self.tmp, str(scan.id))
        clone_dir.mkdir(parents=True, exist_ok=True)

        with self._patch_root():
            scan.status = Scan.Status.RUNNING
            scan.save(update_fields=["status"])

        self.assertTrue(clone_dir.exists())

    def test_missing_dir_is_tolerated(self):
        repo = make_repo()
        scan = Scan.objects.create(repository=repo)
        # Каталога нет — сигнал не должен падать.
        with self._patch_root():
            scan.status = Scan.Status.SUCCESS
            scan.save(update_fields=["status"])


class ReapOrphanCloneDirsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_removes_orphan_and_terminal(self):
        repo = make_repo()
        terminal = Scan.objects.create(repository=repo, status=Scan.Status.SUCCESS)
        running = Scan.objects.create(repository=repo, status=Scan.Status.RUNNING)

        orphan_dir = Path(self.tmp, "999999")
        orphan_dir.mkdir(parents=True, exist_ok=True)
        terminal_dir = Path(self.tmp, str(terminal.id))
        terminal_dir.mkdir(parents=True, exist_ok=True)
        running_dir = Path(self.tmp, str(running.id))
        running_dir.mkdir(parents=True, exist_ok=True)

        with patch("health.orchestrator.settings.SCAN_REPO_DIR", self.tmp):
            fix_stale_scans()

        self.assertFalse(orphan_dir.exists())
        self.assertFalse(terminal_dir.exists())
        # Не-терминальный скан не трогаем.
        self.assertTrue(running_dir.exists())

    def test_ignores_non_numeric_dirs(self):
        weird = Path(self.tmp, "not-an-id")
        weird.mkdir(parents=True, exist_ok=True)

        with patch("health.orchestrator.settings.SCAN_REPO_DIR", self.tmp):
            fix_stale_scans()

        self.assertTrue(weird.exists())
