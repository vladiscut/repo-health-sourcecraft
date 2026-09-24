from rest_framework import viewsets

from health.api.serializers import RepositorySerializer
from health.models import Repository


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
            return queryset.order_by("-health_score", "-rating_value")
        if sort == "updated":
            return queryset.order_by("-last_updated", "-rating_value")
        return queryset.order_by("-rating_value")
