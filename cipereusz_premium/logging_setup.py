from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Konfiguruje logowanie dla calej aplikacji.

    Loguje na konsole zawsze, a jesli podano log_file - rowniez do
    rotowanego pliku (5MB x 5 kopii), zeby dalo sie sprawdzic co bot
    faktycznie robil (zapytania do api, bledy, akcje na kanalach itd).
    """
    root = logging.getLogger()

    if root.handlers:
        # logowanie juz skonfigurowane (np. druga inicjalizacja w testach)
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # biblioteka discord.py jest bardzo gadatliwa ponizej WARNING (heartbeaty itp)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "logowanie skonfigurowane poziom=%s plik=%s", level.upper(), log_file or "(brak)"
    )
