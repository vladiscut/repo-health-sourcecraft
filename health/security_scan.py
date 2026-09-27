"""Прогон анализа категории Security для одного Scan.

Категория считается только в личном контуре: токен берётся из
Profile.sourcecraft_pat пользователя Scan.triggered_by_user.
Без токена, без скана AppSec или при 401/403/404 пишется HealthScore
с total=None. 429 и 5xx не глотаются — их ретраит Celery.

Модуль не меняет Scan.status и не считает итоговый Repo Health Score.
"""

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction

from health.models import Finding, HealthScore, MetricSample, Profile, Scan
from health.scoring import CATEGORY_WEIGHTS
from integrations.appsec import AppSecClient, AppSecClientError

logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.SECURITY
CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]

CLEAN_SCORE = 100
DIRECT_CRITICAL_PENALTY = 30
TRANSITIVE_CRITICAL_PENALTY = 20
HIGH_PENALTY = 10

SCANNERS = frozenset({"sca", "sast", "secrets"})
CLOSED_STATUSES = frozenset({
    "resolved",
    "solved",
    "closed",
    "false_positive",
    "false-positive",
})


@dataclass
class _GroupHit:
    severity: str
    scanner: str
    transitive: bool
    penalty: int
    url: str


def _user_pat(scan: Scan) -> str:
    user = scan.triggered_by_user
    if user is None:
        return ""
    try:
        profile = user.profile
    except Profile.DoesNotExist:
        return ""
    return (profile.sourcecraft_pat or "").strip()


def _save_unavailable(scan: Scan, reason: str) -> HealthScore:
    with transaction.atomic():
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key="_category_unavailable",
            defaults=dict(
                value=None,
                is_available=False,
                error_reason=reason[:255],
            ),
        )
        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=None,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=0.0,
                raw_metrics={"reason": reason},
            ),
        )
    return health_score


def _scanner(group: dict[str, Any]) -> str:
    raw = (
        group.get("scanner")
        or group.get("analyzer")
        or group.get("scannerType")
        or group.get("type")
        or ""
    )
    text = str(raw).lower()
    if "sast" in text:
        return "sast"
    if "secret" in text:
        return "secrets"
    if text == "sca" or "sca" in text:
        return "sca"
    return ""


def _is_open(group: dict[str, Any]) -> bool:
    if group.get("falsePositive") or group.get("isFalsePositive") or group.get("false_positive"):
        return False
    status = str(group.get("status") or group.get("state") or "").lower().replace(" ", "_")
    return status not in CLOSED_STATUSES


def _is_transitive(group: dict[str, Any]) -> bool:
    if group.get("transitive") or group.get("isTransitive") or group.get("is_transitive"):
        return True
    depth = group.get("dependencyDepth")
    return isinstance(depth, int) and depth > 1


def _group_url(group: dict[str, Any]) -> str:
    url = group.get("url") or group.get("htmlUrl") or group.get("link")
    if url:
        return str(url)
    uuid = str(group.get("uuid") or group.get("id") or "")
    if not uuid:
        return ""
    base = settings.SOURCECRAFT_API_APPSEC_BASE_URL.rstrip("/")
    return f"{base}/v1/defect-groups/{uuid}"


def _hit(group: dict[str, Any]) -> _GroupHit | None:
    if not _is_open(group):
        return None
    scanner = _scanner(group)
    if scanner not in SCANNERS:
        return None
    severity = str(group.get("severity") or "").lower()
    transitive = _is_transitive(group)
    if severity == "critical":
        penalty = TRANSITIVE_CRITICAL_PENALTY if transitive else DIRECT_CRITICAL_PENALTY
    elif severity == "high":
        penalty = HIGH_PENALTY
    else:
        return None
    return _GroupHit(
        severity=severity,
        scanner=scanner,
        transitive=transitive,
        penalty=penalty,
        url=_group_url(group),
    )


def _collect(groups: list[dict[str, Any]]) -> list[_GroupHit]:
    hits: list[_GroupHit] = []
    for group in groups:
        hit = _hit(group)
        if hit is not None:
            hits.append(hit)
    return hits


def _score(hits: list[_GroupHit]) -> int:
    return max(0, CLEAN_SCORE - sum(hit.penalty for hit in hits))


def _build_findings(scan: Scan, hits: list[_GroupHit]) -> None:
    Finding.objects.filter(scan=scan, category=CATEGORY).delete()
    findings = [
        Finding(
            scan=scan,
            category=CATEGORY,
            severity=(
                Finding.Severity.CRITICAL
                if hit.severity == "critical"
                else Finding.Severity.HIGH
            ),
            title=(
                f"{hit.severity} в {hit.scanner.upper()}"
                + (" (транзитивная)" if hit.transitive else "")
            ),
            detail=(
                f"Открытая группа {hit.severity} анализатора {hit.scanner.upper()} "
                "снижает балл категории Security."
            ),
            recommendation="Закройте уязвимость или отметьте ложное срабатывание в AppSec.",
            evidence_refs=[hit.url] if hit.url else [],
            estimated_score_impact=hit.penalty,
        )
        for hit in hits
    ]
    if findings:
        Finding.objects.bulk_create(findings)


def _save_scored(scan: Scan, hits: list[_GroupHit], scan_uuid: str) -> HealthScore:
    total = _score(hits)
    critical_count = sum(1 for hit in hits if hit.severity == "critical")
    high_count = sum(1 for hit in hits if hit.severity == "high")
    with transaction.atomic():
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key="security_open_critical_count",
            defaults=dict(value=critical_count, unit="groups", is_available=True),
        )
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key="security_open_high_count",
            defaults=dict(value=high_count, unit="groups", is_available=True),
        )
        _build_findings(scan, hits)
        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=total,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=1.0,
                raw_metrics={
                    "scan_uuid": scan_uuid,
                    "open_critical_count": critical_count,
                    "open_high_count": high_count,
                    "transitive_critical_count": sum(
                        1 for hit in hits if hit.severity == "critical" and hit.transitive
                    ),
                },
            ),
        )
    return health_score


def run_security_scan(scan: Scan, client: AppSecClient) -> HealthScore:
    repository = scan.repository
    repo_id = repository.sourcecraft_id
    try:
        latest = client.get_latest_scan(repo_id)
    except AppSecClientError as exc:
        logger.info(f"AppSec недоступен для {repository}: {exc}")
        return _save_unavailable(scan, str(exc))

    if not latest:
        return _save_unavailable(scan, "нет скана AppSec")

    status = str(latest.get("status") or "").upper()
    if status != "FINISHED":
        return _save_unavailable(scan, f"скан AppSec не FINISHED: {status or 'пусто'}")

    scan_uuid = str(latest.get("uuid") or "")
    try:
        groups = client.list_defect_groups(repo_id, scan_uuid=scan_uuid or None)
    except AppSecClientError as exc:
        logger.info(f"Группы дефектов недоступны для {repository}: {exc}")
        return _save_unavailable(scan, str(exc))

    return _save_scored(scan, _collect(groups), scan_uuid)


def run(scan_id: int) -> int:
    scan = Scan.objects.select_related("repository", "triggered_by_user").get(pk=scan_id)
    pat = _user_pat(scan)
    if not pat:
        health_score = _save_unavailable(
            scan,
            "нет персонального токена SourceCraft у владельца скана",
        )
        return health_score.pk

    client = AppSecClient(token=pat)
    health_score = run_security_scan(scan, client)
    return health_score.pk
