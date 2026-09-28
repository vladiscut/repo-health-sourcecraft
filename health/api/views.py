from django.db.models import F
from rest_framework import viewsets

from health.api.serializers import RepositorySerializer
from health.models import Repository
from health.repo_ordering import annotate_visible_score


class RepositoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Repository.objects.filter(
        visibility=Repository.VisibilityType.PUBLIC,
    )
    serializer_class = RepositorySerializer
    lookup_field = "repo_slug"

    def get_queryset(self):
        queryset = super().get_queryset()
        language = self.request.query_params.get("language")
        if language:
            queryset = queryset.filter(language=language)
        sort = self.request.query_params.get("sort", "rating")
        if sort == "score":
            return annotate_visible_score(queryset).order_by(
                F("visible_score").desc(nulls_last=True),
                F("rating_value").desc(nulls_last=True),
            )
        if sort == "updated":
            return queryset.order_by("-last_updated", "-rating_value")
        return queryset.order_by("-rating_value")
