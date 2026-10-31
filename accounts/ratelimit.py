"""Small cache-based rate limiter for the sign-in endpoints.

Counts attempts per key inside a time window. With several server processes use a shared cache (Redis or
the database cache) so the counts are shared; the default in-memory cache is per process, which is fine for one server.
"""

from django.conf import settings
from django.core.cache import cache


def client_ip(request):
    """The visitor's address. X-Forwarded-For is only believed for the proxies we run (TRUSTED_PROXY_COUNT):
    each of them appends the address it saw, so the real visitor is that many entries from the end."""
    forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    count = settings.TRUSTED_PROXY_COUNT
    if count > 0 and len(forwarded) >= count:
        return forwarded[-count]
    return request.META.get("REMOTE_ADDR", "unknown")


def hit(key, limit, window):
    """Count one attempt. Returns True while still within `limit` per `window` seconds."""
    key = f"rl:{key}"
    if cache.add(key, 1, window):
        return True
    try:
        return cache.incr(key) <= limit
    except ValueError:  # the key expired between add and incr
        cache.set(key, 1, window)
        return True


def blocked(key, limit):
    """Is this key already over the limit (without counting a new attempt)?"""
    return (cache.get(f"rl:{key}") or 0) >= limit


def reset(key):
    cache.delete(f"rl:{key}")
