from django.core.cache import cache

from health.docs_scan import CACHE_PREFIX


def run(scan_id: int) -> int:
    # TODO

    cache_key = CACHE_PREFIX + str(scan_id)

    tree = cache.get(cache_key)

    cache.delete(cache_key)
