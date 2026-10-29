"""Local configuration UI, independent of frozen research implementations."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import threading
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, ValidationError


PUBLIC_FIELDS = (
    "OPENAI_BASE_URL", "CHAT_MODEL", "REASONING_MODEL", "QLIB_DATA_DIR",
    "QLIB_PROVIDER_URI", "DATA_RESULTS_DIR",
)
EDITABLE_FIELDS = {*PUBLIC_FIELDS, "OPENAI_API_KEY"}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None
    CHAT_MODEL: str | None = None
    REASONING_MODEL: str | None = None
    QLIB_DATA_DIR: str | None = None
    QLIB_PROVIDER_URI: str | None = None
    DATA_RESULTS_DIR: str | None = None


class SettingsStore:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".env"
        self.lock = threading.RLock()

    def read(self) -> dict[str, str]:
        values = {k: v for k, v in dotenv_values(self.path, interpolate=False).items()
                  if v is not None} if self.path.exists() else {}
        return self.normalize_paths(values)

    def normalize_paths(self, values: dict[str, str]) -> dict[str, str]:
        values = dict(values)
        for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI", "DATA_RESULTS_DIR"):
            value = values.get(key, "")
            if value and not value.startswith("<"):
                path = Path(value).expanduser()
                values[key] = str((path if path.is_absolute() else self.root / path).resolve())
        return values

    def public(self) -> dict[str, Any]:
        values = self.read()
        key = values.get("OPENAI_API_KEY", "")
        configured = bool(key and not key.startswith("<") and key != "your-api-key")
        public = {k: values.get(k, "") for k in PUBLIC_FIELDS}
        # Imported legacy .env values may themselves contain URL credentials.
        # Never disclose these through a supposedly public configuration field.
        try:
            parts = urlsplit(public["OPENAI_BASE_URL"])
            if parts.username or parts.password or parts.query or parts.fragment:
                public["OPENAI_BASE_URL"] = ""
        except ValueError:
            public["OPENAI_BASE_URL"] = ""
        return {"env": public,
                "apiKeyConfigured": configured}

    def save(self, payload: Any) -> None:
        try:
            parsed = SettingsUpdate.model_validate(payload)
        except ValidationError:
            # Never include Pydantic's input values in credential error responses.
            raise ValueError("配置格式无效，请检查字段和文本格式") from None
        updates = {k: v for k, v in parsed.model_dump().items() if v is not None}
        for key, value in updates.items():
            if len(value) > 4096 or any(ord(c) < 32 or c in "$`" for c in value):
                raise ValueError("配置不支持控制字符、换行或 Shell 展开字符")
            if key == "OPENAI_API_KEY" and (value == "***" or "..." in value):
                raise ValueError("请填写完整密钥，或留空保留已有密钥")
        updates = {k: v.strip() for k, v in updates.items()}
        if not updates.get("OPENAI_API_KEY"):
            updates.pop("OPENAI_API_KEY", None)
        url = updates.get("OPENAI_BASE_URL")
        if url:
            try:
                parts = urlsplit(url)
                valid = (parts.scheme in {"http", "https"} and parts.hostname
                         and not parts.username and not parts.password
                         and not parts.query and not parts.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("API 地址需为 HTTP(S) Base URL，不含凭证、查询参数或片段")
        if "QLIB_DATA_DIR" in updates:
            updates["QLIB_PROVIDER_URI"] = updates["QLIB_DATA_DIR"]
        updates = self.normalize_paths(updates)
        with self.lock:
            content = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
            for key, value in updates.items():
                # JSON string quoting also makes shell consumers treat spaces/quotes
                # literally. Expansion and control characters were rejected above.
                line = f"{key}={json.dumps(value, ensure_ascii=False)}"
                pattern = rf"^{re.escape(key)}\s*=.*$"
                if re.search(pattern, content, re.MULTILINE):
                    content = re.sub(pattern, lambda _: line, content, flags=re.MULTILINE)
                else:
                    content = content.rstrip("\n") + "\n" + line + "\n"
            self.root.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                 dir=self.root, prefix=".env.", delete=False) as stream:
                    temporary = stream.name
                    os.chmod(temporary, 0o600)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

    def environment(self) -> dict[str, Any]:
        values = self.read()
        raw_path = values.get("QLIB_DATA_DIR", "")
        provider = Path(raw_path).expanduser() if raw_path and not raw_path.startswith("<") else None
        if provider and not provider.is_absolute():
            provider = self.root / provider
        structure = bool(provider and all((provider / p).exists() for p in
                                         ["calendars/day.txt", "features", "instruments"]))
        raw_results = values.get("DATA_RESULTS_DIR", "")
        results = Path(raw_results).expanduser() if raw_results and not raw_results.startswith("<") else None
        if results and not results.is_absolute():
            results = self.root / results
        ancestor = results
        while ancestor and not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        writable = bool(results and ancestor and ancestor.is_dir() and os.access(ancestor, os.W_OK)
                        and (not results.exists() or results.is_dir()))
        deps = [name for name in ["fastapi", "qlib", "lightgbm", "pandas", "openai"]
                if importlib.util.find_spec(name) is None]
        checks = [
            {"name": "Python", "passed": tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 10),
             "detail": "需要 Python 3.10 或更高版本"},
            {"name": "核心依赖", "passed": not deps,
             "detail": "未安装：" + ", ".join(deps) if deps else "核心依赖可发现（未验证模型调用）"},
            {"name": "API 配置", "passed": self.public()["apiKeyConfigured"] and
             all(bool(value) and not value.startswith("<") for value in
                 (self.public()["env"]["OPENAI_BASE_URL"], values.get("CHAT_MODEL", ""),
                  values.get("REASONING_MODEL", ""))),
             "detail": "只检查密钥、地址及聊天/推理模型是否填写，不请求外部服务"},
            {"name": "Qlib 数据结构", "passed": structure,
             "detail": "检查 calendars/day.txt、features 和 instruments；不代表数据业务资格通过"},
            {"name": "结果目录", "passed": writable,
             "detail": "检查目录或最近已存在父目录的写入权限，不创建文件"},
        ]
        return {"pythonVersion": platform.python_version(),
                "environmentName": os.environ.get("CONDA_DEFAULT_ENV", "当前 Python 环境"),
                "checks": checks, "scope": "检查后端当前运行环境和已保存配置；不安装软件、不调用 LLM、不验证盈利"}


def install_settings_routes(backend: Any, *, root: Path | None = None) -> SettingsStore:
    store = SettingsStore(root or backend.PROJECT_ROOT)
    # The frozen module's reader did not decode quoted .env values. Its file
    # remains untouched; the new local entrypoint uses the standard parser.
    backend._load_dotenv_dict = store.read
    app = backend.app
    app.router.routes[:] = [route for route in app.router.routes
                            if not (getattr(route, "path", None) == "/api/v1/system/config"
                                    and getattr(route, "methods", set()) & {"GET", "PUT"})]

    @app.get("/api/v1/system/config")
    async def get_config():
        return {"success": True, "data": {**store.public(), "experimentYaml": "",
                "factorLibraries": [Path(p).name for p in backend._find_factor_jsons()]}}

    @app.put("/api/v1/system/config")
    async def put_config(request: Request):
        try:
            store.save(await request.json())
        except (ValueError, TypeError):
            raise HTTPException(400, "配置未保存，请检查字段、地址和文本格式（不支持换行或 Shell 展开字符）") from None
        except OSError:
            raise HTTPException(500, "配置未保存，请检查后端目录的写入权限") from None
        return {"success": True, "message": "后端配置已保存，后续新任务使用新配置"}

    @app.get("/api/v1/system/environment")
    async def environment():
        return {"success": True, "data": store.environment()}

    @app.get("/api/v1/system/guide")
    async def guide():
        return FileResponse(store.root / "docs/SETUP_AND_USAGE.md", media_type="text/plain; charset=utf-8")

    @app.get("/api/v1/system/handoff")
    async def handoff():
        return FileResponse(store.root / "docs/HANDOFF_PUBLIC.md", media_type="text/plain; charset=utf-8")

    app.openapi_schema = None
    return store
