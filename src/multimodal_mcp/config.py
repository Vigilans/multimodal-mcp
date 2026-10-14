import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

Category = Literal["image", "asr", "tts", "video"]

CAPABILITIES: dict[str, set[str]] = {
    "image": {"generate", "edit", "reference", "mask"},
    "asr": {"text", "segment", "word", "speaker", "hotwords"},
    "tts": {"voice", "reference", "timestamps", "design"},
    "video": {"text", "frames", "reference"},
}

DEFAULT_PATH = Path.home() / ".agents" / "mcp" / "multimodal" / "config.toml"


class Provider(BaseModel):
    endpoint: Literal["openai", "minimax", "ark", "gemini"]
    # Omitted: the endpoint's official API root.
    base_url: str | None = None
    api_key: str | None = None
    env_key: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> "Provider":
        if (self.api_key is None) == (self.env_key is None):
            raise ValueError("set exactly one of api_key or env_key")
        return self

    def key(self) -> str:
        return self.api_key or os.environ[self.env_key]


class Model(BaseModel):
    provider: str
    model: str
    description: str = ""
    capabilities: list[str]


class Config(BaseModel):
    providers: dict[str, Provider] = {}
    # Aliases are unique within a category: models.<category>.<alias>.
    models: dict[Category, dict[str, Model]] = {}
    defaults: dict[Category, str] = {}

    @model_validator(mode="after")
    def _references(self) -> "Config":
        for category, models in self.models.items():
            for alias, model in models.items():
                if model.provider not in self.providers:
                    raise ValueError(f"models.{category}.{alias}.provider: unknown provider {model.provider!r}")
                unknown = set(model.capabilities) - CAPABILITIES[category]
                if unknown:
                    raise ValueError(f"models.{category}.{alias}.capabilities: unknown {sorted(unknown)}")
        for category, alias in self.defaults.items():
            if alias not in self.models.get(category, {}):
                raise ValueError(f"defaults.{category}: unknown model {alias!r}")
        return self


def load(path: str | None = None) -> Config:
    """Read the config from --config, then MULTIMODAL_MCP_CONFIG, then the default path."""
    resolved = Path(path or os.environ.get("MULTIMODAL_MCP_CONFIG") or DEFAULT_PATH).expanduser()
    with resolved.open("rb") as file:
        return Config.model_validate(tomllib.load(file))
