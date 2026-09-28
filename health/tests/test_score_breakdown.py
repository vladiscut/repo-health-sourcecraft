from django.test import SimpleTestCase

from health.models import MetricSample
from health.score_breakdown import explain_category, rows_for_scores


class ScoreBreakdownTests(SimpleTestCase):
    def test_docs_parts_share_weight_and_facts(self):
        parts = explain_category(
            MetricSample.Category.DOCS,
            {
                "readme_present": True,
                "readme_size_chars": 3200,
                "license_present": False,
                "submetric_scores": {"license": 0, "readme_quality": 96.4},
            },
            58,
        )
        self.assertEqual(
            [(part["label"], part["score"], part["share"], part["detail"]) for part in parts],
            [
                ("README", 96, 60, "3200 символов"),
                ("Лицензия", 0, 40, "нет файла"),
            ],
        )

    def test_activity_with_commits_renormalizes_missing_part(self):
        parts = explain_category(
            MetricSample.Category.ACTIVITY,
            {
                "include_commits": True,
                "last_activity_age_days": 2,
                "contributors_count": 1,
                "merge_requests_30d": 0,
                "releases_30d": 0,
                "submetric_scores": {
                    "recent_activity": 100,
                    "contributors": 30,
                    "merge_requests": 0,
                    "releases": 0,
                },
            },
            43,
        )
        by_label = {part["label"]: part for part in parts}
        self.assertNotIn("Коммиты", by_label)
        self.assertEqual(sum(part["share"] for part in parts), 100)
        self.assertEqual(by_label["Свежесть"]["detail"], "2 дн. назад")
        self.assertEqual(by_label["Свежесть"]["share"], 41)
        self.assertEqual(by_label["Контрибьюторы"]["share"], 6)

    def test_activity_without_commits_uses_schedule_weights(self):
        parts = explain_category(
            MetricSample.Category.ACTIVITY,
            {
                "include_commits": False,
                "submetric_scores": {
                    "recent_activity": 100,
                    "contributors": 30,
                    "merge_requests": 0,
                    "releases": 0,
                },
            },
            41,
        )
        shares = {part["label"]: part["share"] for part in parts}
        self.assertEqual(shares["Свежесть"], 40)
        self.assertEqual(shares["Контрибьюторы"], 5)
        self.assertEqual(shares["Merge requests"], 30)
        self.assertEqual(shares["Релизы"], 25)

    def test_cicd_no_config_explains_low_score(self):
        parts = explain_category(
            MetricSample.Category.CI_CD,
            {"ci_config_present": False, "submetric_scores": {}},
            15,
        )
        self.assertEqual(parts, [{"label": "Нет .sourcecraft/ci.yaml", "score": None, "share": None, "detail": ""}])

    def test_cicd_runs_show_rate_and_duration(self):
        parts = explain_category(
            MetricSample.Category.CI_CD,
            {
                "ci_config_present": True,
                "success_rate": 0.5,
                "median_duration_minutes": 40,
                "submetric_scores": {"success_rate": 50, "duration": 20},
            },
            42,
        )
        self.assertEqual(parts[0]["label"], "Успешные прогоны")
        self.assertEqual(parts[0]["share"], 75)
        self.assertEqual(parts[0]["detail"], "50%")
        self.assertEqual(parts[1]["detail"], "40 мин")
        self.assertEqual(parts[1]["share"], 25)

    def test_security_penalties_and_clean_scan(self):
        hits = explain_category(
            MetricSample.Category.SECURITY,
            {
                "open_critical_count": 2,
                "transitive_critical_count": 1,
                "open_high_count": 1,
            },
            40,
        )
        self.assertEqual(
            [part["detail"] for part in hits],
            ["1 × −30", "1 × −20", "1 × −10"],
        )
        clean = explain_category(
            MetricSample.Category.SECURITY,
            {"open_critical_count": 0, "open_high_count": 0, "transitive_critical_count": 0},
            100,
        )
        self.assertEqual(clean[0]["label"], "Открытых критических и высоких нет")

    def test_missing_category_shows_reason(self):
        parts = explain_category(
            MetricSample.Category.SECURITY,
            {"reason": "Ответ 404 для https://appsec.sourcecraft.tech/v1/scans/latest"},
            None,
        )
        self.assertEqual(parts[0]["label"], "нет скана AppSec")
        issues = explain_category(
            MetricSample.Category.ISSUES,
            {"error": "в репозитории issues=0 — категория не сканировалась"},
            None,
        )
        self.assertIn("issues=0", issues[0]["label"])

    def test_legacy_numeric_finished_reason_is_plain(self):
        parts = explain_category(
            MetricSample.Category.SECURITY,
            {"reason": "скан AppSec не FINISHED: 1"},
            None,
        )
        self.assertIn("уже завершён", parts[0]["label"])
        self.assertNotIn("FINISHED", parts[0]["label"])

    def test_rows_keep_six_categories(self):
        class _Score:
            def __init__(self, category, total, raw):
                self.category = category
                self.total = total
                self.raw_metrics = raw

        rows = rows_for_scores(
            [
                _Score(MetricSample.Category.DOCS, 10, {"submetric_scores": {"license": 10}}),
            ]
        )
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["label"], "Документация")
        self.assertEqual(rows[0]["parts"][0]["label"], "Лицензия")
        self.assertIsNone(rows[2]["value"])
        self.assertEqual(rows[2]["label"], "Безопасность")
        self.assertEqual(rows[5]["label"], "Состояние кода")

    def test_code_health_parts_are_readable(self):
        parts = explain_category(
            MetricSample.Category.CODE_HEALTH,
            {
                "include_todo_age": True,
                "tests_present": True,
                "lint_config_present": True,
                "vendored_deps_present": False,
                "generated_artifacts_present": True,
                "binary_junk_present": False,
                "is_data_only_repo": False,
                "is_flat_dump": False,
                "dependency_manifest_present": True,
                "lockfile_present": True,
                "todo_total_count": 0,
                "todo_age_available": True,
                "submetric_scores": {
                    "tests_present": 100,
                    "lint_config_present": 100,
                    "no_committed_junk": 67,
                    "structure": 100,
                    "dependency_hygiene": 100,
                    "todo_debt": 100,
                },
            },
            92,
        )
        by_label = {part["label"]: part for part in parts}
        self.assertEqual(
            set(by_label),
            {
                "Тесты",
                "Линтер",
                "Чистота репозитория",
                "Структура кода",
                "Зависимости",
                "TODO и FIXME",
            },
        )
        self.assertEqual(by_label["Тесты"]["detail"], "есть")
        self.assertEqual(by_label["Чистота репозитория"]["detail"], "каталоги сборки")
        self.assertEqual(by_label["Чистота репозитория"]["score"], 67)
        self.assertEqual(by_label["TODO и FIXME"]["detail"], "нет")
        self.assertEqual(by_label["Зависимости"]["detail"], "манифест и lockfile")
        self.assertEqual(by_label["Структура кода"]["detail"], "код разложен по каталогам")
        self.assertEqual(sum(part["share"] for part in parts), 100)

    def test_security_submetrics_use_russian_labels(self):
        parts = explain_category(
            MetricSample.Category.SECURITY,
            {
                "open_critical_count": 2,
                "transitive_critical_count": 1,
                "open_high_count": 1,
                "submetric_scores": {"direct": 0, "transitive": 0, "high": 0},
            },
            40,
        )
        self.assertEqual(
            [part["label"] for part in parts],
            ["Прямые критические", "Транзитивные критические", "Высокие"],
        )
        self.assertEqual(parts[0]["detail"], "1 × −30")
        self.assertEqual(parts[2]["detail"], "1 × −10")

    def test_cicd_config_key_is_labeled(self):
        parts = explain_category(
            MetricSample.Category.CI_CD,
            {
                "ci_config_present": False,
                "submetric_scores": {"ci_config_present": 0},
            },
            0,
        )
        self.assertEqual(parts[0]["label"], "Конфигурация CI")
        self.assertEqual(parts[0]["detail"], "нет .sourcecraft/ci.yaml")

    def test_every_weighted_submetric_has_a_readable_label(self):
        from health.score_breakdown import SUBMETRIC_LABELS
        from health.scoring import (
            ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS,
            ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS,
            CICD_SUBMETRIC_WEIGHTS,
            CODE_HEALTH_SUBMETRIC_WEIGHTS,
            CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
            DOCS_SUBMETRIC_WEIGHTS,
            ISSUES_SUBMETRIC_WEIGHTS,
            SECURITY_SUBMETRIC_WEIGHTS,
        )

        keys = set()
        for weights in (
            DOCS_SUBMETRIC_WEIGHTS,
            ISSUES_SUBMETRIC_WEIGHTS,
            CODE_HEALTH_SUBMETRIC_WEIGHTS,
            CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
            CICD_SUBMETRIC_WEIGHTS,
            ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS,
            ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS,
            SECURITY_SUBMETRIC_WEIGHTS,
        ):
            keys.update(weights)
        for key in keys:
            label = SUBMETRIC_LABELS[key]
            self.assertNotEqual(label, key)
            self.assertNotRegex(label, r"^[a-z0-9_]+$")
