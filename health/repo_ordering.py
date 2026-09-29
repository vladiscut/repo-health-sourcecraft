from django.db.models import FloatField
from django.db.models.expressions import RawSQL

from health.models import HealthScore, Repository, Scan
from health.scoring import CATEGORY_WEIGHTS, PREVIEW_SCORE_WEIGHT_THRESHOLD


def annotate_visible_score(queryset):
    """Добавляет visible_score по последнему SUCCESS с достаточным покрытием.

    Формула та же, что у числа в колонке: вес категории умножается на
    полноту данных. PARTIAL и покрытие ниже порога — как «нет данных».
    """

    repo = Repository._meta.db_table
    score = HealthScore._meta.db_table
    scan = Scan._meta.db_table
    placeholders = ", ".join(["(%s, %s)"] * len(CATEGORY_WEIGHTS))
    params: list = [PREVIEW_SCORE_WEIGHT_THRESHOLD]
    for category, weight in CATEGORY_WEIGHTS.items():
        params.extend([str(category), weight])
    sql = f"""
        (
          SELECT CASE
            WHEN SUM(w.weight) < %s THEN NULL
            ELSE SUM(hs.total * w.weight * COALESCE(hs.data_completeness, 1))
                 / NULLIF(SUM(w.weight * COALESCE(hs.data_completeness, 1)), 0)
          END
          FROM {score} hs
          JOIN (VALUES {placeholders}) AS w(category, weight)
            ON hs.category = w.category
          WHERE hs.total IS NOT NULL
            AND hs.scan_id = (
              SELECT s.id
              FROM {scan} s
              WHERE s.repository_id = {repo}.id
                AND s.status = 'success'
              ORDER BY s.created_at DESC
              LIMIT 1
            )
        )
    """
    return queryset.annotate(
        visible_score=RawSQL(sql, params, output_field=FloatField())
    )
