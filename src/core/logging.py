import logging
from rich.logging import RichHandler
from .paths import LOGS

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        logging.basicConfig(
            level=logging.INFO,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[
                RichHandler(rich_tracebacks=True, show_path=False),
                logging.FileHandler(LOGS / "pipeline.log"),
            ],
        )
        _configured = True
    return logging.getLogger(name)
