from django.apps import AppConfig


class HealthConfig(AppConfig):
    name = "health"
    verbose_name = "здоровье репозиториев"

    def ready(self):
        from health import signals  # noqa: F401
