"""HTML-страницы рейтинга, карточки и выгрузки отчёта."""

from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import F, Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView

from core.celery import SCHEDULE_QUEUE_NAME
from health.access_check import (
    ACCESS_DENIED_TEXT,
    PAGE_UNAVAILABLE_TEXT,
    PageAccessDenied,
    PageAccessUnavailable,
    repository_access_for_page,
)
from health.formatters import humanize_number
from health.models import Finding, Repository, Scan
from health.personal_scan import (
    ACCESS_CHECK_TEXT,
    PHASE_TEXT,
    enqueue_personal_scan,
    user_has_saved_access,
)
from health.repo_ordering import annotate_visible_score
from health.reports import build_markdown_report, with_overall_impacts
from health.score_breakdown import rows_for_scores
from health.scoring import CATEGORY_LABELS, present_scores
from health.tasks import task_check_and_scan_repository


PAGE_SIZE = 50
STRONG_CATEGORY_THRESHOLD = 80
WEAK_CATEGORY_THRESHOLD = 50


SORTS = {
    "score": (
        F("visible_score").desc(nulls_last=True),
        F("rating_value").desc(nulls_last=True),
        F("last_updated").desc(nulls_last=True),
    ),
    "rating": (
        F("rating_value").desc(nulls_last=True),
        F("last_updated").desc(nulls_last=True),
    ),
    "updated": (
        F("last_updated").desc(nulls_last=True),
        F("rating_value").desc(nulls_last=True),
    ),
    "name": ("org_slug", "repo_slug"),
}


def _score_map(scan: Scan | None) -> dict[str, int | None]:
    if scan is None:
        return {}
    return {row.category: row.total for row in scan.scores.all()}


def _full_totals(scan: Scan | None) -> dict[str, int | None]:
    totals = {key: None for key in CATEGORY_LABELS}
    totals.update(_score_map(scan))
    return totals


def _present(scan: Scan | None) -> dict | None:
    if scan is None:
        return None
    presented = present_scores(_full_totals(scan))
    if presented["total"] is None and not _score_map(scan):
        return None
    return presented


def _active_scan(repo: Repository) -> Scan | None:
    return (
        repo.scans.filter(status__in=Scan.ACTIVE_STATUSES)
        .order_by("-created_at")
        .first()
    )


def _failure_notice(repo: Repository, shown: Scan | None) -> str:
    failed = (
        repo.scans.filter(status=Scan.Status.FAILED)
        .order_by("-created_at")
        .first()
    )
    if failed is None or not failed.error:
        return ""
    if shown is not None and failed.created_at < shown.created_at:
        return ""
    return failed.error


def _strengths_and_weaknesses(scan: Scan | None, presented: dict | None):
    strengths: list[str] = []
    weaknesses: list[str] = []
    if presented is None:
        return strengths, weaknesses

    for label, value in presented["categories"]:
        if value is None:
            weaknesses.append(f"{label}: нет данных")
        elif value >= STRONG_CATEGORY_THRESHOLD:
            strengths.append(f"{label}: {value}")
        elif value < WEAK_CATEGORY_THRESHOLD:
            weaknesses.append(f"{label}: {value}")

    if scan is not None:
        for finding in scan.findings.filter(
            severity__in=[Finding.Severity.HIGH, Finding.Severity.CRITICAL],
        )[:5]:
            weaknesses.append(finding.title)

    return strengths, weaknesses


def _list_query(**params: str) -> str:
    clean = {key: value for key, value in params.items() if value}
    return urlencode(clean)


def _page_url(list_query: str, number: int) -> str:
    params = f"{list_query}&page={number}" if list_query else f"page={number}"
    return f"?{params}"


def _page_links(page, list_query: str) -> list[dict]:
    links = []
    for item in page.paginator.get_elided_page_range(
        page.number,
        on_each_side=2,
        on_ends=1,
    ):
        if item == page.paginator.ELLIPSIS:
            links.append({"ellipsis": True})
            continue
        links.append(
            {
                "number": item,
                "current": item == page.number,
                "href": _page_url(list_query, item),
            }
        )
    return links


def _safe_filename(org_slug: str, repo_slug: str) -> str:
    raw = f"{org_slug}-{repo_slug}-health"
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in raw
    )
    return cleaned[:180] or "repo-health"


def _opened_from_personal_list(request: HttpRequest) -> bool:
    return request.GET.get("from") == "me" or request.POST.get("from") == "me"


def _repo_detail_redirect(request: HttpRequest, org_slug: str, repo_slug: str):
    url = reverse(
        "health:repo-detail",
        kwargs={"org_slug": org_slug, "repo_slug": repo_slug},
    )
    if _opened_from_personal_list(request):
        url = f"{url}?from=me"
    return redirect(url)


def _safe_next_url(request: HttpRequest, fallback: str) -> str:
    candidate = request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback


def public_languages() -> list[str]:
    return list(
        Repository.objects.filter(visibility=Repository.VisibilityType.PUBLIC)
        .exclude(language__isnull=True)
        .exclude(language="")
        .values_list("language", flat=True)
        .distinct()
        .order_by("language")
    )


def can_view_repository(request: HttpRequest, repo: Repository) -> bool:
    if repo.visibility == Repository.VisibilityType.PUBLIC:
        return True
    if not request.user.is_authenticated:
        return False
    # Проверка уже в очереди: карточку показывает лоадер, SourceCraft спрашивает воркер.
    if (
        user_has_saved_access(request.user, repo)
        and _active_scan(repo) is not None
    ):
        return True
    decision = repository_access_for_page(request.user, repo)
    if decision == "unavailable":
        raise PageAccessUnavailable()
    if decision == "denied":
        raise PageAccessDenied()
    return decision == "ok"


class RepositoryAccessMixin:
    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except PageAccessDenied:
            messages.error(request, ACCESS_DENIED_TEXT)
            return redirect("health:my-repos")
        except PageAccessUnavailable:
            messages.error(request, PAGE_UNAVAILABLE_TEXT)
            return redirect("health:my-repos")

    def get_repository(self) -> Repository:
        repo = get_object_or_404(
            Repository,
            org_slug=self.kwargs["org_slug"],
            repo_slug=self.kwargs["repo_slug"],
        )
        if not can_view_repository(self.request, repo):
            raise Http404()
        return repo


class RepoListView(ListView):
    template_name = "health/repo_list.html"
    paginate_by = PAGE_SIZE

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.query = request.GET.get("q", "").strip()
        self.language = request.GET.get("lang", "").strip()
        sort = request.GET.get("sort", "score")
        self.selected_sort = sort if sort in SORTS else "score"

    def get_queryset(self):
        repos = Repository.objects.filter(
            visibility=Repository.VisibilityType.PUBLIC,
        )
        if self.query:
            repos = repos.filter(
                Q(org_slug__icontains=self.query)
                | Q(repo_slug__icontains=self.query)
                | Q(description__icontains=self.query)
            )
        if self.language:
            repos = repos.filter(language=self.language)
        if self.selected_sort == "score":
            repos = annotate_visible_score(repos)
        return repos.order_by(*SORTS[self.selected_sort]).prefetch_related(
            Prefetch(
                "scans",
                queryset=(
                    Scan.objects
                    .filter(status__in=[Scan.Status.SUCCESS, Scan.Status.PARTIAL])
                    .prefetch_related("scores")
                    .order_by("-created_at")
                ),
            )
        )

    def paginate_queryset(self, queryset, page_size):
        paginator = self.get_paginator(queryset, page_size)
        page = paginator.get_page(self.request.GET.get("page"))
        return paginator, page, page.object_list, page.has_other_pages()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page = context["page_obj"]
        list_query = _list_query(
            q=self.query,
            lang=self.language,
            sort=self.selected_sort,
        )
        items = []
        for repo in page:
            scan = next(iter(repo.scans.all()), None)
            items.append(
                {
                    "repo": repo,
                    "scan": scan,
                    "score": _present(scan),
                }
            )
        context.update(
            {
                "items": items,
                "page": page,
                "page_links": _page_links(page, list_query),
                "prev_url": (
                    _page_url(list_query, page.previous_page_number())
                    if page.has_previous()
                    else ""
                ),
                "next_url": (
                    _page_url(list_query, page.next_page_number())
                    if page.has_next()
                    else ""
                ),
                "query": self.query,
                "language": self.language,
                "languages": public_languages(),
                "sort": self.selected_sort,
                "total_count": humanize_number(page.paginator.count),
                "filters_active": bool(
                    self.query or self.language or self.selected_sort != "score"
                ),
            }
        )
        return context


class RepoDetailView(RepositoryAccessMixin, DetailView):
    model = Repository
    template_name = "health/repo_detail.html"
    context_object_name = "repo"

    def get_object(self, queryset=None):
        return self.get_repository()

    def get(self, request, *args, **kwargs):
        if request.GET.get("scan", "").lower() in {"1", "true", "yes"}:
            repo = self.get_repository()
            task_check_and_scan_repository.apply_async(
                kwargs={
                    "repository_id": repo.id,
                    "user_id": None,
                    "force": True,
                },
                queue=SCHEDULE_QUEUE_NAME,
            )
            messages.info(request, "Плановый скан поставлен в очередь.")
            return _repo_detail_redirect(request, repo.org_slug, repo.repo_slug)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        repo = self.object
        active = _active_scan(repo)
        scan_in_progress = active is not None
        scan = None if scan_in_progress else repo.latest_completed_scan()
        presented = None if scan_in_progress else _present(scan)
        strengths, weaknesses = _strengths_and_weaknesses(scan, presented)
        history = []
        for item in (
            repo.scans
            .filter(status__in=[Scan.Status.SUCCESS, Scan.Status.PARTIAL])
            .prefetch_related("scores")[:12]
        ):
            item_presented = _present(item)
            history.append(
                {
                    "finished_at": item.finished_at,
                    "total": item_presented["total"] if item_presented else None,
                }
            )
        can_analyze = user_has_saved_access(self.request.user, repo)
        from_me = _opened_from_personal_list(self.request)
        has_null = bool(
            presented
            and any(value is None for _, value in presented["categories"])
        )
        context.update(
            {
                "scan": scan,
                "score": presented,
                "scan_in_progress": scan_in_progress,
                "scan_status_url": reverse(
                    "health:repo-scan-status",
                    args=[repo.org_slug, repo.repo_slug],
                ),
                "findings": with_overall_impacts(scan) if scan else [],
                "strengths": strengths,
                "weaknesses": weaknesses,
                "has_null_categories": has_null,
                "category_rows": rows_for_scores(scan.scores.all()) if scan else [],
                "analyzed_at": scan.finished_at if scan else None,
                "history": history,
                "can_analyze": can_analyze,
                "scan_phase_text": PHASE_TEXT.get(active.status, "") if active else "",
                "phase_texts": PHASE_TEXT,
                "scan_notice": "" if scan_in_progress else _failure_notice(repo, scan),
                "from_me": from_me,
                "back_url": (
                    reverse("health:my-repos")
                    if from_me
                    else reverse("health:repo-list")
                ),
            }
        )
        return context


class RepoRescanView(RepositoryAccessMixin, View):
    http_method_names = ["post"]

    def post(self, request, org_slug, repo_slug):
        repo = get_object_or_404(
            Repository,
            org_slug=org_slug,
            repo_slug=repo_slug,
        )
        result = enqueue_personal_scan(request.user, repo)
        if result == "forbidden":
            raise Http404()
        if result == "active":
            messages.info(request, "Анализ этого репозитория уже идёт.")
        else:
            messages.info(request, ACCESS_CHECK_TEXT)
        return _repo_detail_redirect(request, org_slug, repo_slug)


class RepoScanStatusView(RepositoryAccessMixin, View):
    http_method_names = ["get"]

    def get_repository(self) -> Repository:
        """Статус читается из базы: опрос карточки не ждёт SourceCraft."""

        repo = get_object_or_404(
            Repository,
            org_slug=self.kwargs["org_slug"],
            repo_slug=self.kwargs["repo_slug"],
        )
        if repo.visibility == Repository.VisibilityType.PUBLIC:
            return repo
        user = self.request.user
        if user_has_saved_access(user, repo):
            return repo
        if user.is_authenticated and repo.scans.filter(
            triggered_by_user=user,
            status__in=Scan.ACTIVE_STATUSES,
        ).exists():
            return repo
        raise Http404()

    def get(self, request, org_slug, repo_slug):
        repo = self.get_repository()
        active = _active_scan(repo)
        if active is not None:
            return JsonResponse(
                {
                    "status": active.status,
                    "scan_id": active.id,
                }
            )

        latest = (
            repo.scans.filter(
                status__in=[
                    Scan.Status.SUCCESS,
                    Scan.Status.PARTIAL,
                    Scan.Status.FAILED,
                ]
            )
            .order_by("-created_at")
            .first()
        )
        if latest is None:
            return JsonResponse({"status": "ready", "scan_id": None})
        if latest.status == Scan.Status.FAILED:
            return JsonResponse({"status": "failed", "scan_id": latest.id})
        return JsonResponse({"status": "ready", "scan_id": latest.id})


class RepoExportView(RepositoryAccessMixin, View):
    http_method_names = ["get"]

    def get(self, request, org_slug, repo_slug, fmt):
        repo = self.get_repository()
        if fmt not in {"md", "pdf"}:
            raise Http404("Неизвестный формат")

        if _active_scan(repo) is not None:
            messages.info(
                request,
                "Выгрузка недоступна, пока идёт анализ. Дождитесь завершения.",
            )
            return _repo_detail_redirect(request, org_slug, repo_slug)

        filename = _safe_filename(org_slug, repo_slug)
        if fmt == "pdf":
            response = HttpResponse(b"", content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
            return response

        body = build_markdown_report(repo)
        response = HttpResponse(
            body.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}.md"'
        return response
