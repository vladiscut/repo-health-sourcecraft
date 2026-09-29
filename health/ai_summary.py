import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
MODEL_NAME = "yandexgpt-lite/latest"
TIMEOUT_SECONDS = 8
MAX_TOKENS = 400
TEMPERATURE = 0.2
MAX_FINDINGS = 5
MAX_STEPS = 3

SYSTEM_PROMPT = (
    "Ты пишешь короткую сводку о здоровье репозитория. "
    "Используй только факты из JSON пользователя. Не придумывай числа, файлы и проблемы. "
    "Ответ — один JSON-объект без пояснений и без markdown: "
    '{"summary":"3-5 предложений по-русски",'
    '"steps":[{"finding_title":"точный заголовок находки","action":"одно действие"}]}. '
    "steps — не больше трёх. finding_title копируй из facts.findings.title. "
    "Если находок нет, steps — пустой массив."
)


def ai_configured() -> bool:
    folder = getattr(settings, "YANDEX_GPT_FOLDER_ID", "") or ""
    key = getattr(settings, "YANDEX_GPT_API_KEY", "") or ""
    return bool(folder.strip() and key.strip())


def stored_summary(scan) -> dict | None:
    if scan is None:
        return None
    payload = (scan.raw or {}).get("ai")
    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return None
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    clean_steps = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        title = str(step.get("finding_title") or "").strip()
        action = str(step.get("action") or "").strip()
        if title and action:
            clean_steps.append({"finding_title": title, "action": action})
    return {"summary": summary, "steps": clean_steps}


def parse_summary(text: str, finding_titles: list[str]) -> dict | None:

    data = _extract_json(text)
    if data is None:
        return None
    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    titles = {title.casefold(): title for title in finding_titles if title}
    steps = []
    seen: set[str] = set()
    raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    for item in raw_steps:
        if len(steps) >= MAX_STEPS:
            break
        if not isinstance(item, dict):
            continue
        title = str(item.get("finding_title") or "").strip()
        action = str(item.get("action") or "").strip()
        canonical = titles.get(title.casefold())
        if not canonical or not action or canonical in seen:
            continue
        seen.add(canonical)
        steps.append({"finding_title": canonical, "action": action[:400]})
    return {"summary": summary[:1500], "steps": steps}


def generate_and_store(scan, *, findings, categories, overall) -> bool:

    if scan is None or not ai_configured():
        return False
    chosen = list(findings)[:MAX_FINDINGS]
    text = _complete(_facts(scan, chosen, categories, overall))
    if not text:
        return False
    parsed = parse_summary(text, [item.title for item in chosen])
    if parsed is None:
        return False
    raw = dict(scan.raw or {})
    raw["ai"] = {
        "summary": parsed["summary"],
        "steps": parsed["steps"],
        "model": MODEL_NAME,
        "generated_at": timezone.now().isoformat(timespec="seconds"),
    }
    scan.raw = raw
    scan.save(update_fields=["raw"])
    return True


def _facts(scan, findings, categories, overall) -> dict:
    repo = scan.repository
    rows = []
    for label, value in categories:
        rows.append({"name": label, "score": value})
    items = []
    for finding in findings:
        items.append(
            {
                "title": finding.title,
                "severity": finding.get_severity_display(),
                "impact": finding.estimated_score_impact,
                "action": finding.recommendation,
            }
        )
    return {
        "repository": f"{repo.org_slug}/{repo.repo_slug}",
        "score": overall,
        "categories": rows,
        "findings": items,
    }


def _complete(facts: dict) -> str | None:
    folder = settings.YANDEX_GPT_FOLDER_ID.strip()
    key = settings.YANDEX_GPT_API_KEY.strip()
    payload = {
        "modelUri": f"gpt://{folder}/{MODEL_NAME}",
        "completionOptions": {
            "stream": False,
            "temperature": TEMPERATURE,
            "maxTokens": str(MAX_TOKENS),
        },
        "messages": [
            {"role": "system", "text": SYSTEM_PROMPT},
            {"role": "user", "text": json.dumps(facts, ensure_ascii=False)},
        ],
    }
    try:
        response = requests.post(
            COMPLETION_URL,
            json=payload,
            headers={
                "Authorization": f"Api-Key {key}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning("YandexGPT request failed: %s", exc)
        return None
    if not response.ok:
        logger.warning("YandexGPT HTTP %s", response.status_code)
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    alternatives = (body.get("result") or {}).get("alternatives") or []
    if not alternatives:
        return None
    message = alternatives[0].get("message") or {}
    text = str(message.get("text") or "").strip()
    return text or None


def _extract_json(text: str) -> dict | None:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data
