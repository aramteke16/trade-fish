"""Launch the Trading Dashboard web server."""

import logging
import uvicorn
from tradingagents.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.INFO)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = create_app()


def main():
    uvicorn.run("run_web:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
