"""HTML-страницы рейтинга, карточки и выгрузки отчёта."""

from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import F, Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView

from health.models import Repository, Scan, UserRepositoryAccess
from health.scoring import present_scores
from health.tasks import task_scan_user_repository
from health.user_repository import user_can_access_repository


PAGE_SIZE = 50


SORTS = {
    "score": (
        F("health_score").desc(nulls_last=True),
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


def _present(scan: Scan | None) -> dict | None:
    totals = _score_map(scan)
    if not totals:
        return None
    presented = present_scores(totals)
    if presented["total"] is None:
        return None
    return presented


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
    return user_can_access_repository(request.user, repo)


class RepositoryAccessMixin:
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
                "total_count": page.paginator.count,
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        repo = self.object
        scan = repo.latest_completed_scan()
        history = []
        for item in (
            repo.scans
            .filter(status__in=[Scan.Status.SUCCESS, Scan.Status.PARTIAL])
            .prefetch_related("scores")[:12]
        ):
            presented = _present(item)
            history.append(
                {
                    "finished_at": item.finished_at,
                    "total": presented["total"] if presented else None,
                }
            )
        if repo.visibility == Repository.VisibilityType.PUBLIC:
            can_analyze = (
                self.request.user.is_authenticated
                and UserRepositoryAccess.objects.filter(
                    user=self.request.user,
                    repository=repo,
                ).exists()
            )
        else:
            can_analyze = True
        context.update(
            {
                "scan": scan,
                "score": _present(scan),
                "findings": scan.findings.all() if scan else [],
                "history": history,
                "can_analyze": can_analyze,
            }
        )
        return context


class RepoRescanView(RepositoryAccessMixin, View):
    http_method_names = ["post"]

    def post(self, request, org_slug, repo_slug):
        repo = self.get_repository()
        task_scan_user_repository.delay(repo.id)
        messages.info(request, "Проверка поставлена в очередь.")
        return redirect(
            "health:repo-detail",
            org_slug=org_slug,
            repo_slug=repo_slug,
        )


class RepoExportView(RepositoryAccessMixin, View):
    http_method_names = ["get"]

    def get(self, request, org_slug, repo_slug, fmt):
        self.get_repository()
        if fmt not in {"md", "pdf"}:
            raise Http404("Неизвестный формат")

        filename = _safe_filename(org_slug, repo_slug)
        if fmt == "pdf":
            response = HttpResponse(b"", content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
            return response

        response = HttpResponse(b"", content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}.md"'
        return response
