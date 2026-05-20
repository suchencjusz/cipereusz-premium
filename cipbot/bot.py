from __future__ import annotations

import asyncio
import os
import random
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


@dataclass(slots=True)
class MessageRecord:
    message_id: int
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
        self.last_activity: dict[int, datetime] = {}
        self.last_proactive: dict[int, datetime] = {}
        self.message_counter: dict[int, int] = defaultdict(int)
        self.next_random_ping: dict[int, datetime] = {}
        self.last_summarized_id: dict[int, int] = defaultdict(int)
        self._summarize_inflight: set[int] = set()
        self._tool_context: ToolContext | None = None
        self._register_commands()
        self._register_tools()

    def _register_commands(self) -> None:
        @self.hybrid_command(name="pomoc", aliases=["komendy", "help"], with_app_command=True)
        async def pomoc(ctx: commands.Context) -> None:
            embed = discord.Embed(title="komendy", color=discord.Color.dark_teal())
            embed.add_field(name="info", value="/pomoc /teczka /api /pamiec", inline=False)
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
                "properties": {
                    "text": {"type": "string", "description": "short text after the ping"}
                },
                "additionalProperties": False,
            },
        )
        async def ping_target(args: dict[str, str]) -> str:
            context = self._tool_context

            if context is None:
                return "no_context"
            if context.ping_used:
                return "already_pinged"
            channel = self.get_channel(context.channel_id)
            if channel is None:
                return "no_channel"
            text = str(args.get("text", "")).strip()
            suffix = f" {text}" if text else ""
            context.ping_used = True

            await channel.send(f"<@{context.target_user_id}>{suffix}")
            
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
        if message.author.bot:
            return

        if message.guild is not None:
            guild_id = message.guild.id
            self.last_activity[guild_id] = _now()
            self.recent_participants[guild_id].append((str(message.author.id), message.author.display_name))
            self.recent_messages[guild_id].append(
                MessageRecord(
                    message_id=message.id,
                    user_id=str(message.author.id),
                    username=message.author.display_name,
                    content=(message.content or "").strip(),
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
                system_prompt = self.llm.build_system_prompt(
                    memory_context=(
                        f"{memory_context}\n\n"
                        f"ostatni kanal:\n{recent_context}"
                        if recent_context
                        else memory_context
                    )
                )
                user_prompt = f"odpisz na to krotko i chamsko\n\n{message.content or ''}"
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
                    user_prompt="skomentuj obrazek krotko i zlosliwie",
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
            system_prompt = self.llm.build_system_prompt(memory_context=f"ostatni kanal:\n{recent_context}")
            reply = await self.llm.generate_reply(
                model=self.config.groq_chat_model,
                system_prompt=system_prompt,
                user_prompt="wtrac sie w losowy sposob do tej rozmowy bardzo krotko",
                temperature=0.95,
                max_tokens=40,
                enable_tools=False,
            )
        finally:
            self._tool_context = None
      
        if message.guild is not None and message.guild.me is not None:
            if message.channel.permissions_for(message.guild.me).send_messages:
                if tool_context.ping_used:
                    return
                safe_reply = discord.utils.escape_mentions(reply)
                await message.channel.send(safe_reply)

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
        if guild is None or channel is None:
            return
    
        target = self._pick_active_participant(guild.id)
        if target is None:
            return
    
        if isinstance(channel, discord.abc.GuildChannel):
            channel_id = channel.id
        else:
            channel_id = getattr(channel, "id", 0)
    
        tool_context: ToolContext | None = None
    
        if channel_id:
            tool_context = ToolContext(
                guild_id=guild.id,
                channel_id=channel_id,
                target_user_id=int(target[0]),
            )
            self._tool_context = tool_context
        
        try:
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
            reply = await self.llm.generate_reply(
                model=self.config.groq_chat_model,
                system_prompt=system_prompt,
                user_prompt=f"zaczep uzytkownika @{target[1]} i napisz bardzo krotki zlosliwy ping",
                temperature=1.0,
                max_tokens=28,
                enable_tools=False,
            )
        finally:
            self._tool_context = None
     
        if tool_context is not None and tool_context.ping_used:
            return
     
        safe_reply = discord.utils.escape_mentions(reply)
     
        await channel.send(f"<@{target[0]}> {safe_reply}")

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

            for profile in payload.get("profiles", []):
                user_id = str(profile.get("user_id", "")).strip()
                discord_name = str(profile.get("discord_name", "")).strip()
                general_vibe = str(profile.get("general_vibe", "")).strip()
                if user_id and discord_name and general_vibe:
                    try:
                        await self.memory.upsert_profile(user_id, discord_name, general_vibe)
                    except Exception:
                        continue

            for dirt in payload.get("dirt", []):
                user_id = str(dirt.get("user_id", "")).strip()
                memory_text = str(dirt.get("memory_text", "")).strip()
                if user_id and memory_text:
                    try:
                        await self.memory.add_dirt(user_id, memory_text)
                    except Exception:
                        continue

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
            try:
                reply = await self.llm.generate_reply(
                    model=self.config.groq_chat_model,
                    system_prompt=system_prompt,
                    user_prompt=f"zaczep uzytkownika @{target[1]} i napisz bardzo krotki zlosliwy ping",
                    temperature=1.0,
                    max_tokens=28,
                    enable_tools=False,
                )
            except Exception:
                continue
            
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


def run_bot() -> None:
    load_dotenv()
    config = load_config()
    bot = CipekBot(config)
    bot.run(config.discord_token)
