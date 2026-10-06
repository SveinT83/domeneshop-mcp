"""Small fixed-origin HTTP client, with retries only for side-effect-free reads."""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


class APIError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


@dataclass
class Reply:
    data: Any
    status: int
    location: str | None = None


class DomeneshopClient:
    def __init__(self, token: str, secret: str, *, transport=None, timeout: float = 20):
        if not token.strip() or not secret.strip():
            raise ValueError("DOMENESHOP_TOKEN and DOMENESHOP_SECRET are required")
        self._http = httpx.AsyncClient(
            base_url="https://api.domeneshop.no/v0/",
            auth=httpx.BasicAuth(token, secret),
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "domeneshop-mcp/0.1.0"},
            follow_redirects=False,
            verify=True,
            trust_env=False,
            transport=transport,
        )

    async def close(self):
        await self._http.aclose()

    async def request(self, method: str, path: str, *, params=None, body=None) -> Reply:
        # Path is constructed by service methods, never accepted from an MCP caller.
        if path.startswith(("/", "http")) or ".." in path or "?" in path or "#" in path:
            raise ValueError("Invalid API path")
        safe_read = method == "GET" and path != "dyndns/update"
        for attempt in range(3 if safe_read else 1):
            try:
                response = await self._http.request(method, path, params=params, json=body)
            except httpx.TransportError:
                # Never expose request headers, credentials or upstream exception text.
                raise APIError(
                    None,
                    "API transport/TLS failure; outcome may be unknown for writes. "
                    "TLS validation is required; inspect connectivity before retrying.",
                ) from None
            if safe_read and response.status_code in {429, 502, 503, 504} and attempt < 2:
                try:
                    delay = min(
                        5.0, max(0.0, float(response.headers.get("Retry-After", 2**attempt)))
                    )
                except ValueError:
                    delay = float(2**attempt)
                await asyncio.sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                descriptions = {
                    401: "Authentication failed",
                    403: "Access denied",
                    404: "Resource not found",
                    409: "Conflict",
                    429: "Rate limited",
                }
                raise APIError(
                    response.status_code,
                    f"Domeneshop HTTP {response.status_code}: "
                    f"{descriptions.get(response.status_code, 'Request failed')}",
                )
            data = None
            if response.content.strip():
                try:
                    data = response.json()
                except ValueError:
                    raise APIError(response.status_code, "API returned invalid JSON") from None
            return Reply(data, response.status_code, response.headers.get("Location"))
        raise AssertionError("Unreachable")

    async def get(self, path: str, **params):
        return (
            await self.request(
                "GET", path, params={k: v for k, v in params.items() if v is not None}
            )
        ).data
