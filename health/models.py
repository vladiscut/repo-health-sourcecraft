from django.conf import settings
from django.db import models

from core.encrypt import EncryptedTextField
from health.formatters import format_percentile, format_rating


class Profile(models.Model):
    """Профиль пользователя, вошедшего через Я ID."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    ya_id = models.CharField(
        "идентификатор Я ID",
        max_length=255,
        unique=True,
    )
    access_token = EncryptedTextField(
        "OAuth access token Я ID",
        null=True,
        blank=True,
    )
    refresh_token = EncryptedTextField(
        "OAuth refresh token Я ID",
        null=True,
        blank=True,
    )
    token_expires_at = models.DateTimeField(
        "срок access token",
        null=True,
        blank=True,
    )
    sourcecraft_pat = EncryptedTextField(
        "персональный токен SourceCraft",
        null=True,
        blank=True,
        help_text="Нужен, если токен Я ID не подходит для API SourceCraft",
    )
    sourcecraft_username = models.CharField(
        "логин SourceCraft",
        max_length=255,
        blank=True,
    )

    class Meta:
        verbose_name = "профиль"
        verbose_name_plural = "профили"

    def __str__(self) -> str:
        return self.sourcecraft_username or self.ya_id

    @property
    def sourcecraft_token(self) -> str | None:
        """PAT имеет приоритет: им ходим в API SourceCraft."""
        return self.sourcecraft_pat or self.access_token

    @property
    def pat_mask(self) -> str:
        token = self.sourcecraft_pat or ""
        if len(token) < 4:
            return ""
        return f"•••• {token[-4:]}"


class UserRepositoryAccess(models.Model):
    """Репозитории SourceCraft, доступные пользователю."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="repository_access",
    )
    repository = models.ForeignKey(
        "Repository",
        on_delete=models.CASCADE,
        related_name="user_access",
    )

    class Meta:
        verbose_name = "доступ к репозиторию"
        verbose_name_plural = "доступ к репозиториям"
        unique_together = ("user", "repository")

    def __str__(self) -> str:
        return f"{self.user} -> {self.repository}"


class Repository(models.Model):
    """Репозиторий SourceCraft"""

    class VisibilityType(models.TextChoices):
        PUBLIC = "public", "public"
        INTERNAL = "internal", "internal"
        PRIVATE = "private", "private"

    org_slug = models.CharField(
        "организация",
        max_length=255,
    )
    repo_slug = models.CharField(
        "репозиторий",
        max_length=500,
    )
    description = models.TextField(
        "описание",
        blank=True,
    )
    language = models.CharField(
        "язык",
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
    )
    rating_value = models.FloatField(
        "рейтинг SourceCraft",
        default=0,
        help_text="rating.value — составной рейтинг платформы, им сортируем",
    )
    rating_percentile = models.FloatField(
        "перцентиль рейтинга",
        null=True,
        blank=True,
        help_text="1 = топ-1% публичных репозиториев SourceCraft",
    )
    likes = models.PositiveIntegerField(
        "лайки",
        default=0,
        help_text="👍 positive_low",
    )
    hearts = models.PositiveIntegerField(
        "сердца",
        default=0,
        help_text="❤️ positive_medium",
    )
    diamonds = models.PositiveIntegerField(
        "бриллианты",
        default=0,
        help_text="💎 positive_high",
    )
    health_score = models.PositiveSmallIntegerField(
        "Repo Health Score",
        null=True,
        blank=True,
        help_text="Кэш итогового Score 0–100, null = ещё не считали",
    )
    forks = models.PositiveIntegerField(
        "форки",
        default=0,
    )
    issues = models.PositiveIntegerField(
        "issues",
        default=0,
    )
    sourcecraft_id = models.CharField(
        "идентификатор SourceCraft",
        max_length=255,
        unique=True,
    )
    url = models.URLField(
        max_length=500,
    )
    logo_url = models.URLField(
        max_length=255,
        blank=True,
        null=True,
    )
    is_empty = models.BooleanField(
        default=False
    )
    last_updated = models.DateTimeField(
        null=True,
        blank=True,
    )
    visibility = models.CharField(
        max_length=10,
        choices=VisibilityType.choices,
        db_index=True,
    )
    last_scanned_at = models.DateTimeField(
        "последняя проверка",
        null=True,
        blank=True,
    )
    last_commit_sha_processed = models.CharField(
        "поледний просканированный хеш коммита",
        max_length=64,
        blank=True,
    )
    default_branch = models.CharField(
        "ветка по умолчанию",
        max_length=255,
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(
        auto_now_add=True
    )
    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        verbose_name = "репозиторий"
        verbose_name_plural = "репозитории"
        ordering = ["org_slug", "repo_slug"]
        indexes = [
            models.Index(
                fields=["visibility", "-rating_value"],
                name="health_repo_visibil_rating_idx",
            ),
            models.Index(
                fields=["visibility", "-health_score"],
                name="health_repo_visibil_score_idx",
            ),
            models.Index(
                fields=["visibility", "language"],
                name="health_repo_visibil_lang_idx",
            ),
        ]

    def __str__(self):
        return f"{self.org_slug}/{self.repo_slug}"

    @property
    def rating_display(self) -> str:
        return format_rating(self.rating_value)

    @property
    def percentile_display(self) -> str:
        return format_percentile(self.rating_percentile)

    def latest_scan(self):
        return self.scans.all().first()

    def latest_completed_scan(self):
        return self.scans.filter(
            status__in=[Scan.Status.SUCCESS, Scan.Status.PARTIAL]
        ).first()


class Scan(models.Model):
    """Один запуск анализа репозитория — неизменяемый снимок результата."""

    class Status(models.TextChoices):
        PENDING = "pending", "в очереди"
        RUNNING = "running", "идёт"
        SUCCESS = "success", "готово"
        FAILED = "failed", "ошибка"
        PARTIAL = "partial", "частично (часть данных недоступна)"

    class TriggeredBy(models.TextChoices):
        SCHEDULE = "schedule", "по расписанию"
        USER = "user", "пользователем"
        MANUAL = "manual", "вручную (management-команда)"

    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="scans",
        verbose_name="репозиторий",
    )
    status = models.CharField(
        "статус",
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(
        "создан",
        auto_now_add=True,
    )
    finished_at = models.DateTimeField(
        "завершён",
        null=True,
        blank=True,
    )
    raw = models.JSONField(
        "сырые данные",
        default=dict,
        blank=True,
    )
    error = models.TextField(
        "ошибка",
        null=True,
        blank=True,
    )
    triggered_by = models.CharField(
        "кем запущен",
        max_length=20,
        choices=TriggeredBy.choices,
    )
    triggered_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_scans",
    )
    commit_sha_at_analysis = models.CharField(
        "хеш коммита",
        max_length=64,
        blank=True,
        null=True,
    )

    class Meta:
        verbose_name = "проверка"
        verbose_name_plural = "проверки"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["repository"],
                condition=models.Q(status__in=["pending", "running"]),
                name="one_active_scan_per_repo",
            )
        ]

    def __str__(self):
        return f"{self.repository} @ {self.created_at:%Y-%m-%d %H:%M}"


class MetricSample(models.Model):
    """Одна собранная метрика внутри категории для конкретного прогона анализа."""

    class Category(models.TextChoices):
        DOCS = "docs", "документация и лучшие практики"
        CI_CD = "ci_cd", "CI/CD"
        SECURITY = "security", "security"
        ACTIVITY = "activity", "активность проекта"
        ISSUES = "issues", "issues"
        CODE_HEALTH = "code_health", "состояние кода и технический долг"

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="metric_samples",
    )
    category = models.CharField(
        "категория",
        max_length=15,
        choices=Category.choices,
    )
    metric_key = models.CharField(
        "например: ci_success_rate_90d, readme_present, todo_count",
        max_length=100,
    )
    value = models.JSONField(
        "значение метрики (число/строка/структура) или null, если недоступно",
        null=True,
        blank=True,
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
    )
    is_available = models.BooleanField(
        "доступность",
        default=True,
        help_text="False = данные недоступны ('нет данных'), не путать со значением 0",
    )
    error_reason = models.CharField(
        "причина недоступности, если is_available=False",
        max_length=255,
        blank=True,
    )
    source_reference = models.CharField(
        "ссылка на файл/pipeline/commit/issue/MR, подтверждающая метрику",
        max_length=500,
        blank=True,
    )
    created_at = models.DateTimeField(
        "создан",
        auto_now_add=True,
    )

    class Meta:
        verbose_name = "Метрика"
        verbose_name_plural = "Метрики"
        unique_together = ("scan", "category", "metric_key")

    def __str__(self) -> str:
        return f"{self.scan_id}:{self.category}:{self.metric_key}"


class HealthScore(models.Model):
    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="scores",
        verbose_name="проверка"
    )
    category = models.CharField(
        "категория",
        max_length=15,
        choices=MetricSample.Category.choices
    )
    total = models.PositiveSmallIntegerField(
        "итого",
        null=True,
        blank=True,
        help_text="0-100, null = категория помечена 'Нет данных'"
    )
    weight_used = models.FloatField(
        "вес категории, фактически применённый при расчёте (после перенормировки)"
    )
    data_completeness = models.FloatField(
        "доля доступных метрик категории, от 0.0 до 1.0",
        default=1.0,
    )
    raw_metrics = models.JSONField(
        "снимок значений метрик, использованных в расчёте",
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = "оценка по категории"
        verbose_name_plural = "оценки по категории"
        unique_together = ("scan", "category")

    def __str__(self) -> str:
        return f"{self.scan_id}:{self.category}={self.total if self.total is not None else 'нет данных'}"


class Finding(models.Model):

    class Severity(models.TextChoices):
        LOW = "1", "low"
        MEDIUM = "2", "medium"
        HIGH = "3", "high"
        CRITICAL = "4", "critical"

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="findings",
        verbose_name="проверка",
    )
    category = models.CharField(
        "категория",
        max_length=15,
        choices=MetricSample.Category.choices
    )
    severity = models.CharField(
        "серьёзность",
        max_length=16,
        choices=Severity.choices,
    )
    title = models.CharField(
        "заголовок",
        max_length=255,
    )
    detail = models.TextField(
        "детали",
        blank=True,
    )
    recommendation = models.TextField(
        "рекомендация",
        blank=True,
    )
    evidence_refs = models.JSONField(
        "список ссылок на факты: файлы/pipeline/vulnerability/commit/issue/MR",
        default=list,
        blank=True,
    )
    estimated_score_impact = models.SmallIntegerField(
        "ожидаемый прирост Repo Health Score при устранении проблемы",
        default=0,
    )

    class Meta:
        verbose_name = "находка"
        verbose_name_plural = "находки"
        ordering = ["-estimated_score_impact", "-severity"]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title}"
