from rest_framework.routers import DefaultRouter

from health.api.views import RepositoryViewSet

app_name = 'health-api'

router = DefaultRouter()
router.register("repos", RepositoryViewSet)

urlpatterns = router.urls
