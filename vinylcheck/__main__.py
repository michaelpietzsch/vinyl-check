"""Entry point: `python3 -m vinylcheck` starts the web service (configuration via VINYLCHECK_CONFIG, see config.py)."""

import logging

import uvicorn

from .config import load
from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info",
                access_log=False, ws_ping_interval=20,
                timeout_graceful_shutdown=5)        # idle restart: wait at most 5 s for open connections


if __name__ == "__main__":
    main()
