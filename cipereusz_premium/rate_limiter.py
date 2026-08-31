from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ModelLimits:
    """Znane limity modelu z Groq (darmowy tier)."""
    rpm: int = 30       # requests per minute
    rpd: int = 1000     # requests per day
    tpm: int = 12000    # tokens per minute
    tpd: int = 100000   # tokens per day


@dataclass
class ModelUsage:
    """Sledzone zuzycie jednego modelu."""
    requests: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_total: int = 0
    failures: int = 0
    rate_limits_hit: int = 0
    # pozostale z headerow (jesli dostepne)
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    # cooldown po 429
    cooldown_until: float = 0.0       # time.monotonic()
    last_429_retry_after: float = 0.0
    # reset times z headerow
    reset_requests_at: float = 0.0
    reset_tokens_at: float = 0.0
    # sliding window: timestampy ostatnich requestow (do szacowania RPM)
    _recent_requests: list[float] = field(default_factory=list)
    _recent_tokens: list[tuple[float, int]] = field(default_factory=list)


@dataclass(slots=True)
class ModelTier:
    """Model z priorytetem w lancuchu fallback."""
    model_id: str
    limits: ModelLimits
    priority: int  # nizszy = lepszy (primary = 0)


# domyslny lancuch fallback na darmowym tierze groq
DEFAULT_MODEL_CHAIN: list[ModelTier] = [
    ModelTier(
        "llama-3.3-70b-versatile",
        ModelLimits(rpm=30, rpd=1000, tpm=12000, tpd=100000),
        priority=0,
    ),
    ModelTier(
        "qwen/qwen3.6-27b",
        ModelLimits(rpm=30, rpd=1000, tpm=8000, tpd=200000),
        priority=1,
    ),
    ModelTier(
        "llama-3.1-8b-instant",
        ModelLimits(rpm=30, rpd=14400, tpm=6000, tpd=500000),
        priority=2,
    ),
]


def _parse_duration(value: str) -> float:
    """Parsuje duration z Groq API np. '2m59.56s' lub '7.66s' na sekundy."""
    value = value.strip()
    total = 0.0
    # minuty
    if "m" in value:
        parts = value.split("m", 1)
        try:
            total += float(parts[0]) * 60
        except ValueError:
            pass
        value = parts[1]
    # sekundy
    if "s" in value:
        value = value.rstrip("s")
    if value:
        try:
            total += float(value)
        except ValueError:
            pass
    return max(1.0, total)


class RateLimitManager:
    """Zarzadza lancuchem modeli z automatycznym fallbackiem przy 429.

    Glowna logika:
    - get_best_model() zwraca najlepszy dostepny model (nie na cooldownie)
    - report_rate_limit() oznacza model jako niedostepny i zwraca nastepny
    - report_success() sledzi zuzycie i aktualizuje limity z headerow
    - co jakis czas probuje wrocic do modelu glownego
    """

    def __init__(self, model_chain: list[ModelTier] | None = None) -> None:
        self.chain = model_chain or list(DEFAULT_MODEL_CHAIN)
        self.usage: dict[str, ModelUsage] = {
            tier.model_id: ModelUsage() for tier in self.chain
        }
        self._current_index = 0
        self._start_time = time.monotonic()

    @property
    def current_model(self) -> str:
        return self.chain[self._current_index].model_id

    @property
    def primary_model(self) -> str:
        return self.chain[0].model_id

    def get_best_model(self) -> str:
        """Zwraca najlepszy dostepny model (bierze pod uwage cooldowny i zuzycie)."""
        now = time.monotonic()

        # probuj wrocic do glownego jesli cooldown minal
        if self._current_index > 0:
            primary_usage = self.usage[self.primary_model]
            if primary_usage.cooldown_until <= now:
                log.info(
                    "rate_limiter: cooldown modelu glownego (%s) minal, wracam",
                    self.primary_model,
                )
                self._current_index = 0
                return self.primary_model

        # sprawdz aktualny model
        current = self.current_model
        current_usage = self.usage[current]

        if current_usage.cooldown_until <= now:
            # sprawdz czy nie jestesmy blisko limitu RPM
            if self._is_rpm_critical(current):
                next_idx = self._find_next_available(self._current_index + 1)
                if next_idx is not None:
                    next_model = self.chain[next_idx].model_id
                    log.info(
                        "rate_limiter: %s bliski RPM limitu (%.0f req/min), "
                        "proaktywnie przelaczam na %s",
                        current, self._recent_rpm(current), next_model,
                    )
                    self._current_index = next_idx
                    return next_model
            return current

        # aktualny na cooldownie, szukaj nastepnego
        next_idx = self._find_next_available(self._current_index + 1)
        if next_idx is not None:
            self._current_index = next_idx
            return self.chain[next_idx].model_id

        # wszystkie na cooldownie - wez ten z najkrotszym cooldownem
        return self._pick_shortest_cooldown()

    def report_success(
        self,
        model_id: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Wywolaj po udanym requeście."""
        usage = self._ensure_usage(model_id)
        now = time.monotonic()

        usage.requests += 1
        usage.tokens_prompt += prompt_tokens
        usage.tokens_completion += completion_tokens
        total = prompt_tokens + completion_tokens
        usage.tokens_total += total

        # sliding window
        usage._recent_requests.append(now)
        usage._recent_tokens.append((now, total))
        self._cleanup_sliding_window(usage, now)

        if headers:
            self._update_from_headers(model_id, headers)

    def report_rate_limit(self, model_id: str, retry_after: float = 60.0) -> str:
        """Wywolaj po otrzymaniu 429. Zwraca nastepny model do uzycia."""
        usage = self._ensure_usage(model_id)

        usage.rate_limits_hit += 1
        usage.failures += 1
        usage.last_429_retry_after = retry_after
        usage.cooldown_until = time.monotonic() + retry_after

        log.warning(
            "rate_limiter: 429 na %s (retry_after=%.1fs, laczne_429=%d), szukam fallbacka",
            model_id, retry_after, usage.rate_limits_hit,
        )

        # ustal indeks tego modelu
        for i, tier in enumerate(self.chain):
            if tier.model_id == model_id:
                self._current_index = i
                break

        return self.get_best_model()

    def report_failure(self, model_id: str) -> None:
        usage = self._ensure_usage(model_id)
        usage.failures += 1

    # --- proaktywne szacowanie ---

    def _is_rpm_critical(self, model_id: str) -> bool:
        """Czy model jest bliski limitu RPM (>80% w ostatniej minucie)."""
        tier = self._find_tier(model_id)
        if tier is None:
            return False
        rpm = self._recent_rpm(model_id)
        return rpm >= tier.limits.rpm * 0.8

    def _recent_rpm(self, model_id: str) -> float:
        """Ile requestow w ostatniej minucie."""
        usage = self.usage.get(model_id)
        if usage is None:
            return 0.0
        now = time.monotonic()
        cutoff = now - 60.0
        return sum(1 for t in usage._recent_requests if t > cutoff)

    def _recent_tpm(self, model_id: str) -> int:
        """Ile tokenow w ostatniej minucie."""
        usage = self.usage.get(model_id)
        if usage is None:
            return 0
        now = time.monotonic()
        cutoff = now - 60.0
        return sum(tok for t, tok in usage._recent_tokens if t > cutoff)

    # --- wewnetrzne ---

    def _ensure_usage(self, model_id: str) -> ModelUsage:
        if model_id not in self.usage:
            self.usage[model_id] = ModelUsage()
        return self.usage[model_id]

    def _find_tier(self, model_id: str) -> ModelTier | None:
        for tier in self.chain:
            if tier.model_id == model_id:
                return tier
        return None

    def _find_next_available(self, start_index: int) -> int | None:
        now = time.monotonic()
        for i in range(start_index, len(self.chain)):
            if self.usage[self.chain[i].model_id].cooldown_until <= now:
                return i
        return None

    def _pick_shortest_cooldown(self) -> str:
        now = time.monotonic()
        best = self.chain[0]
        best_wait = self.usage[best.model_id].cooldown_until - now
        for tier in self.chain[1:]:
            wait = self.usage[tier.model_id].cooldown_until - now
            if wait < best_wait:
                best = tier
                best_wait = wait
        return best.model_id

    def _cleanup_sliding_window(self, usage: ModelUsage, now: float) -> None:
        cutoff = now - 120.0  # 2 minuty
        usage._recent_requests = [t for t in usage._recent_requests if t > cutoff]
        usage._recent_tokens = [(t, tok) for t, tok in usage._recent_tokens if t > cutoff]

    def _update_from_headers(self, model_id: str, headers: dict[str, str]) -> None:
        usage = self._ensure_usage(model_id)

        remaining_req = headers.get("x-ratelimit-remaining-requests")
        remaining_tok = headers.get("x-ratelimit-remaining-tokens")

        if remaining_req is not None:
            try:
                usage.remaining_requests = int(remaining_req)
            except ValueError:
                pass

        if remaining_tok is not None:
            try:
                usage.remaining_tokens = int(remaining_tok)
            except ValueError:
                pass

        # reset times
        now = time.monotonic()
        reset_req = headers.get("x-ratelimit-reset-requests")
        reset_tok = headers.get("x-ratelimit-reset-tokens")
        if reset_req:
            usage.reset_requests_at = now + _parse_duration(reset_req)
        if reset_tok:
            usage.reset_tokens_at = now + _parse_duration(reset_tok)

    # --- status / statystyki ---

    def get_status(self) -> dict[str, Any]:
        """Zwraca szczegolowy status dla komend /api i /limity."""
        now = time.monotonic()
        uptime = now - self._start_time
        models_status = {}

        for tier in self.chain:
            usage = self.usage[tier.model_id]
            cooldown_left = max(0.0, usage.cooldown_until - now)
            models_status[tier.model_id] = {
                "priority": tier.priority,
                "requests": usage.requests,
                "tokens_total": usage.tokens_total,
                "tokens_prompt": usage.tokens_prompt,
                "tokens_completion": usage.tokens_completion,
                "failures": usage.failures,
                "rate_limits_hit": usage.rate_limits_hit,
                "remaining_requests": usage.remaining_requests,
                "remaining_tokens": usage.remaining_tokens,
                "cooldown_seconds": round(cooldown_left, 1) if cooldown_left > 0 else 0,
                "available": cooldown_left <= 0,
                "rpm_current": round(self._recent_rpm(tier.model_id), 1),
                "tpm_current": self._recent_tpm(tier.model_id),
                "limits": {
                    "rpm": tier.limits.rpm,
                    "rpd": tier.limits.rpd,
                    "tpm": tier.limits.tpm,
                    "tpd": tier.limits.tpd,
                },
            }

        return {
            "current_model": self.current_model,
            "primary_model": self.primary_model,
            "uptime_seconds": round(uptime, 0),
            "total_tokens": self.total_tokens,
            "total_requests": self.total_requests,
            "total_429s": self.total_429s,
            "models": models_status,
        }

    @property
    def total_tokens(self) -> int:
        return sum(u.tokens_total for u in self.usage.values())

    @property
    def total_requests(self) -> int:
        return sum(u.requests for u in self.usage.values())

    @property
    def total_429s(self) -> int:
        return sum(u.rate_limits_hit for u in self.usage.values())

    def format_status_embed_fields(self) -> list[tuple[str, str, bool]]:
        """Zwraca pola gotowe do discord.Embed (name, value, inline)."""
        status = self.get_status()
        fields: list[tuple[str, str, bool]] = []

        fields.append(("aktywny model", status["current_model"], False))

        uptime_h = status["uptime_seconds"] / 3600
        fields.append((
            "ogolne",
            f"tokeny: {status['total_tokens']:,}\n"
            f"requesty: {status['total_requests']}\n"
            f"429-ki: {status['total_429s']}\n"
            f"uptime: {uptime_h:.1f}h",
            True,
        ))

        for model_id, info in status["models"].items():
            is_current = model_id == status["current_model"]
            marker = "▶ " if is_current else "  "
            available = "✅" if info["available"] else f"⏳{info['cooldown_seconds']}s"

            value = (
                f"{available} p{info['priority']}\n"
                f"tok: {info['tokens_total']:,}\n"
                f"req: {info['requests']} | 429: {info['rate_limits_hit']}\n"
                f"rpm: {info['rpm_current']}/{info['limits']['rpm']}\n"
                f"tpm: {info['tpm_current']:,}/{info['limits']['tpm']:,}"
            )
            if info["remaining_requests"] is not None:
                value += f"\nrem_req: {info['remaining_requests']}"
            if info["remaining_tokens"] is not None:
                value += f"\nrem_tok: {info['remaining_tokens']}"

            short_name = model_id.split("/")[-1][:20]
            fields.append((f"{marker}{short_name}", value, True))

        return fields
