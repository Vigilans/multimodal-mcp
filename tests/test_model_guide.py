"""get_model_guide keeps reference files in the server cache, with downloads replaced so no request leaves the test."""

import json

import httpx
import pytest
from fastmcp import Client

import multimodal_mcp.catalog.prompt_guides as guides_module
import multimodal_mcp.server as server_module
from multimodal_mcp.config import Config

QWEN_FILES = [spec["name"] for spec in guides_module.PROMPT_GUIDES["qwen-image"]["files"]]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def downloads(monkeypatch):
    """The downloads requested so far; each returns its file's name and lang, or raises failure when set."""
    state = {"calls": [], "failure": None}

    async def download(http, spec, lang):
        state["calls"].append((spec["name"], lang))
        if state["failure"]:
            raise state["failure"]
        return f"{spec['name']} {lang}".encode()

    monkeypatch.setattr(guides_module, "download", download)
    return state


@pytest.fixture
async def guide(tmp_path, monkeypatch, downloads):
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path / "cache")
    config = Config.model_validate(
        {
            "providers": {
                "local": {"endpoint": "openai", "api_key": "fixture", "base_url": "http://127.0.0.1:9"},
                "minimax": {"endpoint": "minimax", "api_key": "fixture", "base_url": "http://127.0.0.1:9"},
            },
            "models": {
                "image": [
                    {"provider": "local", "model": "Qwen/Qwen-Image-2.1"},
                    {"provider": "minimax", "model": "image-01"},
                ],
                "omni": [{"provider": "local", "model": "fixture-omni", "capabilities": ["image", "audio"]}],
            },
        }
    )
    async with Client(server_module.build(config)) as client:

        async def call(**args) -> dict:
            result = await client.call_tool("get_model_guide", {"lang": "en"} | args)
            return result.structured_content if result.structured_content is not None else json.loads(result.content[0].text)

        yield call


def references_dir(tmp_path):
    return tmp_path / "cache" / "references" / "qwen-image"


@pytest.mark.anyio
async def test_present_files_are_reused_from_the_cache_without_a_download(guide, downloads, tmp_path):
    directory = references_dir(tmp_path)
    directory.mkdir(parents=True)
    for name in QWEN_FILES:
        (directory / name).write_text("cached")
    result = await guide(model="Qwen/Qwen-Image-2.1", lang="zh")
    references = result["prompt_guide"]["references"]
    assert [item["status"] for item in references] == ["present"] * len(QWEN_FILES)
    assert [item["path"] for item in references] == [str(directory / name) for name in QWEN_FILES]
    assert downloads["calls"] == []


@pytest.mark.anyio
async def test_missing_files_are_downloaded_into_the_cache(guide, downloads, tmp_path):
    result = await guide(model="Qwen/Qwen-Image-2.1")
    references = result["prompt_guide"]["references"]
    assert [item["status"] for item in references] == ["downloaded"] * len(QWEN_FILES)
    for item, name in zip(references, QWEN_FILES):
        assert item["path"] == str(references_dir(tmp_path) / name)
        assert (references_dir(tmp_path) / name).read_text() == f"{name} en"
    assert downloads["calls"] == [(name, "en") for name in QWEN_FILES]


@pytest.mark.anyio
async def test_a_failed_update_keeps_the_existing_file_and_reports_the_error(guide, downloads, tmp_path):
    directory = references_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / QWEN_FILES[0]).write_text("cached")
    downloads["failure"] = httpx.ConnectError("offline")
    references = (await guide(model="Qwen/Qwen-Image-2.1", update=True))["prompt_guide"]["references"]
    kept, *missing = references
    assert kept["status"] == "present" and "offline" in kept["error"]
    assert (directory / QWEN_FILES[0]).read_text() == "cached"
    assert all(item["status"] == "missing" and "offline" in item["error"] for item in missing)
    assert not any(directory.joinpath(name).exists() for name in QWEN_FILES[1:])


@pytest.mark.anyio
async def test_a_model_without_a_family_has_no_prompt_guide(guide, downloads):
    result = await guide(model="fixture-omni")
    assert "prompt_guide" not in result
    assert downloads["calls"] == []


@pytest.mark.anyio
async def test_a_notes_only_family_gives_notes_without_references(guide, downloads):
    result = await guide(model="image-01")
    assert result["prompt_guide"]["family"] == "minimax-image"
    assert result["prompt_guide"]["notes"]
    assert "references" not in result["prompt_guide"]
    assert downloads["calls"] == []
