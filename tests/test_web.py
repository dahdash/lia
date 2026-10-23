from fastapi.testclient import TestClient

from lia.web.server import app


def test_foreign_host_is_rejected():
    c = TestClient(app, base_url="http://127.0.0.1:8000")  # no lifespan: tools are not built
    assert c.get("/", headers={"host": "evil.example:8000"}).status_code == 400
    assert c.get("/").status_code == 200
