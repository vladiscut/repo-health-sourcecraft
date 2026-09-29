from django.urls import path

from health import account, views

app_name = 'health'

urlpatterns = [
    path("", views.RepoListView.as_view(), name="repo-list"),
    path("auth/yandex/", account.YandexLoginView.as_view(), name="yandex-login"),
    path("auth/yandex/callback/", account.YandexCallbackView.as_view(), name="yandex-callback"),
    path("auth/logout/", account.YandexLogoutView.as_view(), name="logout"),
    path("me/", account.MyReposView.as_view(), name="my-repos"),
    path("me/pat/", account.SaveSourcecraftPatView.as_view(), name="save-pat"),
    path("me/pat/revoke/", account.RevokeSourcecraftPatView.as_view(), name="revoke-pat"),
    path("me/refresh/", account.RefreshMyReposView.as_view(), name="refresh-my-repos"),
    path(
        "me/repos/<str:org_slug>/<str:repo_slug>/analyze/",
        account.AnalyzeMyRepoView.as_view(),
        name="analyze-my-repo",
    ),
    path(
        "repos/<str:org_slug>/<str:repo_slug>/",
        views.RepoDetailView.as_view(),
        name="repo-detail",
    ),
    path(
        "repos/<str:org_slug>/<str:repo_slug>/rescan/",
        views.RepoRescanView.as_view(),
        name="repo-rescan",
    ),
    path(
        "repos/<str:org_slug>/<str:repo_slug>/ai-summary/",
        views.RepoAiSummaryView.as_view(),
        name="repo-ai-summary",
    ),
    path(
        "repos/<str:org_slug>/<str:repo_slug>/scan-status/",
        views.RepoScanStatusView.as_view(),
        name="repo-scan-status",
    ),
    path(
        "repos/<str:org_slug>/<str:repo_slug>/export/<str:fmt>/",
        views.RepoExportView.as_view(),
        name="repo-export",
    ),
]
