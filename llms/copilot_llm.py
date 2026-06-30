"""
GitHub Copilot LLM wrapper.
"""

import asyncio
from typing import Any, Dict, Optional

from copilot import CopilotClient, PermissionHandler
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)


class CopilotLLM:
    """Wrapper for GitHub Copilot models."""

    MODEL_PREFIX = "copilot/"

    @staticmethod
    def is_supported(model_name: str) -> bool:
        """Check if this provider supports the given model."""
        return model_name.startswith(CopilotLLM.MODEL_PREFIX)

    @staticmethod
    def initialize(model: str, temperature: float, max_tokens: int):
        """Initialize GitHub Copilot client wrapper."""
        model_name = model.replace(CopilotLLM.MODEL_PREFIX, "", 1)

        class CopilotWrapper:
            def __init__(self, model: str, temperature: float, max_tokens: int):
                self.model = model
                self.temperature = temperature
                self.max_tokens = max_tokens
                self.client: Optional[CopilotClient] = None
                self.session = None
                self._loop = asyncio.new_event_loop()
                self._session_lock = asyncio.Lock()

            async def _ensure_session(self):
                if self.session is not None:
                    return

                async with self._session_lock:
                    if self.session is not None:
                        return

                    self.client = CopilotClient()
                    await self.client.start()
                    self.session = await self.client.create_session({
                        "model": self.model,
                        "on_permission_request": PermissionHandler.approve_all,
                    })

            @retry(
                wait=wait_exponential(multiplier=1, min=20, max=60),
                stop=stop_after_attempt(5),
                retry=retry_if_exception_type(Exception),
                reraise=True,
            )
            async def _ainvoke(self, prompt: str):
                """Single-attempt send; tenacity will retry on exceptions/empty responses.

                This avoids hammering the API and uses exponential backoff for 429s.
                """
                await self._ensure_session()

                # Perform one request per attempt. If the SDK raises (e.g. 429),
                # tenacity will catch and retry according to the decorator.
                response = await self.session.send_and_wait({"prompt": prompt})
                content = self._extract_content(response)
                if not content:
                    # Trigger a retry when Copilot returns an empty/transient payload
                    raise RuntimeError("Empty response from Copilot; retrying")

                return response

            @staticmethod
            def _extract_content(response: Any) -> str:
                data = getattr(response, "data", None)
                if data is not None:
                    content = getattr(data, "content", "")
                    if isinstance(content, str):
                        return content

                content = getattr(response, "content", "")
                if isinstance(content, str):
                    return content

                return ""

            @staticmethod
            def _extract_usage(response: Any) -> Dict[str, int]:
                data = getattr(response, "data", None)
                usage = getattr(data, "usage", None) if data is not None else None

                if isinstance(usage, dict):
                    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
                    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
                    total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
                    return {
                        "input_tokens": int(input_tokens or 0),
                        "output_tokens": int(output_tokens or 0),
                        "total_tokens": int(total_tokens or 0),
                    }

                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                }

            def invoke(self, prompt: str):
                response = self._loop.run_until_complete(self._ainvoke(prompt))
                content = self._extract_content(response)
                usage_metadata = self._extract_usage(response)

                class Response:
                    def __init__(self, text: str, usage_metadata: Dict[str, int]):
                        self.content = text
                        self.usage_metadata = usage_metadata

                return Response(content, usage_metadata)

        return CopilotWrapper(model_name, temperature, max_tokens)
