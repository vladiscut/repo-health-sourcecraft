from django.apps import AppConfig


class HealthConfig(AppConfig):
    name = "health"
    verbose_name = "здоровье репозиториев"

    def ready(self):
        # Регистрация сигналов жизненного цикла сканов.
        from health import signals  # noqa: F401
