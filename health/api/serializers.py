from rest_framework import serializers

from health.models import Repository


class RepositorySerializer(serializers.ModelSerializer):
    scores = serializers.SerializerMethodField()

    class Meta:
        model = Repository
        fields = (
            "id",
            "org_slug",
            "repo_slug",
            "description",
            "language",
            "rating_value",
            "rating_percentile",
            "likes",
            "hearts",
            "diamonds",
            "health_score",
            "forks",
            "last_scanned_at",
            "scores",
        )

    def get_scores(self, obj):
        """Возвращает ``{категория: total}`` последнего прогона."""
        scan = obj.latest_completed_scan()
        if not scan:
            return None
        return {
            row.category: row.total
            for row in scan.scores.all()
        }
