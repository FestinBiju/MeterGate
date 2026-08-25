"""Application logging configuration."""

from logging.config import dictConfig


def configure_logging(log_level: str) -> None:
    """Configure concise process-wide logging without including configuration values."""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {"handlers": ["default"], "level": log_level},
        }
    )
