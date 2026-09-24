from django.urls import path

from health import account, views

app_name = 'health'

urlpatterns = [
    path("", views.RepoListView.as_view(), name="repo-list"),
    path("auth/yandex/", account.yandex_login, name="yandex-login"),
    path("auth/yandex/callback/", account.yandex_callback, name="yandex-callback"),
    path("auth/logout/", account.yandex_logout, name="logout"),
    path("me/", account.my_repos, name="my-repos"),
    path("me/pat/", account.save_sourcecraft_pat, name="save-pat"),
    path("me/pat/revoke/", account.revoke_sourcecraft_pat, name="revoke-pat"),
    path("me/refresh/", account.refresh_my_repos, name="refresh-my-repos"),
    path(
        "me/repos/<str:org_slug>/<str:repo_slug>/analyze/",
        account.analyze_my_repo,
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
        "repos/<str:org_slug>/<str:repo_slug>/export/<str:fmt>/",
        views.RepoExportView.as_view(),
        name="repo-export",
    ),
]
