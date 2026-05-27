import logging


def configure_logging(level_name: str) -> None:
    logger = logging.getLogger("readio_tts")
    logger.setLevel(level_name)

    if any(getattr(handler, "_readio_handler", False) for handler in logger.handlers):
        return

    handler = logging.StreamHandler()
    handler._readio_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
