import json

import httpx
import pytest

from heimdall.config import ProviderConfig
from heimdall.models.systemone import Classifier, SystemOneError, make_provider

ANSWERS = {"q": {"type": "noul", "noul": 0.9}}
QUESTIONS = {"q": {"type": "noul", "instructions": "Is `x` true?"}}
ENV = {"CF_ACCOUNT_ID": "acct", "CF_API_TOKEN": "cf-token", "OPENROUTER_API_KEY": "or-key"}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_workers_ai_request_and_envelope():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"result": {"model": "clef-flash", "answers": ANSWERS}, "success": True})

    cfg = ProviderConfig(provider="workers_ai", model="@cf/cloudflare/clef-flash")
    async with client(handler) as http:
        result = await make_provider(cfg, http, ENV).ask({"x": 1}, QUESTIONS)
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/@cf/cloudflare/clef-flash"
    assert seen["auth"] == "Bearer cf-token"
    assert seen["body"] == {"model": "clef-flash", "state": {"x": 1}, "questions": QUESTIONS}
    assert result.answers == ANSWERS
    assert result.model == "workers_ai:@cf/cloudflare/clef-flash"


async def test_openrouter_unwrapped():
    def handler(req: httpx.Request):
        assert str(req.url) == "https://openrouter.ai/api/v1/systemone"
        assert json.loads(req.content)["model"] == "typesafe/jev-1.13"
        return httpx.Response(200, json={"id": "x", "answers": ANSWERS})

    cfg = ProviderConfig(provider="openrouter", model="typesafe/jev-1.13")
    async with client(handler) as http:
        assert (await make_provider(cfg, http, ENV).ask("s", QUESTIONS)).answers == ANSWERS


async def test_llamacpp_url():
    def handler(req: httpx.Request):
        assert str(req.url) == "http://127.0.0.1:8081/v1/systemone"
        return httpx.Response(200, json={"answers": ANSWERS})

    cfg = ProviderConfig(provider="local_llamacpp", model="clef-flash", url="http://127.0.0.1:8081/")
    async with client(handler) as http:
        assert (await make_provider(cfg, http, ENV).ask("s", QUESTIONS)).answers == ANSWERS


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"result": {}, "success": False, "errors": ["bad"]}),
    httpx.Response(200, json={"result": {"answers": {}}, "success": True}),  # missing answer
    httpx.Response(200, text="not json"),
])
async def test_errors_raise(response):
    cfg = ProviderConfig(provider="workers_ai", model="@cf/cloudflare/clef-flash")
    async with client(lambda req: response) as http:
        with pytest.raises(SystemOneError):
            await make_provider(cfg, http, ENV).ask("s", QUESTIONS)


async def test_missing_secret_raises():
    cfg = ProviderConfig(provider="workers_ai", model="@cf/cloudflare/clef-flash")
    async with client(lambda req: httpx.Response(200)) as http:
        with pytest.raises(SystemOneError, match="CF_ACCOUNT_ID"):
            await make_provider(cfg, http, {}).ask("s", QUESTIONS)


async def test_timeout_then_fallback():
    def primary(req):
        raise httpx.ReadTimeout("slow", request=req)

    def handler(req: httpx.Request):
        if req.url.host == "api.cloudflare.com":
            return primary(req)
        return httpx.Response(200, json={"answers": ANSWERS})

    async with client(handler) as http:
        clf = Classifier(
            make_provider(ProviderConfig(provider="workers_ai", model="@cf/cloudflare/clef-flash"), http, ENV),
            make_provider(ProviderConfig(provider="local_llamacpp", model="clef-flash",
                                         url="http://127.0.0.1:8081"), http, ENV),
        )
        result = await clf.ask("s", QUESTIONS)
    assert result.model == "local_llamacpp:clef-flash"
