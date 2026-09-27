from django.contrib import admin

from health.models import Finding, HealthScore, Profile, Repository, Scan, MetricSample, UserRepositoryAccess


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "ya_id", "sourcecraft_username")
    search_fields = ("user__username", "ya_id", "sourcecraft_username")
    readonly_fields = ("access_token", "refresh_token", "sourcecraft_pat")


@admin.register(UserRepositoryAccess)
class UserRepositoryAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "repository")
    search_fields = ("user__username", "repository__org_slug", "repository__repo_slug")


class FindingInline(admin.TabularInline):
    model = Finding
    extra = 0


class HealthScoreInline(admin.StackedInline):
    model = HealthScore
    extra = 0


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = (
        "org_slug",
        "repo_slug",
        "language",
        "rating_value",
        "likes",
        "hearts",
        "diamonds",
        "health_score",
        "last_scanned_at",
    )
    search_fields = ("org_slug", "repo_slug")
    list_filter = ("language", "visibility")


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = ("repository", "status", "triggered_by", "created_at", "finished_at")
    list_filter = ("status", "triggered_by")
    inlines = [HealthScoreInline, FindingInline]


@admin.register(MetricSample)
class MetricSampleAdmin(admin.ModelAdmin):
    pass
