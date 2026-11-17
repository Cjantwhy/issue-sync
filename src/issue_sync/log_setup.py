import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The arq CLI attaches its own handler to the "arq" logger; its records also reach
    # the root handler, so every job line would be printed twice in two formats.
    logging.getLogger("arq").handlers.clear()
