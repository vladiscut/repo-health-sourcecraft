"""Вход через Я ID и личный кабинет /me/."""

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from health.models import Profile, Repository, Scan, UserRepositoryAccess
from health.user_repository import (
    sourcecraft_token_works,
    sync_user_repositories,
    upsert_yandex_user,
    user_can_access_repository,
)
from health.tasks import task_scan_user_repository
from health.views import _present, _safe_next_url
from integrations.sourcecraft import SourceCraftError
from integrations.yandex import (
    YandexOAuthError,
    build_authorization_url,
    exchange_code,
    fetch_user_info,
    is_configured,
)


def _profile(user) -> Profile | None:
    return getattr(user, "profile", None)


@require_GET
def yandex_login(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("health:my-repos")
    if not is_configured():
        messages.error(
            request,
            "Я ID не настроен. Добавьте YANDEX_CLIENT_ID и YANDEX_CLIENT_SECRET в .env.",
        )
        return redirect("health:repo-list")
    try:
        url, state, verifier = build_authorization_url()
    except YandexOAuthError as exc:
        messages.error(request, str(exc))
        return redirect("health:repo-list")
    request.session["yandex_oauth_state"] = state
    request.session["yandex_oauth_verifier"] = verifier
    return redirect(url)


@require_GET
def yandex_callback(request: HttpRequest) -> HttpResponse:
    if request.GET.get("error"):
        messages.error(request, "Вход через Я ID отменён.")
        return redirect("health:repo-list")

    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    expected = request.session.pop("yandex_oauth_state", "")
    verifier = request.session.pop("yandex_oauth_verifier", "")
    if not code or not expected or state != expected:
        messages.error(request, "Некорректный ответ Я ID. Попробуйте войти ещё раз.")
        return redirect("health:repo-list")

    try:
        tokens = exchange_code(code, verifier)
        info = fetch_user_info(tokens["access_token"])
        user = upsert_yandex_user(info, tokens)
    except (YandexOAuthError, KeyError, ValueError) as exc:
        messages.error(request, f"Не удалось войти через Я ID: {exc}")
        return redirect("health:repo-list")

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    profile = user.profile
    ok, username = sourcecraft_token_works(profile.sourcecraft_token or "")
    if ok:
        if username:
            profile.sourcecraft_username = username
            profile.save(update_fields=["sourcecraft_username"])
        try:
            count = sync_user_repositories(profile)
            messages.success(request, f"Вход выполнен. Найдено репозиториев: {count}.")
        except SourceCraftError as exc:
            messages.warning(request, f"Вошли, но список репозиториев не подтянулся: {exc}")
    else:
        messages.info(
            request,
            "Вошли через Я ID. Для своих репозиториев нужен PAT SourceCraft.",
        )
    return redirect("health:my-repos")


@require_POST
def yandex_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    messages.info(request, "Вы вышли из аккаунта.")
    return redirect("health:repo-list")


@login_required
def my_repos(request: HttpRequest) -> HttpResponse:
    profile = _profile(request.user)
    if profile is None:
        messages.error(request, "Профиль Я ID не найден. Войдите ещё раз.")
        return redirect("health:repo-list")
    access = (
        UserRepositoryAccess.objects.filter(user=request.user)
        .select_related("repository")
        .prefetch_related(
            Prefetch(
                "repository__scans",
                queryset=(
                    Scan.objects
                    .filter(status__in=[Scan.Status.SUCCESS, Scan.Status.PARTIAL])
                    .prefetch_related("scores")
                    .order_by("-created_at")
                ),
            )
        )
        .order_by("repository__org_slug", "repository__repo_slug")
    )
    items = []
    for row in access:
        scan = next(iter(row.repository.scans.all()), None)
        items.append({"repo": row.repository, "scan": scan, "score": _present(scan)})
    return render(
        request,
        "health/my_repos.html",
        {
            "profile": profile,
            "items": items,
            "has_pat": bool(profile.sourcecraft_pat),
        },
    )


@login_required
@require_POST
def save_sourcecraft_pat(request: HttpRequest) -> HttpResponse:
    profile = _profile(request.user)
    if profile is None:
        messages.error(request, "Профиль Я ID не найден. Войдите ещё раз.")
        return redirect("health:repo-list")
    token = (request.POST.get("sourcecraft_pat") or "").strip()
    if not token:
        messages.error(request, "Вставьте персональный токен SourceCraft.")
        return redirect("health:my-repos")
    ok, username = sourcecraft_token_works(token)
    if not ok:
        messages.error(request, "SourceCraft не принял токен. Проверьте PAT и права.")
        return redirect("health:my-repos")
    profile.sourcecraft_pat = token
    if username:
        profile.sourcecraft_username = username
    profile.save(update_fields=["sourcecraft_pat", "sourcecraft_username"])
    try:
        count = sync_user_repositories(profile)
        messages.success(request, f"Токен сохранён. Доступно репозиториев: {count}.")
    except SourceCraftError as exc:
        messages.warning(request, f"Токен сохранён, но список репозиториев не подтянулся: {exc}")
    return redirect("health:my-repos")


@login_required
@require_POST
def revoke_sourcecraft_pat(request: HttpRequest) -> HttpResponse:
    profile = _profile(request.user)
    if profile is None:
        messages.error(request, "Профиль Я ID не найден. Войдите ещё раз.")
        return redirect("health:repo-list")
    profile.sourcecraft_pat = None
    profile.save(update_fields=["sourcecraft_pat"])
    UserRepositoryAccess.objects.filter(user=request.user).delete()
    messages.info(request, "Токен отозван. Список репозиториев очищен.")
    return redirect("health:my-repos")


@login_required
@require_POST
def refresh_my_repos(request: HttpRequest) -> HttpResponse:
    profile = _profile(request.user)
    if profile is None or not profile.sourcecraft_token:
        messages.error(request, "Сначала сохраните PAT SourceCraft.")
        return redirect("health:my-repos")
    try:
        count = sync_user_repositories(profile)
        messages.success(request, f"Список обновлён: {count} репозиториев.")
    except SourceCraftError as exc:
        messages.error(request, f"Не удалось получить репозитории: {exc}")
    return redirect("health:my-repos")


@login_required
@require_POST
def analyze_my_repo(request: HttpRequest, org_slug: str, repo_slug: str) -> HttpResponse:
    repo = get_object_or_404(Repository, org_slug=org_slug, repo_slug=repo_slug)
    if repo.visibility == Repository.VisibilityType.PUBLIC:
        allowed = UserRepositoryAccess.objects.filter(
            user=request.user,
            repository=repo,
        ).exists()
    else:
        allowed = user_can_access_repository(request.user, repo)
    if not allowed:
        raise Http404()
    Scan.objects.create(
        repository=repo,
        status=Scan.Status.PENDING,
        triggered_by=Scan.TriggeredBy.USER,
        triggered_by_user=request.user,
    )
    task_scan_user_repository.delay(repo.id)
    messages.info(
        request,
        "Анализ поставлен в очередь. Сбор метрик пока не подключён — это заготовка.",
    )
    fallback = reverse("health:repo-detail", args=[org_slug, repo_slug])
    return redirect(_safe_next_url(request, fallback))
