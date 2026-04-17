import json
import logging
from typing import AsyncIterator

import httpx

from backend.core.account_pool import AccountPool
from backend.services.auth_resolver import BASE_URL, AuthResolver
from backend.upstream.payload_builder import build_chat_payload
from backend.upstream.qwen_executor import QwenExecutor
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.client")


class QwenClient:
    def __init__(self, account_pool: AccountPool):
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None
        self.executor = QwenExecutor(self, account_pool)
        
        # 共享 HTTP 客户端，实现连接池复用，大幅提升 TTFT
        self.http_timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
        self.client = httpx.AsyncClient(
            timeout=self.http_timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            http2=True, # 启用 HTTP2 进一步提升流式性能
        )

    @staticmethod
    def _build_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{BASE_URL}/",
            "Origin": BASE_URL,
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }

    async def _request_json(self, method: str, path: str, token: str, body: dict | None = None, timeout: float | None = None) -> dict:
        resp = await self.client.request(
            method,
            f"{BASE_URL}{path}",
            headers=self._build_headers(token),
            json=body,
            timeout=timeout if timeout is not None else self.http_timeout,
        )
        return {"status": resp.status_code, "body": resp.text}

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        return await self.executor.create_chat(token, model, chat_type=chat_type)

    async def delete_chat(self, token: str, chat_id: str):
        await self._request_json("DELETE", f"/api/v2/chats/{chat_id}", token, timeout=20.0)

    async def list_chats(self, token: str, limit: int = 50) -> list[dict]:
        res = await self._request_json("GET", f"/api/v2/chats?limit={limit}", token, timeout=20.0)
        if res["status"] != 200:
            return []
        try:
            data = json.loads(res.get("body", "{}"))
        except Exception:
            return []
        chats = data.get("data", [])
        return chats if isinstance(chats, list) else []

    async def verify_token(self, token: str) -> bool:
        """Verify token validity via direct HTTP (no browser page needed)."""
        if not token:
            return False

        try:
            resp = await self.client.get(
                f"{BASE_URL}/api/v1/auths/",
                headers=self._build_headers(token),
                timeout=15,
            )
            if resp.status_code != 200:
                return False

            try:
                data = resp.json()
                return data.get("role") == "user"
            except Exception as e:
                log.warning(f"[verify_token] JSON 解析失败（可能被拦截或代理异常）: {e}, status={resp.status_code}, text={resp.text[:100]}")
                if "aliyun_waf" in resp.text.lower() or "<!doctype" in resp.text.lower():
                    log.info("[verify_token] 遇到 WAF 拦截页面，放行交给浏览器自动化账号流程处理。")
                    return True
                return False
        except Exception as e:
            log.warning(f"[verify_token] HTTP 请求异常: {e}")
            return False

    async def list_models(self, token: str) -> list:
        try:
            resp = await self.client.get(
                f"{BASE_URL}/api/models",
                headers=self._build_headers(token),
                timeout=10,
            )
            if resp.status_code != 200:
                return []
            try:
                return resp.json().get("data", [])
            except Exception as e:
                log.warning(f"[list_models] JSON 解析失败: {e}, status={resp.status_code}, text={resp.text[:100]}")
                return []
        except Exception:
            return []

    def _build_payload(self, chat_id: str, model: str, content: str, has_custom_tools: bool = False, files: list[dict] | None = None) -> dict:
        return build_chat_payload(chat_id, model, content, has_custom_tools, files=files)

    def parse_sse_chunk(self, chunk: str) -> list[dict]:
        return parse_sse_chunk(chunk)

    async def stream(
        self,
        token: str,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        thinking_enabled: bool = True,
        thinking_mode: str = "Auto",
        thinking_format: str = "summary",
    ):
        async for event in self.executor.stream(
            token,
            chat_id,
            model,
            content,
            has_custom_tools,
            files=files,
            thinking_enabled=thinking_enabled,
            thinking_mode=thinking_mode,
            thinking_format=thinking_format,
        ):
            yield event

    async def stream_chat_once(self, token: str, chat_id: str, payload: dict) -> AsyncIterator[dict]:
        # 降低read timeout，避免模型卡住时长时间等待
        # connect: 连接超时30秒
        # read: 读取超时120秒（2分钟内没有新数据就超时）
        # write: 写入超时30秒
        # pool: 连接池超时30秒
        async with self.client.stream(
            "POST",
            f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}",
            headers=self._build_headers(token),
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                yield {"status": resp.status_code, "body": await resp.aread()}
                return
            async for chunk in resp.aiter_text():
                if chunk:
                    yield {"chunk": chunk}
            yield {"status": "streamed"}

    async def chat_stream_events_with_retry(
        self,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        fixed_account=None,
        existing_chat_id: str | None = None,
        thinking_enabled: bool = True,
        thinking_mode: str = "Auto",
        thinking_format: str = "summary",
    ):
        async for item in self.executor.chat_stream_events_with_retry(
            model,
            content,
            has_custom_tools,
            files=files,
            fixed_account=fixed_account,
            existing_chat_id=existing_chat_id,
            thinking_enabled=thinking_enabled,
            thinking_mode=thinking_mode,
            thinking_format=thinking_format,
        ):
            yield item

    async def close(self):
        """关闭共享 HTTP 客户端，释放资源。"""
        await self.client.aclose()
