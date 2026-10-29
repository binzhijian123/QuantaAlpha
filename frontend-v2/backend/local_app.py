"""Application entrypoint for user-configurable local installations.

Frozen historical app.py stays byte-identical. This wrapper is a new runtime
component, not covered by historical V9's implementation declaration.
"""
import os
import app as backend
from system_settings import install_settings_routes
from shadow_recovery import install_shadow_recovery

settings = install_settings_routes(backend)
recovery = install_shadow_recovery(backend)
app = backend.app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BACKEND_HOST", "127.0.0.1"),
                port=int(os.environ.get("BACKEND_PORT", "8000")), log_level="info")
