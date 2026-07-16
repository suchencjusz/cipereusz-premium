from __future__ import annotations

import base64
import json
import logging
import re
import string
import time
from dataclasses import dataclass
from typing import Any

from groq import AsyncGroq

from .persona import BASE_PERSONA, MEMORY_EXTRACTION_PERSONA
from .tools import ToolRegistry

log = logging.getLogger(__name__)


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
    stt_requests: int = 0
    tts_requests: int = 0
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
    def __init__(
        self,
        api_key: str,
        chat_model: str,
        vision_model: str,
        tools: ToolRegistry,
        stt_model: str = "whisper-large-v3-turbo",
    ) -> None:
        self.client = AsyncGroq(api_key=api_key)
        self.chat_model = chat_model
        self.vision_model = vision_model
        self.stt_model = stt_model
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

        for attempt in range(tool_loop_limit + 1):
            self.stats.chat_requests += 1
            started = time.monotonic()

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
                log.error(
                    "groq chat request failed model=%s attempt=%d tools=%s: %s",
                    model, attempt, enable_tools, exc,
                )

                if enable_tools and "tool_use_failed" in str(exc).lower():
                    log.warning("ponawiam zapytanie bez narzedzi (tool_use_failed)")
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

            elapsed = time.monotonic() - started
            self._accumulate_usage(getattr(response, "usage", None))
            choice = response.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []
            usage = getattr(response, "usage", None)
            log.info(
                "groq chat request model=%s attempt=%d czas=%.3fs tool_calls=%d prompt_tokens=%s completion_tokens=%s",
                model, attempt, elapsed, len(tool_calls),
                getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"),
            )
            
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
                        log.warning("nieprawidlowe argumenty narzedzia %s: %r", tool_call.function.name, raw_args)
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

        log.warning("osiagnieto limit petli narzedzi (tool_loop_limit=%d) dla modelu %s", tool_loop_limit, model)
        return _clean_text("no i sie zesralo")

    async def analyze_image(self, *, system_prompt: str, user_prompt: str, image_bytes: bytes) -> str:
        image_data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        
        self.stats.vision_requests += 1
        log.info("groq vision request model=%s image_bytes=%d", self.vision_model, len(image_bytes))

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
        started = time.monotonic()
        log.info("groq memory extraction start chars=%d", len(transcript))

        base_kwargs: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            # bylo 300 - za malo, ekstrakcja obcinala sie w polowie jsona i cala
            # partia wiadomosci przepadala bez sladu (glowna przyczyna "teczka nic nie pamieta")
            "max_tokens": 1500,
        }

        try:
            try:
                response = await self.client.chat.completions.create(
                    **base_kwargs, response_format={"type": "json_object"}
                )
            except Exception as exc:
                log.warning(
                    "model %s nie wspiera response_format=json_object (%s), ponawiam bez tego",
                    self.chat_model, exc,
                )
                response = await self.client.chat.completions.create(**base_kwargs)
        except Exception as exc:
            self.stats.failures += 1
            log.error("blad ekstrakcji pamieci (chars=%d): %s", len(transcript), exc)
            raise

        elapsed = time.monotonic() - started
        self._accumulate_usage(getattr(response, "usage", None))
        raw_content = response.choices[0].message.content or "{}"

        try:
            payload = _extract_json(raw_content)
        except Exception as exc:
            self.stats.failures += 1
            log.error(
                "nie udalo sie sparsowac jsona z ekstrakcji pamieci czas=%.3fs blad=%s tresc=%r",
                elapsed, exc, raw_content[:500],
            )
            raise

        log.info(
            "groq memory extraction done czas=%.3fs profile=%d brudy=%d",
            elapsed, len(payload.get("profiles", []) or []), len(payload.get("dirt", []) or []),
        )
        return payload

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        filename: str = "utterance.wav",
        language: str | None = "pl",
    ) -> str:
        self.stats.stt_requests += 1
        started = time.monotonic()
        log.info("groq stt request model=%s bytes=%d language=%s", self.stt_model, len(audio_bytes), language)

        kwargs: dict[str, Any] = {
            "file": (filename, audio_bytes),
            "model": self.stt_model,
            "response_format": "json",
        }
        if language:
            kwargs["language"] = language

        try:
            response = await self.client.audio.transcriptions.create(**kwargs)
        except Exception as exc:
            self.stats.failures += 1
            log.error("blad transkrypcji audio (stt) bytes=%d: %s", len(audio_bytes), exc)
            raise

        elapsed = time.monotonic() - started
        text = str(getattr(response, "text", "") or "").strip()
        log.info("groq stt done czas=%.3fs dlugosc_tekstu=%d", elapsed, len(text))
        return text
