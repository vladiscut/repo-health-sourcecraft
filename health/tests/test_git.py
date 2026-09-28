from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from git import GitCommandError, Repo

from integrations.git import SourceCraftGitClient
from integrations.sourcecraft import SourceCraftError


def _init_repo(path: Path) -> Repo:
    """Создаёт непустой git-репозиторий с двумя коммитами."""
    repo = Repo.init(path)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "tester")
        cfg.set_value("user", "email", "tester@example.com")

    tracked = path / "file.txt"
    tracked.write_text("first\n", encoding="utf-8")
    repo.index.add(["file.txt"])
    repo.index.commit("first commit")

    tracked.write_text("first\nsecond\n", encoding="utf-8")
    repo.index.add(["file.txt"])
    repo.index.commit("second commit")
    return repo


class GetCommitHistoryTests(SimpleTestCase):
    def test_returns_commit_dates_from_scan_repo_dir(self):
        with TemporaryDirectory() as tmp:
            scan_dir = Path(tmp) / "1"
            scan_dir.mkdir()
            repo = _init_repo(scan_dir)
            repo.close()
            expected = [
                c.committed_datetime
                for c in Repo(scan_dir).iter_commits()
            ]

            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                dates = client.get_commit_history(scan_id=1)

            self.assertEqual(len(dates), len(expected))
            for value in dates:
                self.assertIsInstance(value, datetime)

    def test_raises_when_clone_dir_is_not_a_repo(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "1").mkdir()
            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                with self.assertRaises(SourceCraftError):
                    client.get_commit_history(scan_id=1)


class ScanMarkersTests(SimpleTestCase):
    def test_returns_marker_lines(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            repo = _init_repo(path)
            tracked = path / "file.txt"
            tracked.write_text("TODO: x\nFIXME: y\nok\n", encoding="utf-8")
            repo.index.add(["file.txt"])
            repo.index.commit("add markers")
            repo.close()

            lines = SourceCraftGitClient.scan_markers(str(path))

            self.assertTrue(any("TODO" in line for line in lines))
            self.assertTrue(any("FIXME" in line for line in lines))
            self.assertTrue(all("file.txt" in line for line in lines))

    def test_returns_empty_when_no_markers(self):
        with TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            repo.close()

            lines = SourceCraftGitClient.scan_markers(str(tmp))

            self.assertEqual(lines, [])

    def test_raises_on_invalid_repo(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SourceCraftError):
                SourceCraftGitClient.scan_markers(tmp)

class ScanMarkerPositionsTests(SimpleTestCase):
    def test_returns_tuples_with_path_line_and_marker(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            repo = _init_repo(path)
            tracked = path / "file.txt"
            tracked.write_text("TODO: x\nFIXME: y\nok\n", encoding="utf-8")
            repo.index.add(["file.txt"])
            repo.index.commit("add markers")
            repo.close()

            positions = SourceCraftGitClient.scan_marker_positions(str(path))

            self.assertIn(("file.txt", 1, "TODO"), positions)
            self.assertIn(("file.txt", 2, "FIXME"), positions)

    def test_returns_empty_when_no_markers(self):
        with TemporaryDirectory() as tmp:
            repo = _init_repo(Path(tmp))
            repo.close()

            positions = SourceCraftGitClient.scan_marker_positions(str(tmp))

            self.assertEqual(positions, [])

    def test_raises_on_invalid_repo(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SourceCraftError):
                SourceCraftGitClient.scan_marker_positions(tmp)


class GetLineCommitDatesTests(SimpleTestCase):
    def test_empty_line_specs_returns_empty(self):
        client = SourceCraftGitClient(token="dummy")
        with TemporaryDirectory() as tmp:
            with override_settings(SCAN_REPO_DIR=tmp):
                self.assertEqual(client.get_line_commit_dates(1, "main", {}), {})

    def test_reads_from_existing_clone(self):
        with TemporaryDirectory() as tmp:
            scan_dir = Path(tmp) / "1"
            scan_dir.mkdir()
            repo = _init_repo(scan_dir)
            repo.close()

            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                dates = client.get_line_commit_dates(
                    1, "HEAD", {"file.txt": [1]}
                )

            self.assertIn("file.txt", dates)
            self.assertIn(1, dates["file.txt"])
            self.assertIsInstance(dates["file.txt"][1], datetime)

    def test_raises_on_missing_clone(self):
        with TemporaryDirectory() as tmp:
            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                with self.assertRaises(SourceCraftError):
                    client.get_line_commit_dates(1, "HEAD", {"file.txt": [1]})

    def test_blame_lines_parses_porcelain(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            repo = _init_repo(path)
            repo.close()
            r = Repo(path)
            try:
                dates = SourceCraftGitClient._blame_lines(
                    r, "HEAD", "file.txt", [1, 2]
                )
            finally:
                r.close()

            self.assertEqual(set(dates), {1, 2})
            for value in dates.values():
                self.assertIsInstance(value, datetime)


class GetMarkerCommitDatesTests(SimpleTestCase):
    def test_empty_when_no_markers(self):
        with TemporaryDirectory() as tmp:
            scan_dir = Path(tmp) / "1"
            scan_dir.mkdir()
            repo = _init_repo(scan_dir)
            repo.close()

            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                result = client.get_marker_commit_dates(1, "HEAD")

            self.assertEqual(result, {})

    def test_returns_marker_with_commit_date(self):
        with TemporaryDirectory() as tmp:
            scan_dir = Path(tmp) / "1"
            scan_dir.mkdir()
            repo = _init_repo(scan_dir)
            tracked = scan_dir / "file.txt"
            tracked.write_text("TODO: x\nFIXME: y\nok\n", encoding="utf-8")
            repo.index.add(["file.txt"])
            repo.index.commit("add markers")
            repo.close()

            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                result = client.get_marker_commit_dates(1, "HEAD")

            self.assertIn(("file.txt", 1), result)
            marker, committed = result[("file.txt", 1)]
            self.assertEqual(marker, "TODO")
            self.assertIsInstance(committed, datetime)
            self.assertIn(("file.txt", 2), result)
            self.assertEqual(result[("file.txt", 2)][0], "FIXME")

    def test_raises_on_missing_clone(self):
        with TemporaryDirectory() as tmp:
            with override_settings(SCAN_REPO_DIR=tmp):
                client = SourceCraftGitClient(token="dummy")
                with self.assertRaises(SourceCraftError):
                    client.get_marker_commit_dates(1, "HEAD")


class CloneFailureTests(SimpleTestCase):
    def test_missing_base_url_raises_before_git(self):
        client = SourceCraftGitClient(token="dummy", semaphore=MagicMock())
        with override_settings(SOURCECRAFT_GIT_BASE_URL=""):
            with self.assertRaises(SourceCraftError) as ctx:
                client.clone("org", "repo", "main", 1)
        self.assertIn("SOURCECRAFT_GIT_BASE_URL", str(ctx.exception))

    def test_git_error_is_not_masked_by_close(self):
        semaphore = MagicMock()
        client = SourceCraftGitClient(token="dummy", semaphore=semaphore)
        askpass = MagicMock()
        with TemporaryDirectory() as tmp, override_settings(
            SOURCECRAFT_GIT_BASE_URL="https://git.example",
            SCAN_REPO_DIR=tmp,
        ), patch(
            "integrations.git._build_git_env",
            return_value=({}, askpass),
        ), patch(
            "integrations.git.Repo.clone_from",
            side_effect=GitCommandError(["git", "clone"], 128, stderr="fatal"),
        ):
            with self.assertRaises(SourceCraftError) as ctx:
                client.clone("org", "repo", "main", 1)

        self.assertIsInstance(ctx.exception.__cause__, GitCommandError)
        askpass.unlink.assert_called_once()
