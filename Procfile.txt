web: gunicorn --bind 0.0.0.0:${PORT:-3000} app:app
worker: python worker.py
