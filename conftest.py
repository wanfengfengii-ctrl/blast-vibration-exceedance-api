"""pytest 配置：

- 默认用进程内 ASGI（TestClient）做单元测试，无需启动服务；
- 容器内验收（compose 的 verify 服务）时设置 BASE_URL 指向运行中的
  api 容器，测试自动切换为黑盒 HTTP 调用。
"""

import os

import httpx
import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def post(client):
    base_url = os.environ.get("BASE_URL")

    def _post(payload):
        if base_url:
            return httpx.post(f"{base_url}/api/v1/analyze", json=payload, timeout=10)
        return client.post("/api/v1/analyze", json=payload)

    return _post


@pytest.fixture
def get(client):
    base_url = os.environ.get("BASE_URL")

    def _get(path: str):
        if base_url:
            return httpx.get(f"{base_url}{path}", timeout=10)
        return client.get(path)

    return _get
