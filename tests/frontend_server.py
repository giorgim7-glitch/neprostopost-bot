"""Disposable HTTP server for frontend tests; MAX calls are fixtures only."""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from werkzeug.serving import make_server, WSGIRequestHandler

with tempfile.TemporaryDirectory(prefix='neprostopost-frontend-') as data:
    os.environ['DATA_DIR'] = data
    os.environ['MAX_BOT_TOKEN'] = 'synthetic-test-key-not-a-real-token'
    import app
    from unittest.mock import Mock
    app.get_admin_group = lambda chat_id, user_id: True
    app.max_api = lambda *args, **kwargs: Mock(ok=True, json=lambda: {'title':'Тестовая группа'})
    class QuietHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs): pass
    server = make_server('127.0.0.1', 0, app.app, request_handler=QuietHandler)
    print(server.server_port, flush=True)
    server.serve_forever()
