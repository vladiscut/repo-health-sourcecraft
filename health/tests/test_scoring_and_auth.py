from django.test import SimpleTestCase, override_settings

from health.models import MetricSample
from health.scoring import overall_from_category_totals, present_scores, score_level
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
        self.assertEqual(score_level(40), "low")
        self.assertEqual(score_level(None), "")


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


class SourceCraftClientTests(SimpleTestCase):
    @override_settings(SOURCECRAFT_API_TOKEN="app-token")
    def test_none_token_uses_settings_but_empty_does_not(self):
        self.assertEqual(SourceCraftClient(token=None).access_token, "app-token")
        self.assertEqual(SourceCraftClient(token="").access_token, "")
        self.assertEqual(SourceCraftClient(token="user-pat").access_token, "user-pat")
