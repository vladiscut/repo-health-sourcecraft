from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from health.ai_summary import generate_and_store, parse_summary
from health.models import Finding, MetricSample, Repository, Scan
from health.tests.helpers import make_repo


def _scan(repo: Repository, *, raw=None) -> Scan:
    return Scan.objects.create(
        repository=repo,
        status=Scan.Status.SUCCESS,
        triggered_by=Scan.TriggeredBy.SCHEDULE,
        finished_at=timezone.now(),
        raw=raw or {},
    )


def _finding(scan: Scan, title: str) -> Finding:
    return Finding.objects.create(
        scan=scan,
        category=MetricSample.Category.DOCS,
        severity=Finding.Severity.HIGH,
        title=title,
        recommendation="Допишите README.",
        estimated_score_impact=4,
    )


def _completion(text: str):
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = {
        "result": {
            "alternatives": [
                {"message": {"role": "assistant", "text": text}},
            ]
        }
    }
    return response


@override_settings(YANDEX_GPT_FOLDER_ID="b1test", YANDEX_GPT_API_KEY="secret")
class AiSummaryTests(TestCase):
    def test_parse_drops_unknown_finding_title(self):
        parsed = parse_summary(
            """```json
            {"summary":"Документация слабая.","steps":[
                {"finding_title":"Нет README","action":"Добавьте README."},
                {"finding_title":"Выдуманная проблема","action":"Сделайте что-нибудь."}
            ]}
            ```""",
            ["Нет README"],
        )
        self.assertEqual(parsed["summary"], "Документация слабая.")
        self.assertEqual(
            parsed["steps"],
            [{"finding_title": "Нет README", "action": "Добавьте README."}],
        )

    def test_timeout_keeps_score_and_raw(self):
        repo = make_repo(health_score=61)
        scan = _scan(repo, raw={"health_score": 61})
        finding = _finding(scan, "Нет README")
        with patch("health.ai_summary.requests.post", side_effect=requests.Timeout("slow")):
            saved = generate_and_store(
                scan,
                findings=[finding],
                categories=[("Документация", 40)],
                overall=61,
            )
        scan.refresh_from_db()
        repo.refresh_from_db()
        self.assertFalse(saved)
        self.assertNotIn("ai", scan.raw)
        self.assertEqual(scan.raw["health_score"], 61)
        self.assertEqual(repo.health_score, 61)

    def test_success_stores_summary_without_changing_score(self):
        repo = make_repo(health_score=61)
        scan = _scan(repo, raw={"health_score": 61})
        finding = _finding(scan, "Нет README")
        answer = (
            '{"summary":"Итог 61. Слабое место — документация.",'
            '"steps":[{"finding_title":"Нет README","action":"Добавьте README."}]}'
        )
        with patch("health.ai_summary.requests.post", return_value=_completion(answer)) as post:
            saved = generate_and_store(
                scan,
                findings=[finding],
                categories=[("Документация", 40)],
                overall=61,
            )
        scan.refresh_from_db()
        repo.refresh_from_db()
        self.assertTrue(saved)
        self.assertEqual(scan.raw["ai"]["summary"], "Итог 61. Слабое место — документация.")
        self.assertEqual(scan.raw["ai"]["steps"][0]["finding_title"], "Нет README")
        self.assertEqual(scan.raw["health_score"], 61)
        self.assertEqual(repo.health_score, 61)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["modelUri"], "gpt://b1test/yandexgpt-lite/latest")
        self.assertNotIn("health_score", sent["messages"][1]["text"])

    def test_card_hides_button_without_keys(self):
        repo = make_repo(org_slug="acme", repo_slug="tools")
        _scan(repo)
        with override_settings(YANDEX_GPT_FOLDER_ID="", YANDEX_GPT_API_KEY=""):
            page = self.client.get(reverse("health:repo-detail", args=["acme", "tools"]))
        self.assertNotContains(page, "Сводка YandexGPT")

    def test_button_and_markdown_use_saved_summary(self):
        repo = make_repo(org_slug="acme", repo_slug="tools", health_score=61)
        scan = _scan(
            repo,
            raw={
                "health_score": 61,
                "ai": {
                    "summary": "Проект держится на документации.",
                    "steps": [
                        {
                            "finding_title": "Нет README",
                            "action": "Добавьте README.",
                        }
                    ],
                },
            },
        )
        _finding(scan, "Нет README")
        page = self.client.get(reverse("health:repo-detail", args=["acme", "tools"]))
        self.assertContains(page, "Проект держится на документации.")
        self.assertContains(page, "Обновить сводку")
        md = self.client.get(reverse("health:repo-export", args=["acme", "tools", "md"]))
        body = md.content.decode()
        self.assertIn("## Сводка YandexGPT", body)
        self.assertIn("Проект держится на документации.", body)
        self.assertIn("Нет README", body)

    def test_post_saves_summary_on_the_card(self):
        repo = make_repo(org_slug="acme", repo_slug="tools", health_score=61)
        scan = _scan(repo, raw={"health_score": 61})
        _finding(scan, "Нет README")
        answer = (
            '{"summary":"Руководителю: документация тянет балл вниз.",'
            '"steps":[{"finding_title":"Нет README","action":"Добавьте README."}]}'
        )
        with patch("health.ai_summary.requests.post", return_value=_completion(answer)):
            response = self.client.post(
                reverse("health:repo-ai-summary", args=["acme", "tools"])
            )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(reverse("health:repo-detail", args=["acme", "tools"]))
        self.assertContains(page, "документация тянет балл вниз")
        scan.refresh_from_db()
        repo.refresh_from_db()
        self.assertEqual(scan.raw["health_score"], 61)
        self.assertEqual(repo.health_score, 61)
