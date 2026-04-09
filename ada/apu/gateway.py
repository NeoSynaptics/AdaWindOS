"""LLM Gateway — routes inference to cloud API (DeepSeek / OpenAI-compatible).

AdaWindOS: Replaces the GPU-based APU gateway with a lightweight cloud gateway.
No Ollama, no GPU, no model loading. Just HTTP calls to a cloud API.

All LLM calls in Ada go through this gateway. The rest of the codebase
doesn't know or care whether it's local or cloud.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("ada.gateway")


class CloudGateway:
    """Cloud LLM gateway — OpenAI-compatible API (DeepSeek, OpenRouter, etc.)."""

    def __init__(
        self,
        api_base: str = "https://api.deepseek.com/v1",
        api_key: str = "",
        default_model: str = "deepseek-chat",
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.default_model = default_model
        self._call_count = 0
        self._total_tokens = 0

        if not self.api_key:
            log.warning("No API key set — set DEEPSEEK_API_KEY env var or pass api_key")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        model: str = "",
        messages: list[dict] = None,
        format: dict | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = 30.0,
        gpu=None,  # ignored, kept for API compat
    ) -> dict:
        """Send a chat completion request to the cloud API.

        Returns a dict matching the OpenAI response format.
        """
        model = model or self.default_model
        messages = messages or []

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # JSON Schema enforcement via response_format
        if format is not None:
            payload["response_format"] = {"type": "json_object"}

        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

                self._call_count += 1
                usage = data.get("usage", {})
                self._total_tokens += usage.get("total_tokens", 0)

                duration = time.time() - start
                log.debug(
                    f"Gateway: {model} chat completed in {duration:.2f}s "
                    f"(tokens={usage.get('total_tokens', 0)})"
                )

                # Normalize to Ollama-like format for compatibility
                return {
                    "message": {
                        "content": data["choices"][0]["message"]["content"],
                        "role": "assistant",
                    },
                    "eval_count": usage.get("completion_tokens", 0),
                    "prompt_eval_count": usage.get("prompt_tokens", 0),
                }

        except httpx.TimeoutException:
            log.error(f"Gateway: {model} chat timed out ({timeout}s)")
            raise APUInferenceError(f"Chat timed out after {timeout}s")
        except httpx.ConnectError:
            log.error(f"Gateway: cannot connect to {self.api_base}")
            raise APUInferenceError(f"Cannot connect to API at {self.api_base}")
        except httpx.HTTPStatusError as e:
            log.error(f"Gateway: API returned {e.response.status_code}: {e.response.text[:200]}")
            raise APUInferenceError(f"API error {e.response.status_code}")

    async def chat_response(
        self,
        model: str = "",
        messages: list[dict] = None,
        format: dict | None = None,
        temperature: float = 0.7,
        timeout: float = 30.0,
        gpu=None,
    ) -> str:
        """Convenience: chat and return just the response text."""
        data = await self.chat(model, messages, format=format,
                               temperature=temperature, timeout=timeout)
        try:
            return data["message"]["content"]
        except (KeyError, TypeError):
            raise APUInferenceError(f"Unexpected response format from {model}")

    async def chat_stream(
        self,
        model: str = "",
        messages: list[dict] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        gpu=None,
    ):
        """Streaming chat — yields text chunks. OpenAI SSE format."""
        model = model or self.default_model
        messages = messages or []

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.api_base}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            return
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                yield token
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        except httpx.TimeoutException:
            raise APUInferenceError(f"Chat stream timed out after {timeout}s")

    async def chat_json(
        self,
        model: str = "",
        messages: list[dict] = None,
        schema: dict = None,
        temperature: float = 0.0,
        timeout: float = 30.0,
        gpu=None,
    ) -> dict:
        """Convenience: chat with JSON response, return parsed dict."""
        # Add JSON instruction to the last message if schema provided
        if schema and messages:
            messages = list(messages)  # copy
            last = messages[-1].copy()
            last["content"] = last["content"] + "\n\nRespond with valid JSON only."
            messages[-1] = last

        data = await self.chat(model, messages, format=schema,
                               temperature=temperature, timeout=timeout)
        try:
            content = data["message"]["content"]
            # Strip markdown code fences if present
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            return json.loads(content)
        except json.JSONDecodeError as e:
            log.error(f"Gateway: {model} returned invalid JSON: {content[:200]}")
            raise APUInferenceError(f"Invalid JSON from {model}: {e}")

    async def generate(
        self,
        model: str = "",
        prompt: str = "",
        timeout: float = 30.0,
        gpu=None,
    ) -> str:
        """Simple generate (non-chat) via chat endpoint."""
        return await self.chat_response(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )

    def metrics(self) -> dict:
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
            "backend": "cloud",
            "api_base": self.api_base,
        }


# Keep the same exception name for compatibility
class APUInferenceError(Exception):
    """Raised when an inference call fails."""
    pass


# Backward compatibility alias
APUGateway = CloudGateway
