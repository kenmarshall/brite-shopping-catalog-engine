import logging

_LOGGER: logging.Logger | None = None


def get_logger(name: str = "agent") -> logging.Logger:
    global _LOGGER
    if _LOGGER is None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        )
        _LOGGER = logging.getLogger(name)
    return logging.getLogger(name)


__all__ = ["get_logger"]
