from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import unicodedata
from typing import TYPE_CHECKING

import discord

try:
    import edge_tts
except ImportError:  # pragma: no cover - biblioteka jest opcjonalna
    edge_tts = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from .bot import CipekBot

log = logging.getLogger(__name__)

VOICE_STACK_AVAILABLE = edge_tts is not None

# UWAGA - dlaczego tu nie ma nasluchiwania kanalu:
#
# Od 2 marca 2026 Discord wymusza szyfrowanie end-to-end (DAVE) na wszystkich
# kanalach glosowych. py-cord sam, oficjalnie, w runtime ostrzega:
# "Voice reception is currently broken due to Discord's DAVE (End-to-End
# Encryption) protocol" (https://github.com/Pycord-Development/pycord/issues/3139).
# Proba uzycia discord.sinks.Sink do odbioru audio konczy sie tam bledami
# (m.in. AttributeError w wewnetrznym routerze biblioteki). To nie jest cos,
# co da sie obejsc po naszej stronie - trzeba czekac az py-cord to naprawi.
#
# Zamiast tego: bot dolacza do kanalu i odpowiada glosem, gdy ktos NAPISZE
# slowo-klucz na czacie tekstowym (patrz CipekBot.on_message) - to jedyna
# czesc pomyslu ktora da sie dzisiaj zrealizowac niezawodnie, bo wysylanie
# dzwieku (TTS) dziala normalnie, dotkniety jest tylko odbior.


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return text


def contains_trigger(text: str, triggers: list[str]) -> bool:
    """Sprawdza czy tekst zawiera jedno ze slow-kluczy (np. cipek/cipereusz),
    tolerujac polskie odmiany przez przypadki (cipku, cipka, cipereuszu...).
    """
    if not text or not triggers:
        return False

    normalized = _normalize(text)

    for trigger in triggers:
        stem = _normalize(trigger)
        if not stem:
            continue
        prefix = stem[:-1] if len(stem) > 4 else stem
        pattern = re.compile(rf"\b{re.escape(prefix)}\w*", re.UNICODE)
        if pattern.search(normalized):
            return True

    return False


class VoiceSession:
    """Zarzadza jedna sesja "cipka na kanale glosowym" dla jednej gildii.

    Odbior/transkrypcja mowy jest WYLACZONA (patrz komentarz na gorze pliku -
    zepsute w py-cord przez DAVE). Sesja obsluguje polaczenie/rozlaczenie
    oraz mowienie odpowiedzi (Groq LLM + edge-tts), wyzwalane z tekstu na
    czacie, nie z glosu.
    """

    def __init__(self, bot: "CipekBot", guild: discord.Guild, text_channel_id: int) -> None:
        self.bot = bot
        self.guild = guild
        self.text_channel_id = text_channel_id
        self.voice_client: discord.VoiceClient | None = None
        self.channel: discord.VoiceChannel | None = None

    @property
    def config(self):
        return self.bot.config

    async def connect(self, channel: discord.VoiceChannel) -> None:
        if edge_tts is None:
            raise RuntimeError("brak biblioteki edge-tts na serwerze")

        self.voice_client = await channel.connect()
        self.channel = channel
        log.info("voice: dolaczono do kanalu '%s' (%s) na gildii %s", channel.name, channel.id, self.guild.id)

    async def disconnect(self) -> None:
        if self.voice_client is not None:
            try:
                await self.voice_client.disconnect(force=True)
            except Exception:
                log.exception("voice: blad przy rozlaczaniu")
            self.voice_client = None

        self.channel = None
        log.info("voice: opuszczono kanal glosowy na gildii %s", self.guild.id)

    # --- mowienie odpowiedzi -------------------------------------------------
    #
    # UWAGA: nie ma tu juz osobnej generacji odpowiedzi "do mowienia". Kiedys
    # bylo tak, ze respond_to_text() samo pytalo LLM o oddzielna, krotsza
    # odpowiedz - w efekcie to co bot mowil na glos i to co pisal na czacie
    # nie mialo ze soba nic wspolnego (dwie niezalezne generacje). Teraz
    # CipekBot._reply_to_mention() generuje JEDNA odpowiedz, wysyla ja na
    # czat i - jesli warunki sie zgadzaja (autor faktycznie jest na kanale
    # glosowym) - wola ponizsze speak() z DOKLADNIE tym samym tekstem.

    async def speak(self, text: str) -> bool:
        if not text:
            return False
        return await self._speak(text)

    # --- text-to-speech -------------------------------------------------------

    async def _speak(self, text: str) -> bool:
        if self.voice_client is None:
            return False

        for _ in range(100):
            if not self.voice_client.is_playing():
                break
            await asyncio.sleep(0.1)

        path = await self._synthesize(text)
        if path is None:
            return False

        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def _after(error: Exception | None) -> None:
            if error:
                log.error("voice: blad odtwarzania audio: %s", error)
            loop.call_soon_threadsafe(done.set)
            try:
                os.remove(path)
            except OSError:
                pass

        try:
            source = discord.FFmpegPCMAudio(path)
            self.voice_client.play(source, after=_after)
        except Exception:
            log.exception("voice: nie udalo sie odtworzyc odpowiedzi glosowej")
            try:
                os.remove(path)
            except OSError:
                pass
            return False

        await done.wait()
        return True

    async def _synthesize(self, text: str) -> str | None:
        assert edge_tts is not None

        self.bot.llm.stats.tts_requests += 1
        started = time.monotonic()
        tmp_path = ""

        try:
            communicate = edge_tts.Communicate(
                text,
                voice=self.config.voice_tts_voice,
                rate=self.config.voice_tts_rate,
            )
            fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            await communicate.save(tmp_path)
        except Exception:
            self.bot.llm.stats.failures += 1
            log.exception("voice: blad syntezy mowy (edge-tts, glos=%s)", self.config.voice_tts_voice)
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None

        elapsed = time.monotonic() - started
        log.info("voice: tts done czas=%.3fs znaki=%d glos=%s", elapsed, len(text), self.config.voice_tts_voice)
        return tmp_path
