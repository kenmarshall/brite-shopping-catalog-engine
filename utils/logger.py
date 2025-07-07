import logging

_LOGGERS = {}

def get_logger(name: str) -> logging.Logger:
    """Get a configured logger."""
    if name in _LOGGERS:
        return _LOGGERS[name]
    logger = logging.getLogger(name)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[%(levelname)s] %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    _LOGGERS[name] = logger
    return logger
