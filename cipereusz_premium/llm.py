from __future__ import annotations

import base64
import json
import re
import string
from dataclasses import dataclass
from typing import Any

from groq import AsyncGroq

from .persona import BASE_PERSONA, MEMORY_EXTRACTION_PERSONA
from .tools import ToolRegistry


def _clean_text(text: str) -> str:
    text = text.lower().replace("_", " ")
    text = re.sub(r"[^\w\s<>@!/:.?&=%#+-]", "", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()

    return text[:240]


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()

    if candidate.startswith("```"):
        candidate = candidate.strip("`")
    
    start = candidate.find("{")
    end = candidate.rfind("}")
    
    if start != -1 and end != -1:
        candidate = candidate[start : end + 1]
    
    return json.loads(candidate)


@dataclass(slots=True)
class MemoryBatch:
    transcript: str
    participants: list[tuple[str, str]]


@dataclass(slots=True)
class ApiStats:
    chat_requests: int = 0
    vision_requests: int = 0
    memory_requests: int = 0
    tool_calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_time: float = 0.0
    completion_time: float = 0.0
    queue_time: float = 0.0
    total_time: float = 0.0


class GroqService:
    def __init__(self, api_key: str, chat_model: str, vision_model: str, tools: ToolRegistry) -> None:
        self.client = AsyncGroq(api_key=api_key)
        self.chat_model = chat_model
        self.vision_model = vision_model
        self.tools = tools
        self.stats = ApiStats()

    def _accumulate_usage(self, usage: Any | None) -> None:
        if not usage:
            return

        self.stats.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.stats.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        self.stats.total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
        self.stats.prompt_time += float(getattr(usage, "prompt_time", 0.0) or 0.0)
        self.stats.completion_time += float(getattr(usage, "completion_time", 0.0) or 0.0)
        self.stats.queue_time += float(getattr(usage, "queue_time", 0.0) or 0.0)
        self.stats.total_time += float(getattr(usage, "total_time", 0.0) or 0.0)

    def build_system_prompt(self, memory_context: str = "") -> str:
        parts = [BASE_PERSONA]

        if memory_context:
            parts.append(memory_context)
        
        return "\n\n".join(parts)

    async def generate_reply(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_data_url: str | None = None,
        temperature: float = 0.8,
        max_tokens: int = 80,
        tool_loop_limit: int = 4,
        enable_tools: bool = True,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        
        if image_data_url is None:
            messages.append({"role": "user", "content": user_prompt})
        else:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            )

        for _ in range(tool_loop_limit + 1):
            self.stats.chat_requests += 1

            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }

                if enable_tools:
                    kwargs["tools"] = self.tools.schemas
                    kwargs["tool_choice"] = "auto"
                
                response = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                self.stats.failures += 1

                if enable_tools and "tool_use_failed" in str(exc).lower():
                    return await self.generate_reply(
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        image_data_url=image_data_url,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tool_loop_limit=0,
                        enable_tools=False,
                    )
                raise

            self._accumulate_usage(getattr(response, "usage", None))
            choice = response.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []
            
            if tool_calls:
                self.stats.tool_calls += len(tool_calls)
                
                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.content or "",
                        "tool_calls": tool_calls,
                    }
                )

                for tool_call in tool_calls:
                    raw_args = tool_call.function.arguments or "{}"
                    
                    try:
                        arguments = json.loads(raw_args)
                        if not isinstance(arguments, dict):
                            arguments = {}
                    except Exception:
                        arguments = {}
                    
                    result = await self.tools.call(tool_call.function.name, arguments)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        }
                    )
                continue

            content = choice.content or ""
            
            return _clean_text(content)

        return _clean_text("no i sie zesralo")

    async def analyze_image(self, *, system_prompt: str, user_prompt: str, image_bytes: bytes) -> str:
        image_data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        
        self.stats.vision_requests += 1

        return await self.generate_reply(
            model=self.vision_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_data_url=image_data_url,
            temperature=0.7,
            max_tokens=90,
        )

    async def extract_memory(self, transcript: str) -> dict[str, Any]:
        system_prompt = MEMORY_EXTRACTION_PERSONA

        user_prompt = (
            "zapisz profile uzytkownikow i konkretne brudy z tego logu\n\n"
            f"{transcript}\n\n"
            "zwracany format json:\n"
            "{\"profiles\":[{\"user_id\":\"123\",\"discord_name\":\"nick\",\"general_vibe\":\"krotki opis\"}],"
            "\"dirt\":[{\"user_id\":\"123\",\"memory_text\":\"konkretny przypal\"}]}"
        )

        self.stats.memory_requests += 1
        
        try:
            response = await self.client.chat.completions.create(
                model=self.chat_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=300,
            )
        except Exception:
            self.stats.failures += 1
            raise

        self._accumulate_usage(getattr(response, "usage", None))
        
        return _extract_json(response.choices[0].message.content or "{}")
