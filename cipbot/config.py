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


def load_config() -> BotConfig:
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
    )
