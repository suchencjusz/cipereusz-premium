from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class BotConfig:
    discord_token: str
    groq_api_key: str
    groq_chat_model: str
    groq_vision_model: str
    database_path: str
    random_reply_chance: float
    idle_seconds: int
    memory_batch_size: int
    memory_recent_messages: int
    mention_reply_limit: int
    admin_user_id: int
    allowed_bot_id: int
    allowed_bot_ping_chance: float
    image_reply_chance: float
    random_image_chance: float
    recent_images_max: int
    max_image_bytes: int


def load_config() -> BotConfig:
    bot_ping_chance = float(os.getenv("BOT_PING_CHANCE", "0.05"))
    bot_ping_chance = max(0.0, min(1.0, bot_ping_chance))

    image_reply_chance = float(os.getenv("IMAGE_REPLY_CHANCE", "0.15"))
    image_reply_chance = max(0.0, min(1.0, image_reply_chance))
    random_image_chance = float(os.getenv("RANDOM_IMAGE_CHANCE", "0.2"))
    random_image_chance = max(0.0, min(1.0, random_image_chance))
    recent_images_max = int(os.getenv("RECENT_IMAGES_MAX", "80"))
    recent_images_max = max(10, min(400, recent_images_max))
    max_image_bytes = int(os.getenv("MAX_IMAGE_BYTES", "6291456"))
    max_image_bytes = max(1048576, min(15728640, max_image_bytes))

    return BotConfig(
        discord_token=os.environ["TOKEN_DISCORD"],
        groq_api_key=os.environ["GROQ_API_KEY"],
        groq_chat_model=os.getenv("GROQ_CHAT_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        groq_vision_model=os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        database_path=os.getenv("DATABASE_PATH", "./data/cipereusz-premium.sqlite3"),
        random_reply_chance=float(os.getenv("RANDOM_REPLY_CHANCE", "0.05")),
        idle_seconds=int(os.getenv("IDLE_SECONDS", "7200")),
        memory_batch_size=int(os.getenv("MEMORY_BATCH_SIZE", "20")),
        memory_recent_messages=int(os.getenv("MEMORY_RECENT_MESSAGES", "24")),
        mention_reply_limit=int(os.getenv("MENTION_REPLY_LIMIT", "220")),
        admin_user_id=int(os.getenv("ADMIN_USER_ID", "321309474667233284")),
        allowed_bot_id=int(os.getenv("BOT_PING_ID", "1437781243751301264")),
        allowed_bot_ping_chance=bot_ping_chance,
        image_reply_chance=image_reply_chance,
        random_image_chance=random_image_chance,
        recent_images_max=recent_images_max,
        max_image_bytes=max_image_bytes,
    )
