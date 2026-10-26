"""Shared list-page helpers: safe GET parsing and pagination.

Every CarbonPilot list page funnels its (already company-scoped) queryset
through :func:`paginate` and, when it has filters, through the shared filter
bar partial ``partials/list_filters.html``. Keeping the parsing here means
invalid page numbers, unknown page sizes and out-of-range pages behave the
same everywhere, and pagination links never drop the active filters.

Security note
-------------
These helpers never touch tenant scoping: the caller must hand in a queryset
that is already restricted to the caller's company (and role). They only
read, validate and clamp client-supplied GET parameters.
"""

from __future__ import annotations

from django.core.paginator import EmptyPage, InvalidPage, Paginator

#: Default number of rows on a list page.
DEFAULT_PAGE_SIZE = 10

#: Page sizes a user may pick from the pagination footer.
PAGE_SIZE_CHOICES = (10, 20, 50)

#: Hard upper bound so a crafted ``?per_page=100000`` cannot exhaust the DB.
MAX_PAGE_SIZE = 100


def as_int(value):
    """Return ``value`` as an ``int``, or ``None`` when it is not an integer.

    Never raises: any garbage (``""``, ``"abc"``, ``None``, ``"1.5"``) maps to
    ``None`` so invalid filter/page values are simply ignored.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_page_size(request, *, default=DEFAULT_PAGE_SIZE, choices=PAGE_SIZE_CHOICES):
    """Resolve the requested page size, clamped to the allowed choices."""
    requested = as_int(request.GET.get("per_page"))
    if requested in choices:
        return requested
    if requested and 1 <= requested <= MAX_PAGE_SIZE:
        return requested
    return default


def base_query(request, *, drop=("page", "per_page")):
    """Return the current querystring minus pagination keys.

    Retained for callers that build links by hand; templates rendered with a
    request can use Django's built-in ``{% querystring %}`` tag instead.
    """
    params = request.GET.copy()
    for key in drop:
        params.pop(key, None)
    return params.urlencode()


def paginate(request, queryset, *, per_page=None, page_size_options=PAGE_SIZE_CHOICES):
    """Paginate ``queryset`` with graceful handling of invalid input.

    Returns a dictionary (so templates keep using ``pagination.items`` etc.):

    ``items`` — the current page's rows as a list
    ``page`` / ``pages`` / ``total`` — current page, page count, row count
    ``has_previous`` / ``previous_page`` / ``has_next`` / ``next_page``
    ``per_page`` / ``page_size_options`` — for the page-size selector
    ``page_range`` — elided page numbers for a numbered pager
    """
    size = per_page or parse_page_size(request)
    paginator = Paginator(queryset, size)

    requested = max(as_int(request.GET.get("page")) or 1, 1)
    try:
        page = paginator.page(requested)
    except InvalidPage:  # EmptyPage / PageNotAnInteger — clamp to the last page.
        page = paginator.page(paginator.num_pages or 1)

    return {
        "items": list(page.object_list),
        "page": page.number,
        "pages": paginator.num_pages or 1,
        "total": paginator.count,
        "per_page": size,
        "page_size_options": tuple(page_size_options),
        "has_previous": page.has_previous(),
        "has_next": page.has_next(),
        "previous_page": page.previous_page_number() if page.has_previous() else None,
        "next_page": page.next_page_number() if page.has_next() else None,
        "page_range": list(
            paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1)
        ),
    }
