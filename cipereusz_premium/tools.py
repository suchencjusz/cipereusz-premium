from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ToolCallable = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._current_row = []
        elif tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            text = html.unescape("".join(self._current_cell))
            text = re.sub(r"\s+", " ", text).strip()
            self._current_row.append(text)
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._in_row = False
        elif tag == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell.append(data)


def _fetch_url(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 7.1.2; TX2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.73 Safari/537.36"})
    
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _parse_first_table_rows(html_text: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(html_text)
    if not parser.tables:
        return []
    return parser.tables[0]


def _limit_rows(rows: list[list[str]], limit: int) -> list[list[str]]:
    return rows[: max(1, limit)]


def _get_local_tz() -> ZoneInfo:
    name = os.getenv("TZ", "Europe/Warsaw")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolCallable] = {}
        self._schemas: list[dict[str, Any]] = []

    def register(self, name: str, description: str, parameters: dict[str, Any] | None = None):
        def decorator(func: ToolCallable) -> ToolCallable:
            self._tools[name] = func
            self._schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters
                        or {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }
            )
            return func

        return decorator

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler = self._tools.get(name)
        if handler is None:
            log.warning("wywolano nieznane narzedzie: %s args=%s", name, arguments)
            return "unknown_tool"

        log.info("tool_call name=%s args=%s", name, arguments)
        started = time.monotonic()

        try:
            result = handler(arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            elapsed = time.monotonic() - started
            log.exception("blad narzedzia %s (czas=%.3fs)", name, elapsed)
            return f"tool_error:{type(exc).__name__}"

        elapsed = time.monotonic() - started
        text_result = result if isinstance(result, str) else str(result)
        log.info("tool_result name=%s czas=%.3fs wynik=%r", name, elapsed, text_result[:200])

        return text_result


def create_default_tools() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(
        name="get_time",
        description="return current local time (uses TZ env) in iso format",
    )
    def get_time(_: dict[str, Any]) -> str:
        tz = _get_local_tz()
        now = datetime.now(tz)
        tz_name = getattr(tz, "key", "") or "local"

        return f"{now.isoformat()} {tz_name}"

    @registry.register(
        name="get_time_local",
        description="return current local time in iso format",
    )
    def get_time_local(_: dict[str, Any]) -> str:
        tz = _get_local_tz()
        now = datetime.now(tz)
        tz_name = getattr(tz, "key", "") or "local"
        
        return f"{now.isoformat()} {tz_name}"

    @registry.register(
        name="search_web",
        description="search the web using DuckDuckGo and return a few top results",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {
                    "type": "integer",
                    "description": "How many results (1-5)",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search_web(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        
        if not query:
            return "brak query"
        
        max_results = int(args.get("max_results", 3) or 3)
        max_results = max(1, min(5, max_results))

        def _do_search() -> list[dict[str, str]]:
            from duckduckgo_search import DDGS

            out: list[dict[str, str]] = []
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=max_results):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title", "")).strip()
                    url = str(item.get("href", item.get("url", ""))).strip()
                    body = str(item.get("body", item.get("snippet", ""))).strip()
                    if title or url or body:
                        out.append({"title": title, "url": url, "body": body})
            return out

        try:
            results = await asyncio.to_thread(_do_search)
        except Exception:
            return "blad szukania"

        if not results:
            return "nic nie znalazlem"
        
        lines: list[str] = []
        
        for item in results[:max_results]:
            title = item.get("title", "")
            url = item.get("url", "")
            body = item.get("body", "")
            line = " - ".join(part for part in [title, url, body] if part)
            if line:
                lines.append(line)
        
        text = " | ".join(lines)
        
        return text[:1200]

    @registry.register(
        name="wikipedia_lookup",
        description=(
            "pobiera krotkie, rzetelne streszczenie hasla z Wikipedii (kim/czym jest X, definicje, "
            "biografie, pojecia). UZYJ TEGO zamiast zgadywac gdy ktos pyta kim/czym jest ktos/cos, "
            "a search_web daje za malo konkretow"
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "haslo/temat do wyszukania, np. 'Kraków' albo 'fotosynteza'"},
                "lang": {
                    "type": "string",
                    "description": "kod jezyka wikipedii, domyslnie pl",
                    "enum": ["pl", "en"],
                },
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    )
    async def wikipedia_lookup(args: dict[str, Any]) -> str:
        topic = str(args.get("topic", "")).strip()
        if not topic:
            return "brak tematu"

        lang = str(args.get("lang", "pl")).strip() or "pl"
        if lang not in ("pl", "en"):
            lang = "pl"

        def _do_lookup() -> str:
            search_url = (
                f"https://{lang}.wikipedia.org/w/api.php?action=opensearch&format=json"
                f"&limit=1&namespace=0&search={urllib.parse.quote(topic)}"
            )
            raw = _fetch_url(search_url, timeout=8)
            data = json.loads(raw)
            titles = data[1] if isinstance(data, list) and len(data) > 1 else []
            if not titles:
                return ""
            title = titles[0]

            summary_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
            raw_summary = _fetch_url(summary_url, timeout=8)
            summary_data = json.loads(raw_summary)
            extract = str(summary_data.get("extract", "")).strip()
            return extract

        try:
            extract = await asyncio.to_thread(_do_lookup)
        except Exception:
            return "blad wikipedii"

        if not extract:
            return "nic nie znalazlem na wikipedii"

        return extract[:1200]

    @registry.register(
        name="get_weather",
        description="get current weather via wttr.in",
        parameters={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City or place name"},
                "format": {"type": "string", "description": "wttr.in format, default 3"},
            },
            "additionalProperties": False,
        },
    )
    async def get_weather(args: dict[str, Any]) -> str:
        location = str(args.get("location", "Katowice")).strip() or "Katowice"

        fmt = str(args.get("format", "3")).strip() or "3"
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format={urllib.parse.quote(fmt)}"
        
        try:
            text = await asyncio.to_thread(_fetch_url, url, 10)
        except Exception:
            return "blad pogody"
        
        text = re.sub(r"\s+", " ", text).strip()
        
        return text[:240]

    @registry.register(
        name="sprawdz_krypto",
        description="pobiera aktualna cene krypto z publicznego api coingecko",
        parameters={
            "type": "object",
            "properties": {
                "coin_id": {
                    "type": "string",
                    "description": "pelna nazwa np bitcoin ethereum dogecoin",
                }
            },
            "additionalProperties": False,
        },
    )
    async def sprawdz_krypto(args: dict[str, Any]) -> str:
        coin_id = str(args.get("coin_id", "bitcoin")).strip() or "bitcoin"

        def _fetch() -> str:
            import requests

            url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd,pln"
            
            try:
                response = requests.get(url, timeout=5)
            except Exception:
                return "blad polaczenia z rynkiem krypto"

            if response.status_code == 200:
                try:
                    dane = response.json()
                except Exception:
                    return "blad danych z krypto"
                if coin_id in dane and isinstance(dane[coin_id], dict):
                    cena_usd = dane[coin_id].get("usd")
                    cena_pln = dane[coin_id].get("pln")
                    return f"aktualna cena {coin_id}: {cena_usd} usd {cena_pln} pln"
                return f"nie znalazlem krypto o nazwie {coin_id}"

            if response.status_code == 429:
                return "limit zapytan do api osiagniety sprobuj za minute"

            return f"blad serwera krypto {response.status_code}"

        return await asyncio.to_thread(_fetch)

    @registry.register(
        name="bilard_stats",
        description=(
            "UŻYJ TEGO ZAWSZE gdy ktoś pyta o bilard, statystyki, kranus pro, Akademik Babilon, "
            "lub pyta kto jest królem / najlepszym graczem. Narzędzie pobiera dane z bazy."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["today", "matches", "ranking", "players", "player"],
                    "description": "Co pobrać. Wybierz 'ranking' gdy pytają o króla bilarda, 'today' dla dzisiejszych gier, 'player' dla konkretnej osoby.",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "Maksymalna liczba wyników (np. 5)",
                    "minimum": 1,
                    "maximum": 50,
                },
                "player": {"type": "string", "description": "Nick gracza (wymagane tylko gdy scope to 'player')"},
            },
            "required": ["scope"],
            "additionalProperties": False,
        },
    )
    def bilard_stats(args: dict[str, Any]) -> str:
        scope = str(args.get("scope", "")).strip()
        raw_limit = args.get("limit", 5)

        try:
            limit = int(raw_limit)  # Groq sometimes emits "5" as a string
        except (TypeError, ValueError):
            limit = 5
            
        limit = max(1, min(50, limit))
        base = "https://bilard.kranus.pro"

        try:
            if scope == "today":
                html_text = _fetch_url(base)
                if "Brak meczy dzisiaj" in html_text:
                    return "dzisiaj 0 meczy"
                rows = _parse_first_table_rows(html_text)
                rows = _limit_rows(rows, limit)
                return "dzisiaj: " + " | ".join(
                    f"{row[0]} {row[2]} winner {row[3]}" for row in rows if len(row) >= 4
                )

            if scope == "matches":
                html_text = _fetch_url(f"{base}/matches")
                rows = _parse_first_table_rows(html_text)
                rows = _limit_rows(rows, limit)
                return "mecze: " + " | ".join(
                    f"{row[0]} {row[2]} winner {row[3]}" for row in rows if len(row) >= 4
                )

            if scope == "ranking":
                html_text = _fetch_url(f"{base}/ranking")
                rows = _parse_first_table_rows(html_text)
                rows = _limit_rows(rows, limit)
                return "ranking: " + " | ".join(
                    f"{row[0]} {row[1]} w:{row[2]} l:{row[3]} win%:{row[4]}" for row in rows if len(row) >= 5
                )

            if scope == "players":
                html_text = _fetch_url(f"{base}/players")
                rows = _parse_first_table_rows(html_text)
                rows = _limit_rows(rows, limit)
                return "gracze: " + ", ".join(row[1] for row in rows if len(row) >= 2)

            if scope == "player":
                player = str(args.get("player", "")).strip()
                if not player:
                    return "brak gracza"
                html_text = _fetch_url(f"{base}/matches")
                rows = _parse_first_table_rows(html_text)
                wins = 0
                losses = 0
                seen = 0
                for row in rows:
                    if len(row) < 4:
                        continue
                    players = row[2]
                    winner = row[3]
                    if player.lower() not in players.lower():
                        continue
                    seen += 1
                    if player.lower() in winner.lower():
                        wins += 1
                    else:
                        losses += 1
                    if seen >= limit:
                        break
                return f"gracz {player} ostatnie {seen} w:{wins} l:{losses}"
        except urllib.error.URLError:
            return "blad pobierania"

        return "nieznany scope"

    @registry.register(
        name="filmweb_ostatnie_filmy",
        description=(
            "UZYJ TEGO gdy ktos poda swoj nick/login z filmweb.pl (portal o filmach) albo pyta co "
            "ostatnio ogladal/ocenial - dzieki temu mozesz go wyzwac za ocene albo gust filmowy. "
            "Zwraca liste ostatnio ocenionych filmow z ocenami z 10."
        ),
        parameters={
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "login/nick uzytkownika na filmweb.pl"},
                "limit": {
                    "type": "integer",
                    "description": "ile ostatnich pozycji pokazac (1-10, domyslnie 5)",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["username"],
            "additionalProperties": False,
        },
    )
    async def filmweb_ostatnie_filmy(args: dict[str, Any]) -> str:
        username = str(args.get("username", "")).strip()
        if not username:
            return "brak nicku"

        try:
            limit = int(args.get("limit", 5) or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(10, limit))

        def _do_lookup() -> str:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 7.1.2; TX2) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/77.0.3865.73 Safari/537.36"
                )
            }

            def _get_json(url: str, timeout: int = 8) -> Any:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="ignore"))

            id_url = f"https://www.filmweb.pl/api/v1/users/{urllib.parse.quote(username)}/id"
            try:
                id_data = _get_json(id_url)
            except Exception:
                return "nie znalazlem takiego uzytkownika na filmweb (zly nick albo prywatny profil)"

            user_id = id_data.get("userId") if isinstance(id_data, dict) else None
            if user_id is None:
                return "nie znalazlem takiego uzytkownika na filmweb"

            votes_url = f"https://www.filmweb.pl/api/v1/users/{user_id}/votes/film"
            try:
                votes_data = _get_json(votes_url)
            except Exception:
                return "profil prywatny albo brak ocenionych filmow"

            if isinstance(votes_data, list):
                votes = votes_data
            elif isinstance(votes_data, dict):
                votes = votes_data.get("votes", [])
            else:
                votes = []

            if not votes:
                return f"{username} nic jeszcze nie ocenil na filmweb"

            def _vote_movie_id(vote: Any) -> Any:
                entry = vote.get("id") if isinstance(vote, dict) else None
                if isinstance(entry, dict):
                    return entry.get("id")
                return entry

            entries: list[str] = []
            # bierzemy zapas, bo pojedyncze zapytania o tytul moga sie nie udac
            for vote in votes[: limit * 2]:
                if len(entries) >= limit:
                    break
                movie_id = _vote_movie_id(vote)
                if movie_id is None:
                    continue
                rate = vote.get("rate") if isinstance(vote, dict) else None

                title = None
                try:
                    info_data = _get_json(f"https://www.filmweb.pl/api/v1/title/{movie_id}/info", timeout=6)
                    if isinstance(info_data, dict):
                        title = info_data.get("title") or info_data.get("originalTitle") or info_data.get("name")
                except Exception:
                    title = None

                label = str(title) if title else f"film o id {movie_id}"
                entries.append(f"{label} ({rate}/10)" if rate else label)

            if not entries:
                return "nie udalo sie pobrac szczegolow ocenionych filmow"

            return f"ostatnio na filmweb ({username}): " + " | ".join(entries)

        try:
            return await asyncio.to_thread(_do_lookup)
        except Exception:
            return "blad polaczenia z filmweb"

    return registry
