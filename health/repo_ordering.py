from django.db.models import (
    Case,
    ExpressionWrapper,
    F,
    FloatField,
    OuterRef,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from health.models import HealthScore, Scan
from health.scoring import CATEGORY_WEIGHTS, PREVIEW_SCORE_WEIGHT_THRESHOLD


def _category_weight():
    return Case(
        *[
            When(category=category, then=Value(weight))
            for category, weight in CATEGORY_WEIGHTS.items()
        ],
        default=Value(0.0),
        output_field=FloatField(),
    )


def _effective_weight():
    completeness = Coalesce(
        F("data_completeness"),
        Value(1.0),
        output_field=FloatField(),
    )
    return ExpressionWrapper(
        _category_weight() * completeness,
        output_field=FloatField(),
    )


def annotate_visible_score(queryset):
    """Добавляет visible_score по последнему SUCCESS с достаточным покрытием.

    Формула та же, что у числа в колонке: вес категории умножается на
    полноту данных. PARTIAL и покрытие ниже порога — как «нет данных».
    """

    latest_success = (
        Scan.objects.filter(
            repository_id=OuterRef(OuterRef("pk")),
            status=Scan.Status.SUCCESS,
        )
        .order_by("-created_at")
        .values("pk")[:1]
    )
    covered = Sum(_category_weight())
    effective = Sum(_effective_weight())
    weighted_total = Sum(
        ExpressionWrapper(
            F("total") * _effective_weight(),
            output_field=FloatField(),
        )
    )
    per_scan = (
        HealthScore.objects.filter(
            scan_id=Subquery(latest_success),
            total__isnull=False,
            category__in=list(CATEGORY_WEIGHTS),
        )
        .values("scan_id")
        .annotate(
            covered_weight=covered,
            effective_weight=effective,
            weighted_total=weighted_total,
        )
        .annotate(
            visible=Case(
                When(
                    covered_weight__lt=PREVIEW_SCORE_WEIGHT_THRESHOLD,
                    then=Value(None, output_field=FloatField()),
                ),
                When(
                    effective_weight__lte=0,
                    then=Value(None, output_field=FloatField()),
                ),
                default=ExpressionWrapper(
                    F("weighted_total") / F("effective_weight"),
                    output_field=FloatField(),
                ),
                output_field=FloatField(),
            )
        )
        .order_by()
        .values("visible")[:1]
    )
    return queryset.annotate(
        visible_score=Subquery(per_scan, output_field=FloatField())
    )
