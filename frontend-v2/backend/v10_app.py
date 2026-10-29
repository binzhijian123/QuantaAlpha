"""Separate V10 entrypoint. No declaration means every trading worker stays off."""
import os
import hashlib
from pathlib import Path

_LOADED_ENTRYPOINT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
import app as backend
from system_settings import install_settings_routes
from v10_runtime import install_v10_runtime

settings = install_settings_routes(backend)
runtime = install_v10_runtime(backend, entrypoint_sha256=_LOADED_ENTRYPOINT_SHA256)
app = backend.app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('BACKEND_HOST', '127.0.0.1'),
                port=int(os.environ.get('BACKEND_PORT', '8000')), log_level='info')
