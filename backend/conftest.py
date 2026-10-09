"""pytest 引导：确保 backend/ 在 sys.path 上，使 `import app.*` 可用（无论从何处调用 pytest）。"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _hermetic_observability(monkeypatch):
    """单测保持无网络：置空 Langfuse 密钥，使 settings.langfuse_enabled=False，
    obs.trace/span/generation 全部走 no-op，不构造真实客户端。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "langfuse_public_key", "", raising=False)
    monkeypatch.setattr(settings, "langfuse_secret_key", "", raising=False)
