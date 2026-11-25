"""Gunicorn settings. Start from backend/:  gunicorn "app:create_app()"

`app` is the package (backend/app/), not backend/app.py, so the factory is
`app:create_app()`. Render sets PORT.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = 2
# Threads, so a slow help-bot call (up to ASSISTANT_TIMEOUT per socket read) holds
# one of 8 slots instead of one of 2 workers.
worker_class = "gthread"
threads = 4
timeout = 30
accesslog = "-"
