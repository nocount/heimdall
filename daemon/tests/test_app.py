from fastapi.testclient import TestClient

from heimdall.app import create_app

from .conftest import FOCUS, YT_META, FakeClassifier, yt_answers


def make_client(config, answers=None):
    app = create_app(config, FakeClassifier(answers=answers or yt_answers()), clock=lambda: FOCUS)
    return TestClient(app), app


def test_classify_shadow(config):
    client, app = make_client(config, yt_answers(content_type="gaming", conf=0.8, edu=0.2))
    r = client.post("/v1/classify", json={"url": "https://www.youtube.com/watch?v=v1", "metadata": YT_META})
    body = r.json()
    assert r.status_code == 200
    assert body["verdict"] == "allow"
    assert body["would_verdict"] == "block"
    assert body["reason"] == "only 20% educational (gaming (80%))"
    assert body["shadow"] is True
    assert app.state.db.decision(body["decision_id"])["verdict"] == "block"


def test_status_and_heartbeat(config):
    client, _ = make_client(config)
    assert client.post("/v1/heartbeat", json={"client": "firefox", "version": "0.1.0"}).json()["focus"] is True
    s = client.get("/v1/status").json()
    assert s["focus"] is True
    assert s["shadow"] is True
    assert s["last_heartbeat"] == "2026-10-21T17:00:00+00:00"
    assert s["next_change"] == "2026-10-22T00:00:00+00:00"
    assert s["classifier"]["primary"] == "fake:model"


def test_web_origins_refused(config):
    client, _ = make_client(config)
    r = client.post("/v1/heartbeat", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/v1/heartbeat", json={}, headers={"Origin": "null"})
    assert r.status_code == 403
    r = client.post("/v1/heartbeat", json={}, headers={"Origin": "moz-extension://1234-abcd"})
    assert r.status_code == 200


def test_extension_preflight(config):
    client, _ = make_client(config)
    headers = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    r = client.options("/v1/classify", headers={"Origin": "moz-extension://1234-abcd", **headers})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "moz-extension://1234-abcd"
    r = client.options("/v1/classify", headers={"Origin": "https://evil.example", **headers})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


def test_text_plain_body_rejected(config):
    # A web page's "simple" no-cors request can only send text/plain. FastAPI
    # must not parse it as JSON, or the origin check would be the only guard.
    client, _ = make_client(config)
    r = client.post("/v1/heartbeat", content='{"client": "x"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 422
