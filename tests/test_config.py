"""A model's endpoint defaults to its provider's, and capabilities, omni validation and connections follow it."""

import pytest

import multimodal_mcp.server as server_module
from multimodal_mcp.config import Config
from multimodal_mcp.endpoints import api_root


def gateway(models: dict) -> Config:
    return Config.model_validate(
        {
            "providers": {"gateway": {"endpoint": "openai", "api_key": "fixture", "base_url": "http://127.0.0.1:9"}},
            "models": models,
        }
    )


def test_model_endpoint_defaults_to_provider_and_override_selects_its_capabilities():
    config = gateway(
        {
            "image": [
                {"provider": "gateway", "model": "gpt-image-2.5-flare"},
                {"provider": "gateway", "endpoint": "ark", "model": "doubao-seedream-5-0-pro"},
            ]
        }
    )
    flare, seedream = config.models["image"]
    assert flare.endpoint == "openai"
    assert "mask" in flare.capabilities
    assert seedream.endpoint == "ark"
    assert seedream.capabilities == ["generate", "edit", "reference"]


def test_omni_model_needs_openai_endpoint():
    with pytest.raises(ValueError, match=r"models\.omni\[0\]\.endpoint"):
        gateway({"omni": [{"provider": "gateway", "endpoint": "ark", "model": "fixture-omni", "capabilities": []}]})


def test_server_connects_once_per_provider_and_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(server_module, "connect", lambda endpoint, base_url, key: calls.append((endpoint, base_url)))
    config = gateway(
        {
            "image": [
                {"provider": "gateway", "model": "gpt-image-2.5-flare"},
                {"provider": "gateway", "model": "gpt-image-2.5-sunburst"},
                {"provider": "gateway", "endpoint": "ark", "model": "doubao-seedream-5-0-pro"},
            ]
        }
    )
    server_module.build(config)
    assert sorted(calls) == [("ark", "http://127.0.0.1:9"), ("openai", "http://127.0.0.1:9")]


def router(models: dict) -> Config:
    endpoints = {"openai": "http://127.0.0.1:9/openai/v1", "ark": "http://127.0.0.1:9/ark/api/v3"}
    return Config.model_validate(
        {"providers": {"router": {"endpoints": endpoints, "api_key": "fixture"}}, "models": models}
    )


def test_server_connects_each_endpoint_at_its_base_url(monkeypatch):
    calls = []
    monkeypatch.setattr(server_module, "connect", lambda endpoint, base_url, key: calls.append((endpoint, base_url)))
    config = router(
        {
            "image": [
                {"provider": "router", "endpoint": "openai", "model": "gpt-image-2.5-flare"},
                {"provider": "router", "endpoint": "ark", "model": "doubao-seedream-5-0-pro"},
            ]
        }
    )
    server_module.build(config)
    assert sorted(calls) == [("ark", "http://127.0.0.1:9/ark/api/v3"), ("openai", "http://127.0.0.1:9/openai/v1")]


@pytest.mark.parametrize("endpoint", [None, "minimax"])
def test_model_of_an_endpoints_provider_names_one_of_its_endpoints(endpoint):
    with pytest.raises(ValueError, match=r"models\.image\[0\]\.endpoint: name one of provider 'router'"):
        router({"image": [{"provider": "router", "endpoint": endpoint, "model": "fixture", "capabilities": []}]})


@pytest.mark.parametrize(
    ("provider", "error"),
    [
        ({"api_key": "fixture"}, "exactly one of endpoint or endpoints"),
        ({"endpoint": "openai", "endpoints": {"ark": "http://127.0.0.1:9"}, "api_key": "fixture"}, "exactly one"),
        ({"endpoints": {"ark": "http://127.0.0.1:9"}, "base_url": "http://127.0.0.1:9", "api_key": "fixture"}, "drop"),
    ],
)
def test_provider_sets_endpoint_or_endpoints(provider, error):
    with pytest.raises(ValueError, match=error):
        Config.model_validate({"providers": {"router": provider}})


@pytest.mark.parametrize(
    ("endpoint", "base_url", "root"),
    [
        ("openai", "https://gateway.example.com", "https://gateway.example.com/v1"),
        ("gemini", "https://gateway.example.com/", "https://gateway.example.com/v1beta"),
        ("ark", "https://gateway.example.com", "https://gateway.example.com/api/v3"),
        ("minimax", "https://gateway.example.com", "https://gateway.example.com"),
        ("ark", "https://gateway.example.com/api/v3/plan", "https://gateway.example.com/api/v3/plan"),
        ("openai", None, None),
    ],
)
def test_api_root_completes_only_a_url_without_a_path(endpoint, base_url, root):
    assert api_root(endpoint, base_url) == root
