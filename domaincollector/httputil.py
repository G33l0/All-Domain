"""Shared HTTP helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import aiohttp

CHUNK_SIZE = 64 * 1024


async def read_capped(response: "aiohttp.ClientResponse", max_bytes: int) -> bytes:
    """Read at most *max_bytes* of the body.

    ``StreamReader.read(n)`` returns whatever happens to be buffered, which is
    usually far less than *n*, so a single call truncates large downloads.
    This helper keeps pulling chunks until the cap or the end of the body.
    """
    if max_bytes <= 0:
        return b""
    buffer = bytearray()
    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) >= max_bytes:
            return bytes(buffer[:max_bytes])
    return bytes(buffer)
