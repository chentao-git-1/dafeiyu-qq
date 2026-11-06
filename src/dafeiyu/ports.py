"""Structural interfaces for capabilities supplied by runtime adapters."""
from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class TextReply(Protocol):
    completion_text: str


class TextProvider(Protocol):
    async def text_chat(self, **kwargs: Any) -> TextReply: ...


ReviewCall = Callable[[str, str], Awaitable[str]]
