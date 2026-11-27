"""Use the system DNS resolver for the ariston library's HTTP requests.

The ``ariston`` library opens a brand new ``aiohttp.ClientSession()`` for every
request. With ``aiodns`` installed (as in Home Assistant), aiohttp then uses the
c-ares resolver, which can raise "Timeout while contacting DNS servers" even
when the system resolver works fine.

This swaps, only inside the ``ariston.ariston_api`` module, ``aiohttp`` for a thin
proxy whose ``ClientSession`` uses ``ThreadedResolver`` (libc ``getaddrinfo``).
Nothing else in Home Assistant is affected.
"""

from __future__ import annotations

import logging

import aiohttp
from ariston import ariston_api

_LOGGER = logging.getLogger(__name__)


class _AiohttpWithSystemResolver:
    """Proxy for the aiohttp module: same API, ClientSession uses ThreadedResolver."""

    def __getattr__(self, name: str):
        return getattr(aiohttp, name)

    @staticmethod
    def ClientSession(*args, **kwargs):  # noqa: N802 - mimics aiohttp.ClientSession
        """Create a ClientSession that resolves hostnames via the system resolver."""
        if "connector" not in kwargs:
            kwargs["connector"] = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver()
            )
        return aiohttp.ClientSession(*args, **kwargs)


def apply_system_resolver() -> None:
    """Patch the ariston library to use the system DNS resolver (idempotent)."""
    if isinstance(ariston_api.aiohttp, _AiohttpWithSystemResolver):
        return
    ariston_api.aiohttp = _AiohttpWithSystemResolver()
    _LOGGER.debug("ariston library patched to use the system DNS resolver")
