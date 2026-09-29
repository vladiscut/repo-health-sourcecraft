"""Рекомендации на недобор подметрики, который сканер сам не описал.

Без них сумма «+N к Score» не добирает до 100 вместе с текущим баллом:
формула уже сняла пункты, а находки на эту подметрику нет.
"""

from health.models import Finding, HealthScore, MetricSample, Scan
from health.score_breakdown import SUBMETRIC_LABELS
from health.scoring import (
    CATEGORY_LABELS,
    ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS,
    ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS,
    CODE_HEALTH_SUBMETRIC_WEIGHTS,
    CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
    SUBMETRIC_WEIGHTS_BY_CATEGORY,
    _largest_remainder,
    bump_severity,
    find_impact,
    weighted_submetric_score,
)


_TITLE_TO_KEY = {
    "Отсутствует README": "readme_quality",
    "Нет инструкции локального запуска": "local_run",
    "Нет инструкций по сборке и тестам": "build_test",
    "Отсутствует лицензия": "license",
    "Тип лицензии не распознан": "license",
    "Нет CONTRIBUTING и CODEOWNERS": "contributing_codeowners",
    "Нет CHANGELOG и каталога docs/": "structure_extras",
    "Проект давно не проявлял активность": "recent_activity",
    "Нет коммитов в дефолтную ветку за последние 30 дней": "commits",
    "Нет merge requests за последние 30 дней": "merge_requests",
    "Нет релиза за последние 30 дней": "releases",
    "Медленный первый ответ на issues": "first_response",
    "Долгое время закрытия задач": "time_to_close",
    "Не найдено тестов": "tests_present",
    "Нет конфигурации линтера/форматтера": "lint_config_present",
    "Репозиторий похож на набор данных, а не на код": "structure",
    "Файлы свалены в корень без структуры": "structure",
    "В репозитории закоммичен мусор": "no_committed_junk",
    "Не найден манифест зависимостей": "dependency_hygiene",
    "Нет lockfile": "dependency_hygiene",
    "Накопленные TODO/FIXME": "todo_debt",
    "Нет конфигурации SourceCraft CI": "ci_config_present",
    "Много неуспешных прогонов CI": "success_rate",
    "Долгие прогоны CI": "duration",
}

# title, detail, recommendation. {score} и {label} подставляются в detail.
_COPY = {
    "readme_quality": (
        "README неполный",
        "README есть, но «{label}» — {score} из 100: не хватает объёма или раздела о структуре.",
        "Допишите README: зачем проект и из чего состоит репозиторий.",
    ),
    "license": (
        "Лицензия снижает балл",
        "Подметрика «{label}» — {score} из 100.",
        "Положите в корень стандартный файл LICENSE с распознаваемой лицензией.",
    ),
    "local_run": (
        "Нет инструкции локального запуска",
        "Подметрика «{label}» — {score} из 100.",
        "Добавьте в README раздел Quick Start: зависимости, установка и запуск.",
    ),
    "build_test": (
        "Нет инструкций по сборке и тестам",
        "Подметрика «{label}» — {score} из 100.",
        "Опишите в README команды сборки и запуска тестов.",
    ),
    "contributing_codeowners": (
        "Не хватает CONTRIBUTING или CODEOWNERS",
        "Подметрика «{label}» — {score} из 100.",
        "Добавьте недостающий CONTRIBUTING.md или CODEOWNERS.",
    ),
    "structure_extras": (
        "Документация репозитория неполная",
        "Подметрика «{label}» — {score} из 100: не хватает CHANGELOG, docs/ или шаблонов.",
        "Добавьте CHANGELOG, каталог docs/ или шаблоны issue и pull request.",
    ),
    "recent_activity": (
        "Активность репозитория снижается",
        "Подметрика «{label}» — {score} из 100. Полный балл — если последнее изменение не старше 7 дней.",
        "Верните регулярные изменения: коммит, merge request или релиз.",
    ),
    "commits": (
        "Мало коммитов за последние 30 дней",
        "Подметрика «{label}» — {score} из 100. Полный балл — от 20 коммитов в дефолтную ветку.",
        "Проверьте, что разработка идёт в дефолтную ветку, а не только в форки и другие ветки.",
    ),
    "merge_requests": (
        "Мало merge requests за последние 30 дней",
        "Подметрика «{label}» — {score} из 100. Полный балл — от 10 merge requests.",
        "Проверьте, что изменения проходят через merge requests, а не только прямыми коммитами.",
    ),
    "releases": (
        "Мало релизов за последние 30 дней",
        "Подметрика «{label}» — {score} из 100. Полный балл — от 3 релизов за 30 дней.",
        "Выпустите релиз или опишите, почему проект их не использует — балл считает отсутствие релизов недобором.",
    ),
    "stale_ratio": (
        "Есть зависшие issues",
        "Подметрика «{label}» — {score} из 100.",
        "Закройте неактуальные issues и назначьте ответственных на остальные.",
    ),
    "first_response": (
        "Медленный первый ответ на issues",
        "Подметрика «{label}» — {score} из 100. Полный балл — медиана до 24 часов.",
        "Настройте триаж новых issues и целевой срок первого ответа.",
    ),
    "time_to_close": (
        "Долгое время закрытия задач",
        "Подметрика «{label}» — {score} из 100. Полный балл — медиана до 7 дней.",
        "Разберите бэклог и дробите крупные задачи, чтобы их можно было закрывать быстрее.",
    ),
    "close_rate_30d": (
        "Низкая доля закрытых issues",
        "Подметрика «{label}» — {score} из 100.",
        "Закрывайте созданные за последний месяц задачи или переоценивайте, что попадает в бэклог.",
    ),
    "tests_present": (
        "Не найдено тестов",
        "Подметрика «{label}» — {score} из 100.",
        "Добавьте тесты хотя бы для критичной части и подключите их к CI.",
    ),
    "lint_config_present": (
        "Нет конфигурации линтера/форматтера",
        "Подметрика «{label}» — {score} из 100.",
        "Зафиксируйте в репозитории конфиг линтера или форматтера.",
    ),
    "structure": (
        "Структура репозитория снижает балл",
        "Подметрика «{label}» — {score} из 100.",
        "Разнесите код по каталогам и не оставляйте в репозитории только набор данных.",
    ),
    "no_committed_junk": (
        "В репозитории закоммичен мусор",
        "Подметрика «{label}» — {score} из 100.",
        "Уберите зависимости, артефакты сборки и бинарные файлы из git и добавьте их в .gitignore.",
    ),
    "dependency_hygiene": (
        "Зависимости зафиксированы не полностью",
        "Подметрика «{label}» — {score} из 100.",
        "Добавьте манифест зависимостей и lockfile.",
    ),
    "todo_debt": (
        "Накопленные TODO/FIXME",
        "Подметрика «{label}» — {score} из 100.",
        "Закройте старые TODO или переведите их в issues с владельцем.",
    ),
    "ci_config_present": (
        "Нет конфигурации SourceCraft CI",
        "Подметрика «{label}» — {score} из 100.",
        "Добавьте .sourcecraft/ci.yaml с базовой проверкой сборки.",
    ),
    "success_rate": (
        "Не все прогоны CI успешны",
        "Подметрика «{label}» — {score} из 100.",
        "Разберите упавшие прогоны: каждый неуспех снижает балл, даже если падений меньше 20%.",
    ),
    "duration": (
        "Прогоны CI можно ускорить",
        "Подметрика «{label}» — {score} из 100. Полный балл — медиана 5 минут и меньше.",
        "Сократите пайплайн: кэш зависимостей и меньше шагов на каждый push.",
    ),
    "direct": (
        "Открытые критические уязвимости",
        "Подметрика «{label}» — {score} из 100.",
        "Закройте прямые critical в AppSec или отметьте ложное срабатывание.",
    ),
    "transitive": (
        "Открытые транзитивные критические уязвимости",
        "Подметрика «{label}» — {score} из 100.",
        "Обновите зависимость с транзитивным critical или отметьте ложное срабатывание.",
    ),
    "high": (
        "Открытые уязвимости высокой серьёзности",
        "Подметрика «{label}» — {score} из 100.",
        "Закройте открытые high в AppSec или отметьте ложное срабатывание.",
    ),
}


def _matching_weights(
    options: list[dict[str, float]],
    submetrics: dict[str, float],
    total: int,
) -> dict[str, float]:
    for weights in options:
        score, _completeness = weighted_submetric_score(submetrics, weights)
        if score == total:
            return weights
    return options[0]


def _weights(
    category: str,
    raw: dict,
    submetrics: dict[str, float],
    total: int,
) -> dict[str, float]:
    if category == MetricSample.Category.ACTIVITY:
        with_commits = ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS
        without_commits = ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS
        if "include_commits" in raw:
            return with_commits if raw.get("include_commits") else without_commits
        return _matching_weights([without_commits, with_commits], submetrics, total)
    if category == MetricSample.Category.CODE_HEALTH:
        with_age = CODE_HEALTH_SUBMETRIC_WEIGHTS
        without_age = CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE
        if "include_todo_age" in raw:
            return with_age if raw.get("include_todo_age") else without_age
        return _matching_weights([with_age, without_age], submetrics, total)
    table = SUBMETRIC_WEIGHTS_BY_CATEGORY.get(category)
    if not isinstance(table, dict):
        return {}
    if table and isinstance(next(iter(table.values())), dict):
        return {}
    return table


def _claimed_keys(finding: Finding) -> set[str]:
    keys: set[str] = set()
    for ref in finding.evidence_refs or []:
        if isinstance(ref, str) and ref.startswith("score-gap:"):
            keys.add(ref.removeprefix("score-gap:"))
    mapped = _TITLE_TO_KEY.get(finding.title)
    if mapped:
        keys.add(mapped)
    title = finding.title or ""
    if "не обновлялись" in title:
        keys.add("stale_ratio")
    if finding.category == MetricSample.Category.SECURITY:
        lowered = title.lower()
        if "транзит" in lowered:
            keys.add("transitive")
        elif "critical" in lowered:
            keys.add("direct")
        elif lowered.startswith("high"):
            keys.add("high")
    return keys


def _numeric_scores(submetrics: dict) -> dict[str, float]:
    scores: dict[str, float] = {}
    for key, value in submetrics.items():
        try:
            scores[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return scores


def _copy(key: str, score: float) -> tuple[str, str, str]:
    label = SUBMETRIC_LABELS.get(key, key)
    shown = round(score)
    spec = _COPY.get(key)
    if spec is None:
        return (
            f"{label}: {shown} из 100",
            f"Подметрика «{label}» — {shown} из 100, категория из-за этого ниже 100.",
            f"Доведите «{label}» до 100 — эти пункты вернутся в Repo Health Score.",
        )
    title, detail, recommendation = spec
    return title, detail.format(score=shown, label=label), recommendation


def _is_residual(finding: Finding) -> bool:
    return "score-gap:residual" in (finding.evidence_refs or [])


def _explained_points(findings: list[Finding], saved: dict, category: str) -> int:
    total = 0
    for item in findings:
        if item.category != category or _is_residual(item):
            continue
        if item.pk and str(item.pk) in saved:
            total += max(0, int(saved[str(item.pk)]))
        else:
            total += max(0, int(item.estimated_score_impact or 0))
    return total


def _cover_unexplained(
    score_row,
    findings: list[Finding],
    saved: dict,
    created: list[Finding],
    has_submetrics: bool,
) -> None:
    """Остаток категории без сохранённой разбивки по подметрикам."""

    headroom = 100 - int(score_row.total)
    explained = _explained_points(findings, saved, score_row.category)
    residual = headroom - explained
    if has_submetrics and residual > 1:
        return
    owners = [
        item for item in findings
        if item.pk and item.category == score_row.category and _is_residual(item)
    ]
    if residual <= 1:
        for owner in owners:
            owner.delete()
            saved.pop(str(owner.id), None)
            findings.remove(owner)
        return
    if owners:
        saved[str(owners[0].id)] = residual
        for extra in owners[1:]:
            extra.delete()
            saved.pop(str(extra.id), None)
            findings.remove(extra)
        return
    label = CATEGORY_LABELS.get(score_row.category, score_row.category)
    severity = bump_severity(
        Finding.Severity.MEDIUM if score_row.total < 50 else Finding.Severity.LOW,
        score_row.total,
        40,
    )
    finding = Finding(
        scan_id=score_row.scan_id,
        category=score_row.category,
        severity=severity,
        title=f"{label} недобирает баллы",
        detail=(
            f"Балл категории — {score_row.total} из 100. "
            "Часть недобора не привязана к отдельной подметрике в этом скане."
        ),
        recommendation=(
            "Откройте разбивку категории на карточке и поднимите самые низкие части до 100."
        ),
        evidence_refs=["score-gap:residual"],
        estimated_score_impact=residual,
    )
    created.append(finding)
    findings.append(finding)


def ensure_gap_findings(scan: Scan) -> None:
    """Добавляет находку на каждую подметрику ниже 100, если её ещё нет."""

    raw = dict(scan.raw or {})
    saved = dict(raw.get("finding_category_points") or {})
    findings = list(Finding.objects.filter(scan=scan))
    created: list[Finding] = []

    for score_row in HealthScore.objects.filter(scan=scan):
        if score_row.total is None or score_row.total >= 100:
            continue
        metrics_raw = score_row.raw_metrics or {}
        submetrics = _numeric_scores(metrics_raw.get("submetric_scores") or {})
        weights = _weights(score_row.category, metrics_raw, submetrics, score_row.total)
        if submetrics and weights:
            for key, score in submetrics.items():
                if score >= 100 or key not in weights:
                    continue
                points = find_impact(key, weights, submetrics)
                if points <= 0:
                    continue
                owners = [
                    item for item in findings if item.pk and key in _claimed_keys(item)
                ]
                if owners:
                    shares = _largest_remainder([1.0] * len(owners), points)
                    for owner, share in zip(owners, shares):
                        saved[str(owner.id)] = share
                    continue
                title, detail, recommendation = _copy(key, score)
                severity = Finding.Severity.MEDIUM if score < 50 else Finding.Severity.LOW
                severity = bump_severity(severity, score_row.total, 40)
                finding = Finding(
                    scan=scan,
                    category=score_row.category,
                    severity=severity,
                    title=title,
                    detail=detail,
                    recommendation=recommendation,
                    evidence_refs=[f"score-gap:{key}"],
                    estimated_score_impact=points,
                )
                created.append(finding)
                findings.append(finding)
        _cover_unexplained(
            score_row, findings, saved, created, has_submetrics=bool(submetrics)
        )

    if created:
        Finding.objects.bulk_create(created)
        for finding in created:
            saved[str(finding.id)] = finding.estimated_score_impact

    raw["finding_category_points"] = saved
    scan.raw = raw
