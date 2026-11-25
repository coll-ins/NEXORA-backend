import os
from pathlib import Path

from dotenv import load_dotenv

# `python app.py` does not read .env by itself (only the `flask` command does),
# and config.py reads the environment when it is imported. Load backend/.env
# first; variables already set in the shell win.
load_dotenv(Path(__file__).resolve().parent / ".env")

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG") == "1"
    )
