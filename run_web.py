"""Launch the Trading Dashboard web server."""

import uvicorn
from tradingagents.web import create_app

app = create_app()


def main():
    uvicorn.run("run_web:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
