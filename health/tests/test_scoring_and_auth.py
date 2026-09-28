from django.test import SimpleTestCase, override_settings

from health.docs_scan import _DocsMetrics
from health.models import MetricSample
from health.scoring import (
    CATEGORY_WEIGHTS,
    DOCS_SUBMETRIC_WEIGHTS,
    SUBMETRIC_WEIGHTS_BY_CATEGORY,
    _weighted_overall,
    compute_finding_impact,
    find_impact,
    impact_relation,
    impacts_relation,
    overall_from_category_totals,
    present_scores,
    score_docs_category,
    score_level,
)
from health.formatters import format_percentile, format_rating
from integrations.sourcecraft import SourceCraftClient
from integrations.yandex import build_authorization_url, is_configured, token_expires_at


class ScoringTests(SimpleTestCase):
    def test_overall_ignores_missing_categories(self):
        total = overall_from_category_totals(
            {
                MetricSample.Category.SECURITY: 100,
                MetricSample.Category.CODE_HEALTH: None,
            }
        )
        self.assertEqual(total, 100)

    def test_overall_none_when_empty(self):
        self.assertIsNone(overall_from_category_totals({}))

    def test_present_scores_level(self):
        presented = present_scores(
            {
                MetricSample.Category.SECURITY: 90,
                MetricSample.Category.CODE_HEALTH: 90,
                MetricSample.Category.ACTIVITY: 90,
                MetricSample.Category.DOCS: 90,
                MetricSample.Category.CI_CD: 90,
                MetricSample.Category.ISSUES: 90,
            }
        )
        self.assertEqual(presented["total"], 90)
        self.assertEqual(presented["level"], "ok")
        self.assertEqual(len(presented["categories"]), 6)

    def test_present_scores_keeps_missing_category(self):
        presented = present_scores({MetricSample.Category.DOCS: 0})
        by_name = dict(presented["categories"])
        self.assertEqual(by_name["Документация"], 0)
        self.assertIsNone(by_name["Security"])
        self.assertEqual(len(presented["categories"]), 6)
        self.assertEqual(score_level(40), "low")
        self.assertEqual(score_level(None), "")


class FindImpactTests(SimpleTestCase):
    """design.md D1: impact находки = баллы КАТЕГОРИИ, не общего Score."""

    def test_missing_submetric_is_zero(self):
        self.assertEqual(
            find_impact("contributing_codeowners", DOCS_SUBMETRIC_WEIGHTS, {}),
            0,
        )

    def test_unknown_submetric_key_is_zero(self):
        self.assertEqual(
            find_impact("not_a_submetric", DOCS_SUBMETRIC_WEIGHTS, {"x": 0.0}),
            0,
        )

    def test_weight_times_headroom(self):
        impact = find_impact(
            "contributing_codeowners",
            DOCS_SUBMETRIC_WEIGHTS,
            {"contributing_codeowners": 0.0},
        )
        self.assertEqual(impact, 15)

    def test_full_score_is_zero(self):
        impact = find_impact(
            "contributing_codeowners",
            DOCS_SUBMETRIC_WEIGHTS,
            {"contributing_codeowners": 100.0},
        )
        self.assertEqual(impact, 0)

    def test_partial_score_is_prorated(self):
        # 0.15 * (100 - 50) = 7.5 -> round -> 8
        impact = find_impact(
            "contributing_codeowners",
            DOCS_SUBMETRIC_WEIGHTS,
            {"contributing_codeowners": 50.0},
        )
        self.assertEqual(impact, 8)


class ImpactRelationTests(SimpleTestCase):
    """impact_relation: impact категории -> отношение [1..100] к баллу категории."""

    def test_no_category_total_is_zero(self):
        self.assertEqual(impact_relation(25, None), 0)

    def test_zero_impact_is_zero(self):
        self.assertEqual(impact_relation(0, 31), 0)

    def test_code_health_tests_finding(self):
        # реальные данные: code_health=31, impact 25 -> round(25/31*100)=81
        self.assertEqual(impact_relation(25, 31), 81)

    def test_code_health_junk_finding(self):
        # реальные данные: code_health=31, impact 7 -> round(7/31*100)=23
        self.assertEqual(impact_relation(7, 31), 23)

    def test_activity_finding(self):
        # реальные данные: activity=55, impact 25 -> round(25/55*100)=45
        self.assertEqual(impact_relation(25, 55), 45)

    def test_docs_finding(self):
        # реальные данные: docs=69, impact 15 -> round(15/69*100)=22
        self.assertEqual(impact_relation(15, 69), 22)

    def test_zero_category_total_is_hundred(self):
        # реальные данные: ci_cd=0, impact 10 -> категория мертва -> 100
        self.assertEqual(impact_relation(10, 0), 100)

    def test_ratio_is_capped_at_hundred(self):
        # impact 60 при total 30 -> 200% -> зажато до 100
        self.assertEqual(impact_relation(60, 30), 100)

    def test_small_impact_has_floor_of_one(self):
        # impact 1 при total 200 -> 0.5% -> round 1
        self.assertEqual(impact_relation(1, 200), 1)


class ImpactsRelationTests(SimpleTestCase):
    """impacts_relation: набор impact'ов -> доли [0..100] с суммой ровно 100."""

    def test_real_code_health_sums_to_hundred(self):
        # реальные данные: [25, 15, 15, 7], сумма 62
        # квоты: 40.32, 24.19, 24.19, 11.29 -> floors [40,24,24,11]=99,
        # остаток 1 уходит наибольшей дробной части (индекс 0) -> [41,24,24,11]
        result = impacts_relation([25, 15, 15, 7])
        self.assertEqual(sum(result), 100)
        self.assertEqual(result, [41, 24, 24, 11])

    def test_activity_single_finding_is_hundred(self):
        self.assertEqual(impacts_relation([25]), [100])

    def test_docs_pair_sums_to_hundred(self):
        # [15, 15] -> [50, 50]
        self.assertEqual(impacts_relation([15, 15]), [50, 50])

    def test_empty_is_empty(self):
        self.assertEqual(impacts_relation([]), [])

    def test_all_zero_is_zeros(self):
        self.assertEqual(impacts_relation([0, 0, 0]), [0, 0, 0])

    def test_negative_treated_as_zero(self):
        # [-5, 10] -> веса [0, 10] -> [0, 100]
        self.assertEqual(impacts_relation([-5, 10]), [0, 100])

    def test_rounding_remainder_distributed(self):
        # три равных веса: 100/3 = 33.33 -> [34, 33, 33] в сумме 100
        result = impacts_relation([1, 1, 1])
        self.assertEqual(sum(result), 100)
        self.assertEqual(sorted(result, reverse=True), [34, 33, 33])

    def test_order_preserved(self):
        # больший вес получает большую долю на своей позиции
        result = impacts_relation([7, 25, 15, 15])
        self.assertEqual(sum(result), 100)
        self.assertEqual(len(result), 4)
        self.assertGreater(result[1], result[0])


class SubmetricWeightsMapTests(SimpleTestCase):
    """Task 1.2: единая карта весов; сумма весов каждой категории = 1.0."""

    def _assert_sums_to_one(self, weights):
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_docs(self):
        self._assert_sums_to_one(
            SUBMETRIC_WEIGHTS_BY_CATEGORY[MetricSample.Category.DOCS]
        )

    def test_issues(self):
        self._assert_sums_to_one(
            SUBMETRIC_WEIGHTS_BY_CATEGORY[MetricSample.Category.ISSUES]
        )

    def test_code_health(self):
        self._assert_sums_to_one(
            SUBMETRIC_WEIGHTS_BY_CATEGORY[MetricSample.Category.CODE_HEALTH]
        )

    def test_cicd(self):
        self._assert_sums_to_one(
            SUBMETRIC_WEIGHTS_BY_CATEGORY[MetricSample.Category.CI_CD]
        )

    def test_activity_with_commits(self):
        activity = SUBMETRIC_WEIGHTS_BY_CATEGORY[MetricSample.Category.ACTIVITY]
        self._assert_sums_to_one(activity["with_commits"])

    def test_activity_without_commits(self):
        activity = SUBMETRIC_WEIGHTS_BY_CATEGORY[MetricSample.Category.ACTIVITY]
        self._assert_sums_to_one(activity["without_commits"])

    def test_category_weights_also_sum_to_one(self):
        self._assert_sums_to_one(CATEGORY_WEIGHTS)

    def test_map_covers_all_categories(self):
        self.assertEqual(
            set(SUBMETRIC_WEIGHTS_BY_CATEGORY),
            set(MetricSample.Category.values),
        )


class RatingFormatTests(SimpleTestCase):
    def test_rating_drops_trailing_zero_and_groups_thousands(self):
        self.assertEqual(format_rating(3208.0), "3\u00a0208")
        self.assertEqual(format_rating(119.5), "119,5")
        self.assertEqual(format_rating(61), "61")
        self.assertEqual(format_rating(0), "—")
        self.assertEqual(format_rating(None), "—")

    def test_percentile(self):
        self.assertEqual(format_percentile(1), "топ 1%")
        self.assertEqual(format_percentile(15), "топ 15%")
        self.assertEqual(format_percentile(0), "")


class YandexOAuthTests(SimpleTestCase):
    def test_not_configured_without_keys(self):
        with override_settings(YANDEX_CLIENT_ID="", YANDEX_CLIENT_SECRET=""):
            self.assertFalse(is_configured())

    def test_authorization_url_contains_pkce_and_state(self):
        url, state, verifier = build_authorization_url()
        self.assertIn("response_type=code", url)
        self.assertIn("code_challenge=", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn(state, url)
        self.assertGreater(len(verifier), 20)

    def test_token_expires_at_invalid(self):
        self.assertIsNone(token_expires_at("nope"))
        self.assertIsNotNone(token_expires_at(60))


class DocsScoringTests(SimpleTestCase):
    def test_docs_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(DOCS_SUBMETRIC_WEIGHTS.values()), 1.0)

    def _full_metrics(self):
        return _DocsMetrics(
            readme_present=True,
            readme_size_chars=5000,
            readme_has_local_run=True,
            readme_has_build_test=True,
            readme_has_structure=True,
            license_present=True,
            license_type_recognized=True,
            contributing_present=True,
            codeowners_present=True,
            changelog_present=True,
            docs_dir_present=True,
            issue_templates_present=True,
            pr_template_present=True,
        )

    def test_all_six_submetrics_present_and_complete(self):
        score, completeness, submetrics = score_docs_category(self._full_metrics())
        self.assertEqual(
            set(submetrics),
            {
                "readme_quality",
                "license",
                "local_run",
                "build_test",
                "contributing_codeowners",
                "structure_extras",
            },
        )
        self.assertEqual(completeness, 1.0)
        self.assertIsNotNone(score)

    def test_missing_submetric_omitted_not_zeroed(self):
        # Не читаем README -> нет local_run/build_test и, если размер
        # неизвестен, нет readme_quality. Остальные данные на месте.
        metrics = _DocsMetrics(
            readme_present=True,
            readme_size_chars=None,
            license_present=True,
            license_type_recognized=True,
            contributing_present=True,
            codeowners_present=True,
            changelog_present=True,
            docs_dir_present=True,
            issue_templates_present=True,
            pr_template_present=True,
        )
        _, completeness, submetrics = score_docs_category(metrics)
        self.assertNotIn("readme_quality", submetrics)
        self.assertNotIn("local_run", submetrics)
        self.assertNotIn("build_test", submetrics)
        # Пропущенные веса: readme_quality .30 + local_run .10 + build_test .10
        self.assertAlmostEqual(completeness, 0.5)


class SourceCraftClientTests(SimpleTestCase):
    @override_settings(SOURCECRAFT_API_TOKEN="app-token")
    def test_none_token_uses_settings_but_empty_does_not(self):
        self.assertEqual(SourceCraftClient(token=None).access_token, "app-token")
        self.assertEqual(SourceCraftClient(token="").access_token, "")
        self.assertEqual(SourceCraftClient(token="user-pat").access_token, "user-pat")


class WeightedOverallTests(SimpleTestCase):
    def test_matches_overall_from_category_totals_with_same_weights(self):
        totals = {
            MetricSample.Category.SECURITY: 80,
            MetricSample.Category.CODE_HEALTH: 40,
            MetricSample.Category.ACTIVITY: 60,
            MetricSample.Category.DOCS: 20,
            MetricSample.Category.CI_CD: 100,
            MetricSample.Category.ISSUES: 0,
        }
        self.assertEqual(
            _weighted_overall(totals, CATEGORY_WEIGHTS),
            overall_from_category_totals(totals),
        )

    def test_none_when_no_usable_categories(self):
        self.assertIsNone(_weighted_overall({}, CATEGORY_WEIGHTS))
        self.assertIsNone(
            _weighted_overall(
                {MetricSample.Category.DOCS: None}, CATEGORY_WEIGHTS
            )
        )


class FindingImpactTests(SimpleTestCase):
    # Веса и баллы подобраны так, чтобы дельты были целыми.
    _cat = MetricSample.Category.DOCS

    def test_fixing_submetric_raises_overall(self):
        impact = compute_finding_impact(
            category=self._cat,
            submetric_scores={"readme_quality": 0.0},
            submetric_weights={"readme_quality": 1.0},
            all_category_totals={self._cat: 0},
            renormalized_category_weights={self._cat: 1.0},
            fixed_submetrics={"readme_quality": 50.0},
        )
        self.assertEqual(impact, 50)

    def test_impact_respects_renormalized_weights(self):
        impact = compute_finding_impact(
            category=self._cat,
            submetric_scores={"readme_quality": 0.0},
            submetric_weights={"readme_quality": 1.0},
            all_category_totals={
                self._cat: 0,
                MetricSample.Category.SECURITY: 100,
            },
            renormalized_category_weights={
                self._cat: 0.25,
                MetricSample.Category.SECURITY: 0.75,
            },
            fixed_submetrics={"readme_quality": 100.0},
        )
        # overall = 0.25*0 + 0.75*100 = 75 -> 0.25*100 + 0.75*100 = 100
        self.assertEqual(impact, 25)

    def test_impact_is_zero_when_no_growth(self):
        impact = compute_finding_impact(
            category=self._cat,
            submetric_scores={"readme_quality": 100.0},
            submetric_weights={"readme_quality": 1.0},
            all_category_totals={self._cat: 100},
            renormalized_category_weights={self._cat: 1.0},
            fixed_submetrics={"readme_quality": 100.0},
        )
        self.assertEqual(impact, 0)

    def test_impact_bounded_by_headroom(self):
        impact = compute_finding_impact(
            category=self._cat,
            submetric_scores={"readme_quality": 0.0},
            submetric_weights={"readme_quality": 1.0},
            all_category_totals={
                self._cat: 0,
                MetricSample.Category.SECURITY: 90,
            },
            renormalized_category_weights={
                self._cat: 0.5,
                MetricSample.Category.SECURITY: 0.5,
            },
            fixed_submetrics={"readme_quality": 100.0},
        )
        # current overall = 45; headroom = 55; raw gain = 50 -> 50 <= 55
        self.assertEqual(impact, 50)

    def test_impact_none_without_category_total(self):
        impact = compute_finding_impact(
            category=self._cat,
            submetric_scores={"readme_quality": 0.0},
            submetric_weights={"readme_quality": 1.0},
            all_category_totals={self._cat: None},
            renormalized_category_weights={self._cat: 1.0},
            fixed_submetrics={"readme_quality": 100.0},
        )
        self.assertIsNone(impact)
