"""`web_search` 内置工具（详细设计 2.5 / 4.2.4 第 4 条）。

- **无 API Key 时返回 `TOOL_DISABLED`**（2.5 明确约定）：`WEB_SEARCH_PROVIDER` 或
  `WEB_SEARCH_API_KEY` 未配置即不可用，Run 不挂起；
- 出网前过沙箱网络闸门：必须 `allow_network=true`、命中 `allowed_hosts`、且非回环/内网地址
  （4.2.4 第 4 条，防 SSRF）；
- 支持 `tavily` / `serper` 两个后端（2.5 的 `WEB_SEARCH_PROVIDER`），都走 `httpx.AsyncClient`；
  测试通过 `client_factory` 注入 `MockTransport`（9.2：禁止真实外网）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.core.enums import PermissionLevel
from app.core.errors import ToolDisabledError, ToolExecutionFailedError, ToolInvalidArgumentsError
from app.runtime.tools import sandbox
from app.runtime.tools.base import BaseTool, ToolContext, ToolPermissionConfig, ToolResult

PROVIDER_ENDPOINTS: Mapping[str, str] = {
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
}
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10

ClientFactory = Callable[[float], httpx.AsyncClient]


class WebSearchTool(BaseTool):
    """4.2.4 的 `web_search`。"""

    name = "web_search"
    display_name = "联网搜索"
    description = (
        "调用外部搜索服务检索网页，返回标题、链接与摘要。适合查询最新事实或训练数据之外的信息；"
        "若平台未配置搜索服务，调用会被拒绝。"
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，尽量具体，例如 FastAPI 0.115 release notes",
                "minLength": 1,
                "maxLength": 400,
            },
            "max_results": {
                "type": "integer",
                "description": f"返回条数（默认 {DEFAULT_MAX_RESULTS}，最多 {MAX_RESULTS_LIMIT}）",
                "minimum": 1,
                "maximum": MAX_RESULTS_LIMIT,
                "default": DEFAULT_MAX_RESULTS,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    output_schema: Mapping[str, Any] = {"type": "object", "properties": {"results": {"type": "array"}}}
    default_permission = ToolPermissionConfig(
        level=PermissionLevel.GUARDED,
        allow_network=True,
        allowed_hosts=sorted(httpx.URL(url).host or "" for url in PROVIDER_ENDPOINTS.values()),
        timeout_seconds=20.0,
        max_calls_per_run=10,
    )

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    async def run(self, ctx: ToolContext, *, query: str = "", max_results: int | None = None, **_: Any) -> ToolResult:
        text = (query or "").strip()
        if not text:
            raise ToolInvalidArgumentsError("`query` must not be empty", details={"query": query})

        limit = DEFAULT_MAX_RESULTS if max_results is None else int(max_results)
        if not 1 <= limit <= MAX_RESULTS_LIMIT:
            raise ToolInvalidArgumentsError(
                f"`max_results` must be between 1 and {MAX_RESULTS_LIMIT}",
                details={"max_results": max_results},
            )

        provider, api_key, endpoint = _resolve_backend(ctx.settings)
        sandbox.ensure_network_allowed(
            httpx.URL(endpoint).host or "",
            allow_network=ctx.allow_network,
            allowed_hosts=ctx.allowed_hosts,
        )

        async with self._client_factory(ctx.timeout_seconds) as client:
            try:
                response = await client.post(
                    endpoint,
                    json=_request_payload(provider, text, limit, api_key),
                    headers=_request_headers(provider, api_key),
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ToolExecutionFailedError(
                    f"Search provider '{provider}' returned HTTP {exc.response.status_code}",
                    details={"provider": provider, "status_code": exc.response.status_code},
                ) from exc
            except httpx.HTTPError as exc:
                raise ToolExecutionFailedError(
                    f"Search request failed: {type(exc).__name__}",
                    details={"provider": provider},
                ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolExecutionFailedError(
                "Search provider returned non-JSON response", details={"provider": provider}
            ) from exc

        results = _parse_results(provider, payload)[:limit]
        return ToolResult(
            content={"query": text, "provider": provider, "results": results},
            meta={"provider": provider, "hit_count": len(results)},
        )


def _default_client_factory(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _resolve_backend(settings: Settings) -> tuple[str, str, str]:
    """按 2.5 的配置解析后端；未配置 → `TOOL_DISABLED`（不是 500）。"""
    provider = (settings.web_search_provider or "").strip().lower()
    api_key = (settings.web_search_api_key or "").strip()
    if not provider or not api_key:
        raise ToolDisabledError(
            "web_search is not configured (set WEB_SEARCH_PROVIDER and WEB_SEARCH_API_KEY)",
            details={"provider": provider or None, "has_api_key": bool(api_key)},
        )
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if endpoint is None:
        raise ToolDisabledError(
            f"Unsupported WEB_SEARCH_PROVIDER '{provider}'",
            details={"provider": provider, "supported": sorted(PROVIDER_ENDPOINTS)},
        )
    return provider, api_key, endpoint


def _request_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "serper":
        return {"X-API-KEY": api_key, "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}


def _request_payload(provider: str, query: str, limit: int, api_key: str) -> dict[str, Any]:
    if provider == "serper":
        return {"q": query, "num": limit}
    return {"api_key": api_key, "query": query, "max_results": limit}


def _parse_results(provider: str, payload: Any) -> list[dict[str, str]]:
    """把两家后端的响应归一为 `[{title, url, snippet}]`。"""
    if not isinstance(payload, Mapping):
        return []
    items = payload.get("organic" if provider == "serper" else "results")
    if not isinstance(items, Sequence):
        return []
    results: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("link") or item.get("url") or ""),
                "snippet": str(item.get("snippet") or item.get("content") or ""),
            }
        )
    return results
