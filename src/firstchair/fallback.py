"""Which provider failures let another named provider take over a stage."""

_TRANSIENT_CODES = frozenset({
    "rate_limited",
    "provider_unavailable",
    "provider_timeout",
})


def is_transient(code: str) -> bool:
    """True when another named provider may take over the unfinished stage."""
    return code in _TRANSIENT_CODES
