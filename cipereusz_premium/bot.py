from __future__ import annotations

import asyncio
import os
import random
import re
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from .config import BotConfig, load_config
from .llm import GroqService
from .memory import MemoryStore
from .tools import create_default_tools


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_local_tz() -> ZoneInfo:
    name = os.getenv("TZ", "Europe/Warsaw")

    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _now_local() -> datetime:
    return datetime.now(_get_local_tz())


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()

    if content_type.startswith("image/"):
        return True
    filename = attachment.filename.lower()

    return filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_IMAGE_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_NUMERIC_PING_RE = re.compile(r"(?<![\w/])@(\d{6,20})")
_KRANUS_IMAGES_URL = "https://kranus.pro/obrazki.html"


@dataclass(slots=True)
class MessageRecord:
    message_id: int
    channel_id: int
    user_id: str
    username: str
    content: str
    created_at: str


@dataclass(slots=True)
class ToolContext:
    guild_id: int
    channel_id: int
    target_user_id: int
    ping_used: bool = False
    image_used: bool = False


class CipekBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.config = config
        self.memory = MemoryStore(config.database_path)
        self.tools = create_default_tools()
        self.llm = GroqService(
            api_key=config.groq_api_key,
            chat_model=config.groq_chat_model,
            vision_model=config.groq_vision_model,
            tools=self.tools,
        )
        self.recent_messages: dict[int, deque[MessageRecord]] = defaultdict(
            lambda: deque(maxlen=self.config.memory_recent_messages)
        )
        self.recent_participants: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=64))
        self.recent_images: dict[int, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.config.recent_images_max)
        )
        self.last_activity: dict[int, datetime] = {}
        self.last_proactive: dict[int, datetime] = {}
        self.message_counter: dict[int, int] = defaultdict(int)
        self.next_random_ping: dict[int, datetime] = {}
        self.last_summarized_id: dict[int, int] = defaultdict(int)
        self._summarize_inflight: set[int] = set()
        self._tool_context: ToolContext | None = None
        self._kranus_cache: list[str] = []
        self._kranus_cache_time: datetime | None = None
        self._register_commands()
        self._register_tools()

    def _register_commands(self) -> None:
        @self.hybrid_command(name="pomoc", aliases=["komendy", "help"], with_app_command=True)
        async def pomoc(ctx: commands.Context) -> None:
            embed = discord.Embed(title="komendy", color=discord.Color.dark_teal())
            embed.add_field(name="info", value="/pomoc /teczka /api /dbtest /uczsie", inline=False)
            embed.set_footer(text="dziala tez z prefiksem !")
            await ctx.send(embed=embed)

        @self.hybrid_command(name="teczka", aliases=["db", "baza"], with_app_command=True)
        async def teczka(ctx: commands.Context) -> None:
            if ctx.author.id != self.config.admin_user_id:
                if ctx.interaction:
                    await ctx.send("nie dla ciebie", ephemeral=True)
                else:
                    await ctx.send("nie dla ciebie")
                return
            stats = await self.memory.get_stats()
            user_dirt = await self.memory.get_user_dirt_count(str(ctx.author.id))
            embed = discord.Embed(title="teczka", color=discord.Color.dark_teal())
            embed.add_field(name="profile", value=str(stats["profiles"],), inline=True)
            embed.add_field(name="brudy", value=str(stats["dirt"],), inline=True)
            embed.add_field(name="twoje", value=str(user_dirt), inline=True)
            await ctx.send(embed=embed)

        @self.hybrid_command(name="dbtest", aliases=["bazatest", "testdb"], with_app_command=True)
        async def dbtest(ctx: commands.Context) -> None:
            if ctx.author.id != self.config.admin_user_id:
                if ctx.interaction:
                    await ctx.send("nie dla ciebie", ephemeral=True)
                else:
                    await ctx.send("nie dla ciebie")
                return
            if ctx.interaction:
                await ctx.defer(ephemeral=True)
            result = await self.memory.health_check()
            if result.get("ok"):
                message = (
                    f"db ok profile={result.get('profile_ok')} dirt={result.get('dirt_ok')}"
                )
            else:
                message = f"db fail {result.get('error', 'unknown')}"
            if ctx.interaction:
                await ctx.send(message, ephemeral=True)
            else:
                await ctx.send(message)

        @self.hybrid_command(name="uczsie", aliases=["learn", "historia"], with_app_command=True)
        async def uczsie(ctx: commands.Context) -> None:
            if ctx.author.id != self.config.admin_user_id:
                if ctx.interaction:
                    await ctx.send("nie dla ciebie", ephemeral=True)
                else:
                    await ctx.send("nie dla ciebie")
                return
            if ctx.guild is None:
                if ctx.interaction:
                    await ctx.send("brak gildii", ephemeral=True)
                else:
                    await ctx.send("brak gildii")
                return
            if ctx.interaction:
                await ctx.defer(ephemeral=True)

            result = await self._learn_from_channel_history(ctx.channel, limit=1000)
            if result.get("status") == "busy":
                message = "pamiec zajeta sproboj za chwile"
            elif result.get("messages", 0) <= 0:
                message = "brak wiadomosci do nauki"
            else:
                message = (
                    "ok nauczone "
                    f"wiadomosci={result.get('messages')} "
                    f"batch={result.get('batches')} "
                    f"profile={result.get('profiles')} "
                    f"brudy={result.get('dirt')} "
                    f"fail={result.get('failures')}"
                )

            if ctx.interaction:
                await ctx.send(message, ephemeral=True)
            else:
                await ctx.send(message)

        # @self.hybrid_command(name="pamiec", aliases=["zapamietaj", "summary"], with_app_command=True)
        # async def pamiec(ctx: commands.Context) -> None:
        #     if ctx.author.id != self.config.admin_user_id:
        #         if ctx.interaction:
        #             await ctx.send("nie dla ciebie", ephemeral=True)
        #         else:
        #             await ctx.send("nie dla ciebie")
        #         return
        #     if ctx.guild is None:
        #         if ctx.interaction:
        #             await ctx.send("brak gildii", ephemeral=True)
        #         else:
        #             await ctx.send("brak gildii")
        #         return

        #     if ctx.interaction:
        #         await ctx.defer()
        #     processed = await self._summarize_recent_messages(ctx.guild.id)
        #     message = f"ok, zapisane {processed}" if processed > 0 else "brak nowych"
        #     if ctx.interaction:
        #         await ctx.send(message, ephemeral=True)
        #     else:
        #         await ctx.send(message)

        @self.hybrid_command(name="api", aliases=["apiinfo", "requests"], with_app_command=True)
        async def api(ctx: commands.Context) -> None:
            stats = self.llm.stats
            embed = discord.Embed(title="api", color=discord.Color.dark_teal())
            embed.add_field(name="chat", value=str(stats.chat_requests), inline=True)
            embed.add_field(name="vision", value=str(stats.vision_requests), inline=True)
            embed.add_field(name="memory", value=str(stats.memory_requests), inline=True)
            embed.add_field(name="tool", value=str(stats.tool_calls), inline=True)
            embed.add_field(name="fail", value=str(stats.failures), inline=True)
            embed.add_field(name="tokens", value=str(stats.total_tokens), inline=True)
            embed.add_field(name="prompt", value=str(stats.prompt_tokens), inline=True)
            embed.add_field(name="completion", value=str(stats.completion_tokens), inline=True)
            embed.add_field(name="total time", value=f"{stats.total_time:.3f}s", inline=True)
            embed.add_field(name="queue", value=f"{stats.queue_time:.3f}s", inline=True)

            await ctx.send(embed=embed)

        # @self.hybrid_command(name="losowytest", aliases=["testping"], with_app_command=True)
        # @commands.cooldown(1, 600, commands.BucketType.guild)
        # @app_commands.checks.cooldown(1, 600.0, key=lambda interaction: interaction.guild_id or 0)
        # async def losowytest(ctx: commands.Context) -> None:
        #     if ctx.interaction:
        #         await ctx.defer()
        #     await self._run_random_ping(ctx.guild, ctx.channel)

        # @self.hybrid_command(name="pingtest", aliases=["pinguj"], with_app_command=True)
        # @commands.cooldown(1, 120, commands.BucketType.user)
        # @app_commands.checks.cooldown(1, 120.0, key=lambda interaction: interaction.user.id)
        # async def pingtest(ctx: commands.Context) -> None:
        #     if ctx.interaction:
        #         await ctx.defer()
        #     await asyncio.sleep(5)
        #     await ctx.send(f"<@{ctx.author.id}> test ping")

        @self.tree.error
        async def _on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            if isinstance(error, app_commands.CommandOnCooldown):
                retry_after = max(1, int(error.retry_after))
                message = f"spokojnie, za {retry_after}s"
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
                return

            raise error

    def _register_tools(self) -> None:
        @self.tools.register(
            name="ping_target",
            description="ping the current target user in the current channel",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
        async def ping_target(_args: dict[str, str]) -> str:
            context = self._tool_context

            if context is None:
                return "no_context"
            if context.ping_used:
                return "already_pinged"
            channel = self.get_channel(context.channel_id)
            if channel is None:
                return "no_channel"
            context.ping_used = True

            await channel.send(f"<@{context.target_user_id}>")
            
            return "ok"

        @self.tools.register(
            name="send_image",
            description="send an image or gif to the current channel",
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["auto", "kranus", "recent", "url"],
                        "description": "Where to get the image",
                    },
                    "url": {"type": "string", "description": "Direct image URL when source=url"},
                    "caption": {"type": "string", "description": "Optional short caption"},
                    "ping_target": {"type": "boolean", "description": "Ping target user if available"},
                },
                "additionalProperties": False,
            },
        )
        async def send_image(args: dict[str, Any]) -> str:
            context = self._tool_context

            if context is None:
                return "no_context"

            channel = self.get_channel(context.channel_id)
            if channel is None:
                return "no_channel"

            source = str(args.get("source", "auto")).strip().lower() or "auto"
            caption = str(args.get("caption", "")).strip()
            ping_target = bool(args.get("ping_target", False))
            url = ""

            if source == "url":
                url = str(args.get("url", "")).strip()
                if not self._is_image_url(url):
                    return "bad_url"
            else:
                url = await self._pick_image_url(context.guild_id, source=source)

            if not url:
                return "no_image"

            content = url
            if caption:
                content = f"{caption} {url}".strip()
            if ping_target:
                content = f"<@{context.target_user_id}> {content}"

            await channel.send(content)
            context.image_used = True

            return "ok"

    async def setup_hook(self) -> None:
        await self.memory.open()

        self.idle_watchdog.start()
        self.random_ping_watchdog.start()
        
        await self.tree.sync()

    async def close(self) -> None:
        self.idle_watchdog.cancel()
        self.random_ping_watchdog.cancel()

        await self.memory.close()
        await super().close()

    async def on_ready(self) -> None:
        print(f"logged in as {self.user}")

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None:
            self._track_images(message)

        if message.author.bot:
            if self.user is None or message.author.id == self.user.id:
                return
            should_reply = self.user.mentioned_in(message)
            if not should_reply:
                should_reply = await self._is_reply_to_self(message)
            if should_reply:
                await self._reply_to_mention(message)
            return

        if message.guild is not None:
            guild_id = message.guild.id
            self.last_activity[guild_id] = _now()
            self.recent_participants[guild_id].append((str(message.author.id), message.author.display_name))
            normalized_content = self._normalize_message_content(message)
            self.recent_messages[guild_id].append(
                MessageRecord(
                    message_id=message.id,
                    channel_id=message.channel.id,
                    user_id=str(message.author.id),
                    username=message.author.display_name,
                    content=normalized_content,
                    created_at=_now().isoformat(),
                )
            )
            self.message_counter[guild_id] += 1
            if self.message_counter[guild_id] % self.config.memory_batch_size == 0:
                asyncio.create_task(self._summarize_recent_messages(guild_id))

        if message.content and str(message.content).startswith(self.command_prefix):
            await self.process_commands(message)
            return

        if message.guild is not None and message.attachments:
            image_attachment = next((attachment for attachment in message.attachments if _is_image_attachment(attachment)), None)
            if image_attachment is not None and self.user is not None and self.user.mentioned_in(message):
                await self._reply_with_vision(message, image_attachment)
                return

        if self.user is not None and self.user.mentioned_in(message):
            await self._reply_to_mention(message)
            return

        if message.guild is not None and random.random() < self.config.random_reply_chance:
            await self._random_interruption(message)

        await self.process_commands(message)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.CommandOnCooldown):
            retry_after = max(1, int(error.retry_after))
            await ctx.send(f"spokojnie, za {retry_after}s")
            return
        
        await super().on_command_error(ctx, error)

    async def _build_user_memory_context(self, user_id: str, discord_name: str) -> str:
        user_memory = await self.memory.get_user_memory(user_id)
        
        if user_memory is None:
            return f"cel: {discord_name}\nbraki w teczce"

        dirt = " | ".join(user_memory.dirt) if user_memory.dirt else "brak brudow"
        
        return (
            f"cel: {user_memory.discord_name}\n"
            f"general vibe: {user_memory.general_vibe}\n"
            f"brudy: {dirt}"
        )

    def _recent_context(self, guild_id: int, limit: int = 8) -> str:
        records = list(self.recent_messages[guild_id])[-limit:]
        
        if not records:
            return ""
        
        return "\n".join(f"{record.username}: {record.content}" for record in records if record.content)

    async def _reply_to_mention(self, message: discord.Message) -> None:
        guild_id = message.guild.id if message.guild is not None else 0
      
        async with message.channel.typing():
            tool_context = ToolContext(
                guild_id=guild_id,
                channel_id=message.channel.id,
                target_user_id=message.author.id,
            )
            self._tool_context = tool_context
            try:
                memory_context = await self._build_user_memory_context(str(message.author.id), message.author.display_name)
                recent_context = self._recent_context(guild_id)
                normalized_content = self._normalize_message_content(message)
                system_prompt = self.llm.build_system_prompt(
                    memory_context=(
                        f"{memory_context}\n\n"
                        f"ostatni kanal:\n{recent_context}"
                        if recent_context
                        else memory_context
                    )
                )
                user_prompt = (
                    "odpisz krotko z humorem dobierz ton do sytuacji "
                    "czasem zlosliwie ale bez ciaglego bluzgania "
                    "jesli jest obrazek uwzglednij go a jak pasuje wrzuc gif\n\n"
                    f"{normalized_content}"
                )
                image_bytes = await self._first_image_bytes(message)
                if image_bytes is not None:
                    reply = await self.llm.analyze_image(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        image_bytes=image_bytes,
                    )
                else:
                    reply = await self.llm.generate_reply(
                        model=self.config.groq_chat_model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=0.85,
                        max_tokens=70,
                    )
            finally:
                self._tool_context = None

        if tool_context.ping_used:
            return
   
        safe_reply = discord.utils.escape_mentions(reply)
   
        await message.reply(safe_reply[: self.config.mention_reply_limit], mention_author=False)

        if not tool_context.image_used:
            await self._maybe_send_image_reply(message, guild_id, chance=self.config.image_reply_chance)

    async def _reply_with_vision(self, message: discord.Message, attachment: discord.Attachment) -> None:
        guild_id = message.guild.id if message.guild is not None else 0
     
        async with message.channel.typing():
            tool_context = ToolContext(
                guild_id=guild_id,
                channel_id=message.channel.id,
                target_user_id=message.author.id,
            )
            self._tool_context = tool_context
            try:
                memory_context = await self._build_user_memory_context(str(message.author.id), message.author.display_name)
                recent_context = self._recent_context(guild_id)
                system_prompt = self.llm.build_system_prompt(
                    memory_context=(
                        f"{memory_context}\n\n"
                        f"ostatni kanal:\n{recent_context}"
                        if recent_context
                        else memory_context
                    )
                )
                image_bytes = await attachment.read()
                reply = await self.llm.analyze_image(
                    system_prompt=system_prompt,
                    user_prompt="skomentuj obrazek krotko z humorem nie zawsze zlosliwie",
                    image_bytes=image_bytes,
                )
            finally:
                self._tool_context = None
    
        if tool_context.ping_used:
            return
    
        safe_reply = discord.utils.escape_mentions(reply)
    
        await message.reply(safe_reply[: self.config.mention_reply_limit], mention_author=False)

    async def _random_interruption(self, message: discord.Message) -> None:
        guild_id = message.guild.id if message.guild is not None else 0
        recent_context = self._recent_context(guild_id)
      
        if not recent_context:
            return
      
        tool_context = ToolContext(
            guild_id=guild_id,
            channel_id=message.channel.id,
            target_user_id=message.author.id,
        )
        self._tool_context = tool_context
      
        try:
            memory_context = await self._build_user_memory_context(str(message.author.id), message.author.display_name)
            system_prompt = self.llm.build_system_prompt(
                memory_context=(
                    f"{memory_context}\n\n"
                    f"ostatni kanal:\n{recent_context}"
                    if recent_context
                    else memory_context
                )
            )
            reply = await self.llm.generate_reply(
                model=self.config.groq_chat_model,
                system_prompt=system_prompt,
                user_prompt=(
                    "wtrac sie krotko do rozmowy dobierz humor do kontekstu "
                    "nie zawsze wulgarnie i jesli pasuje uzyj pamieci "
                    "mozesz tez wrzucic gif"
                ),
                temperature=0.95,
                max_tokens=40,
                enable_tools=True,
            )
        finally:
            self._tool_context = None
      
        if message.guild is not None and message.guild.me is not None:
            if message.channel.permissions_for(message.guild.me).send_messages:
                if tool_context.ping_used:
                    return

                safe_reply = discord.utils.escape_mentions(reply)
                
                await message.channel.send(safe_reply)

                if not tool_context.image_used:
                    await self._maybe_send_image_message(
                        message.channel,
                        guild_id,
                        chance=self.config.image_reply_chance,
                    )

    def _record_time(self, record: MessageRecord) -> datetime | None:
        try:
            return datetime.fromisoformat(record.created_at)
        except ValueError:
            return None

    def _recent_records(self, guild_id: int, since: datetime) -> list[MessageRecord]:
        records: list[MessageRecord] = []

        for record in self.recent_messages[guild_id]:

            if not record.content:
                continue

            record_time = self._record_time(record)

            if record_time is None or record_time < since:
                continue

            records.append(record)

        return records

    def _pick_recent_participant(self, guild_id: int, since: datetime) -> MessageRecord | None:
        current_bot_id = str(self.user.id) if self.user else ""
        records = self._recent_records(guild_id, since)

        if not records:
            return None

        latest_by_user: dict[str, MessageRecord] = {}

        for record in records:
            if record.user_id == current_bot_id:
                continue
            existing = latest_by_user.get(record.user_id)
            if existing is None:
                latest_by_user[record.user_id] = record
                continue
            existing_time = self._record_time(existing)
            record_time = self._record_time(record)
            if record_time is not None and existing_time is not None and record_time > existing_time:
                latest_by_user[record.user_id] = record

        if not latest_by_user:
            return None

        return random.choice(list(latest_by_user.values()))

    def _resolve_ping_channel(
        self,
        guild: discord.Guild,
        fallback: discord.abc.Messageable | None,
        preferred_channel_id: int,
    ) -> discord.abc.Messageable | None:
        channel = self.get_channel(preferred_channel_id) if preferred_channel_id else None

        if channel is None:
            channel = fallback

        if channel is None:
            return None

        me = guild.me
        if me is not None and hasattr(channel, "permissions_for"):
            if not channel.permissions_for(me).send_messages:
                return None

        return channel

    async def _maybe_ping_allowed_bot(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable | None,
    ) -> bool:
        bot_id = self.config.allowed_bot_id

        if not bot_id:
            return False
        if self.config.allowed_bot_ping_chance <= 0:
            return False
        if random.random() >= self.config.allowed_bot_ping_chance:
            return False
        if guild.get_member(bot_id) is None:
            return False

        resolved_channel = self._resolve_ping_channel(guild, channel, 0)
        if resolved_channel is None:
            return False

        await resolved_channel.send(f"<@{bot_id}>")

        return True

    def _next_random_time(self, base: datetime) -> datetime:
        delay_seconds = random.uniform(600, 36000)
        candidate = base + timedelta(seconds=delay_seconds)
       
        if 12 <= candidate.hour <= 23:
            return candidate
       
        next_day = candidate.date()
       
        if candidate.hour > 23:
            next_day = (candidate + timedelta(days=1)).date()
       
        base_tz = base.tzinfo or timezone.utc
        start = datetime.combine(next_day, datetime.min.time(), tzinfo=base_tz)
       
        return start.replace(hour=12) + timedelta(seconds=delay_seconds % 36000)

    async def _run_random_ping(self, guild: discord.Guild | None, channel: discord.abc.Messageable | None) -> None:
        if guild is None:
            return

        now = _now()
        recent_window = timedelta(minutes=30)
        target_record = self._pick_recent_participant(guild.id, now - recent_window)
        if target_record is None:
            return

        resolved_channel = self._resolve_ping_channel(guild, channel, target_record.channel_id)
        if resolved_channel is None:
            return

        image_sent = await self._maybe_send_image_message(
            resolved_channel,
            guild.id,
            chance=self.config.random_image_chance,
            ping_user_id=target_record.user_id,
        )
        if image_sent:
            return

        await resolved_channel.send(f"<@{target_record.user_id}>")

    @tasks.loop(seconds=60)
    async def random_ping_watchdog(self) -> None:
        now = _now_local()
       
        if not (12 <= now.hour <= 23):
            return
       
        for guild in self.guilds:
            if guild.id not in self.next_random_ping:
                self.next_random_ping[guild.id] = self._next_random_time(now)
                continue
            if now < self.next_random_ping[guild.id]:
                continue
            channel = self._pick_text_channel(guild)
            try:
                pinged = await self._maybe_ping_allowed_bot(guild, channel)
                if not pinged:
                    await self._run_random_ping(guild, channel)
            except Exception:
                pass
            self.next_random_ping[guild.id] = self._next_random_time(now)

    @random_ping_watchdog.before_loop
    async def _before_random_ping_watchdog(self) -> None:
        await self.wait_until_ready()

    async def _first_image_bytes(self, message: discord.Message) -> bytes | None:
        for attachment in message.attachments:
            if _is_image_attachment(attachment):
                return await attachment.read()

        for url in self._collect_message_image_urls(message):
            image_bytes = await self._fetch_image_bytes(url)
            if image_bytes is not None:
                return image_bytes
        return None

    async def _summarize_recent_messages(self, guild_id: int) -> int:
        if guild_id in self._summarize_inflight:
            return 0
        self._summarize_inflight.add(guild_id)
       
        try:
            last_id = int(self.last_summarized_id.get(guild_id, 0) or 0)
            records = [record for record in self.recent_messages[guild_id] if record.message_id > last_id]
       
            if len(records) < 4:
                return 0
            transcript = "\n".join(
                f"user_id={record.user_id} name={record.username} text={record.content}"
                for record in records
                if record.content
            )
       
            if not transcript.strip():
                self.last_summarized_id[guild_id] = records[-1].message_id
                return 0
       
            try:
                payload = await self.llm.extract_memory(transcript)
            except Exception:
                return 0

            self.last_summarized_id[guild_id] = records[-1].message_id

            await self._apply_memory_payload(payload)

            return len(records)
        finally:
            self._summarize_inflight.discard(guild_id)

    @tasks.loop(seconds=60)
    async def idle_watchdog(self) -> None:
        now = _now()
     
        for guild in self.guilds:
            if guild.id not in self.last_activity:
                continue
            if (now - self.last_activity[guild.id]).total_seconds() < self.config.idle_seconds:
                continue
            if (now - self.last_proactive.get(guild.id, datetime.fromtimestamp(0, tz=timezone.utc))).total_seconds() < self.config.idle_seconds:
                continue

            target = self._pick_active_participant(guild.id)
            if target is None:
                continue
            channel = self._pick_text_channel(guild)
            if channel is None:
                continue

            memory_context = await self._build_user_memory_context(target[0], target[1])
            recent_context = self._recent_context(guild.id)
            system_prompt = self.llm.build_system_prompt(
                memory_context=(
                    f"{memory_context}\n\n"
                    f"ostatni kanal:\n{recent_context}"
                    if recent_context
                    else memory_context
                )
            )
            tool_context = None
            try:
                target_user_id = int(target[0])
                tool_context = ToolContext(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    target_user_id=target_user_id,
                )
            except Exception:
                tool_context = None

            if tool_context is not None:
                self._tool_context = tool_context

            try:
                reply = await self.llm.generate_reply(
                    model=self.config.groq_chat_model,
                    system_prompt=system_prompt,
                    user_prompt=(
                        f"zaczep uzytkownika @{target[1]} krotko "
                        "dobierz humor do sytuacji nie zawsze wulgarnie "
                        "mozna tez wrzucic gif"
                    ),
                    temperature=1.0,
                    max_tokens=28,
                    enable_tools=True,
                )
            except Exception:
                continue
            finally:
                if tool_context is not None:
                    self._tool_context = None
            
            safe_reply = discord.utils.escape_mentions(reply)
            
            await channel.send(f"<@{target[0]}> {safe_reply}")
            
            self.last_proactive[guild.id] = now

    @idle_watchdog.before_loop
    async def _before_idle_watchdog(self) -> None:
        await self.wait_until_ready()

    def _pick_active_participant(self, guild_id: int) -> tuple[str, str] | None:
        current_bot_id = self.user.id if self.user else 0
        participants = [participant for participant in self.recent_participants[guild_id] if participant[0] != str(current_bot_id)]
        
        if not participants:
            return None
        
        return random.choice(participants)

    def _pick_text_channel(self, guild: discord.Guild):
        me = guild.me
  
        if me is None:
            return None
  
        channels = [channel for channel in guild.text_channels if channel.permissions_for(me).send_messages]
  
        if not channels:
            return None
        return random.choice(channels)

    def _normalize_message_content(self, message: discord.Message) -> str:
        content = (message.clean_content or message.content or "").strip()
        return self._replace_numeric_mentions(content, message.guild)

    def _replace_numeric_mentions(self, content: str, guild: discord.Guild | None) -> str:
        if not content:
            return ""
        if guild is None:
            return content

        def _replace(match: re.Match[str]) -> str:
            user_id = match.group(1)
            try:
                member = guild.get_member(int(user_id))
            except Exception:
                member = None
            if member is not None:
                return f"@{member.display_name}({user_id})"
            return f"@{user_id}"

        return _NUMERIC_PING_RE.sub(_replace, content)

    def _track_images(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        for url in self._collect_message_image_urls(message):
            self._remember_image(message.guild.id, url)

    def _remember_image(self, guild_id: int, url: str) -> None:
        if not url:
            return
        if not self._is_image_url(url):
            return
        cache = self.recent_images[guild_id]
        if url in cache:
            return
        cache.append(url)

    def _collect_message_image_urls(self, message: discord.Message) -> list[str]:
        urls: list[str] = []

        for attachment in message.attachments:
            if _is_image_attachment(attachment):
                urls.append(attachment.url)

        if message.content:
            urls.extend(self._extract_image_urls(message.content))

        for embed in message.embeds:
            image_url = getattr(getattr(embed, "image", None), "url", None)
            if image_url:
                urls.append(image_url)
            thumb_url = getattr(getattr(embed, "thumbnail", None), "url", None)
            if thumb_url:
                urls.append(thumb_url)

        deduped = list(dict.fromkeys(urls))
        return [url for url in deduped if self._is_image_url(url)]

    def _extract_image_urls(self, text: str) -> list[str]:
        if not text:
            return []
        urls: list[str] = []
        for match in _IMAGE_URL_RE.finditer(text):
            url = match.group(0).rstrip(")].,!?\"'")
            if self._is_image_url(url):
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _is_image_url(self, url: str) -> bool:
        if not url:
            return False
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS):
            return True
        if "discordapp" in parsed.netloc:
            qs = urllib.parse.parse_qs(parsed.query)
            fmt = (qs.get("format", [""])[0] or "").lower()
            if fmt in {"png", "jpg", "jpeg", "gif", "webp"}:
                return True
        return False

    async def _fetch_image_bytes(self, url: str) -> bytes | None:
        if not self._is_image_url(url):
            return None

        max_bytes = self.config.max_image_bytes

        def _fetch() -> bytes | None:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Linux; DiscordBot)"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    return None
                return data

        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            return None

    async def _get_kranus_images(self) -> list[str]:
        now = _now()
        cache_age = timedelta(minutes=30)

        if self._kranus_cache and self._kranus_cache_time:
            if now - self._kranus_cache_time < cache_age:
                return self._kranus_cache

        def _fetch() -> str:
            req = urllib.request.Request(
                _KRANUS_IMAGES_URL,
                headers={"User-Agent": "Mozilla/5.0 (Linux; DiscordBot)"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.read().decode("utf-8", errors="ignore")

        try:
            html_text = await asyncio.to_thread(_fetch)
        except Exception:
            return self._kranus_cache

        urls: list[str] = [
            f"https://kranus.pro/fotki/{index}.gif" for index in range(1, 76)
        ]
        for match in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html_text, flags=re.IGNORECASE):
            candidate = urllib.parse.urljoin(_KRANUS_IMAGES_URL, match)
            if self._is_image_url(candidate):
                urls.append(candidate)

        urls = list(dict.fromkeys(urls))
        if urls:
            self._kranus_cache = urls
            self._kranus_cache_time = now

        return self._kranus_cache

    async def _pick_image_url(self, guild_id: int, source: str = "auto") -> str:
        source = (source or "auto").lower()

        if source == "recent":
            recent = list(self.recent_images[guild_id])
            return random.choice(recent) if recent else ""

        if source == "kranus":
            kranus = await self._get_kranus_images()
            return random.choice(kranus) if kranus else ""

        if source == "auto":
            recent = list(self.recent_images[guild_id])
            kranus = await self._get_kranus_images()
            if recent and (not kranus or random.random() < 0.6):
                return random.choice(recent)
            if kranus:
                return random.choice(kranus)
            if recent:
                return random.choice(recent)

        return ""

    async def _maybe_send_image_reply(
        self,
        message: discord.Message,
        guild_id: int,
        *,
        chance: float,
    ) -> bool:
        return await self._maybe_send_image_message(
            message.channel,
            guild_id,
            chance=chance,
            reply_to=message,
        )

    async def _maybe_send_image_message(
        self,
        channel: discord.abc.Messageable,
        guild_id: int,
        *,
        chance: float,
        ping_user_id: str | int | None = None,
        reply_to: discord.Message | None = None,
        source: str = "auto",
        caption: str = "",
    ) -> bool:
        if chance <= 0 or random.random() >= chance:
            return False

        url = await self._pick_image_url(guild_id, source=source)
        if not url:
            return False

        content = url
        if caption:
            content = f"{caption} {url}".strip()
        if ping_user_id is not None:
            content = f"<@{ping_user_id}> {content}"

        if reply_to is not None:
            await reply_to.reply(content, mention_author=False)
        else:
            await channel.send(content)

        if self._tool_context is not None:
            self._tool_context.image_used = True

        return True

    async def _is_reply_to_self(self, message: discord.Message) -> bool:
        if self.user is None:
            return False
        if message.reference is None or message.reference.message_id is None:
            return False
        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved.author.id == self.user.id
        try:
            referenced = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            return False
        return referenced.author.id == self.user.id

    async def _apply_memory_payload(self, payload: dict[str, Any]) -> tuple[int, int]:
        profiles_added = 0
        dirt_added = 0

        for profile in payload.get("profiles", []):
            user_id = str(profile.get("user_id", "")).strip()
            discord_name = str(profile.get("discord_name", "")).strip()
            general_vibe = str(profile.get("general_vibe", "")).strip()
            if user_id and discord_name and general_vibe:
                try:
                    await self.memory.upsert_profile(user_id, discord_name, general_vibe)
                except Exception:
                    continue
                profiles_added += 1

        for dirt in payload.get("dirt", []):
            user_id = str(dirt.get("user_id", "")).strip()
            memory_text = str(dirt.get("memory_text", "")).strip()
            if user_id and memory_text:
                try:
                    await self.memory.add_dirt(user_id, memory_text)
                except Exception:
                    continue
                dirt_added += 1

        return profiles_added, dirt_added

    def _build_memory_batches(
        self,
        records: list[MessageRecord],
        max_chars: int = 6000,
        max_lines: int = 120,
    ) -> list[str]:
        batches: list[str] = []
        lines: list[str] = []
        size = 0

        for record in records:
            if not record.content:
                continue
            line = f"user_id={record.user_id} name={record.username} text={record.content}"
            if lines and (size + len(line) + 1 > max_chars or len(lines) >= max_lines):
                batches.append("\n".join(lines))
                lines = []
                size = 0
            lines.append(line)
            size += len(line) + 1

        if lines:
            batches.append("\n".join(lines))

        return batches

    async def _learn_from_channel_history(
        self,
        channel: discord.abc.Messageable,
        limit: int = 1000,
    ) -> dict[str, int | str]:
        guild_id = getattr(getattr(channel, "guild", None), "id", 0)
        if guild_id in self._summarize_inflight:
            return {"messages": 0, "batches": 0, "profiles": 0, "dirt": 0, "failures": 0, "status": "busy"}

        self._summarize_inflight.add(guild_id)

        records: list[MessageRecord] = []
        try:
            async for msg in channel.history(limit=limit, oldest_first=True):
                if msg.author.bot:
                    continue
                content = self._normalize_message_content(msg)
                if not content:
                    continue
                records.append(
                    MessageRecord(
                        message_id=msg.id,
                        channel_id=msg.channel.id,
                        user_id=str(msg.author.id),
                        username=msg.author.display_name,
                        content=content,
                        created_at=_now().isoformat(),
                    )
                )

            if not records:
                return {"messages": 0, "batches": 0, "profiles": 0, "dirt": 0, "failures": 0}

            batches = self._build_memory_batches(records)
            failures = 0
            total_profiles = 0
            total_dirt = 0

            for transcript in batches:
                try:
                    payload = await self.llm.extract_memory(transcript)
                except Exception:
                    failures += 1
                    continue
                profiles_added, dirt_added = await self._apply_memory_payload(payload)
                total_profiles += profiles_added
                total_dirt += dirt_added

            return {
                "messages": len(records),
                "batches": len(batches),
                "profiles": total_profiles,
                "dirt": total_dirt,
                "failures": failures,
            }
        finally:
            self._summarize_inflight.discard(guild_id)


def run_bot() -> None:
    load_dotenv()
    config = load_config()
    bot = CipekBot(config)
    bot.run(config.discord_token)
