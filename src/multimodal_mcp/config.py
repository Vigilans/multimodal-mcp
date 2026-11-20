import os
import tomllib
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from .catalog.api_notes import model_entry

Category = Literal["image", "asr", "ocr", "tts", "video", "omni"]
Endpoint = Literal["openai", "minimax", "ark", "gemini", "deepseek"]

CAPABILITIES: dict[str, set[str]] = {
    "image": {"generate", "edit", "reference", "mask"},
    "asr": {"text", "segment", "word", "speaker", "hotwords"},
    "ocr": {"text", "layout"},
    "tts": {"voice", "reference", "timestamps", "design"},
    "video": {"text", "frames", "reference"},
    "omni": {"image", "audio"},  # The media an omni model reads.
}

DEFAULT_PATH = Path.home() / ".agents" / "mcp" / "multimodal" / "config.toml"


class Provider(BaseModel):
    # The endpoint of models that name none.
    endpoint: Endpoint | None = None
    # Omitted: the endpoint's official API root. A URL without a path takes the endpoint's official root path.
    base_url: str | None = None
    # In place of endpoint and base_url: the base_url of each endpoint the provider serves. Its models name their endpoint.
    endpoints: dict[Endpoint, str] | None = None
    api_key: str | None = None
    env_key: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> "Provider":
        if (self.api_key is None) == (self.env_key is None):
            raise ValueError("set exactly one of api_key or env_key")
        return self

    @model_validator(mode="after")
    def _one_route(self) -> "Provider":
        if (self.endpoint is None) == (self.endpoints is None):
            raise ValueError("set exactly one of endpoint or endpoints")
        if self.endpoints is not None and self.base_url is not None:
            raise ValueError("endpoints gives each endpoint's base_url; drop base_url")
        return self

    def key(self) -> str:
        return self.api_key or os.environ[self.env_key]

    def url(self, endpoint: str) -> str | None:
        """The base_url the provider serves endpoint at."""
        return self.base_url if self.endpoints is None else self.endpoints[endpoint]


class Model(BaseModel):
    provider: str
    # Omitted: the provider's endpoint.
    endpoint: Endpoint | None = None
    model: str
    description: str = ""
    # Omitted: the model's capabilities in the API notes for its endpoint.
    capabilities: list[str] | None = None


class Config(BaseModel):
    providers: dict[str, Provider] = {}
    models: dict[Category, list[Model]] = {}
    defaults: dict[Category, str] = {}

    def named(self) -> dict[str, tuple[Category, Model]]:
        """Each model under its name: the model ID, or <provider>/<model> when several entries share the model ID."""
        counts = Counter(model.model for models in self.models.values() for model in models)
        named: dict[str, tuple[Category, Model]] = {}
        for category, models in self.models.items():
            for model in models:
                name = model.model if counts[model.model] == 1 else f"{model.provider}/{model.model}"
                if name in named:
                    raise ValueError(f"models.{category}: {name} is configured twice")
                named[name] = (category, model)
        return named

    @model_validator(mode="after")
    def _references(self) -> "Config":
        for category, models in self.models.items():
            for index, model in enumerate(models):
                if model.provider not in self.providers:
                    raise ValueError(f"models.{category}[{index}].provider: unknown provider {model.provider!r}")
                provider = self.providers[model.provider]
                if model.endpoint is None:
                    model.endpoint = provider.endpoint
                if provider.endpoints is not None and model.endpoint not in provider.endpoints:
                    raise ValueError(
                        f"models.{category}[{index}].endpoint: name one of provider {model.provider!r}'s endpoints: "
                        f"{', '.join(provider.endpoints)}"
                    )
                if category == "omni" and model.endpoint != "openai":
                    raise ValueError(f"models.omni[{index}].endpoint: omni models need an openai endpoint")
                if model.capabilities is None:
                    model.capabilities = model_entry(model.endpoint, model.model).get("capabilities")
                    if model.capabilities is None:
                        raise ValueError(
                            f"models.{category}[{index}]: the API notes give no {model.endpoint} capabilities for "
                            f"{model.model!r}; set capabilities"
                        )
                unknown = set(model.capabilities) - CAPABILITIES[category]
                if unknown:
                    raise ValueError(f"models.{category}[{index}].capabilities: unknown {sorted(unknown)}")
        named = self.named()
        for category, name in self.defaults.items():
            if name not in named or named[name][0] != category:
                shared = [other for other, (kind, model) in named.items() if kind == category and model.model == name]
                hint = f"; name one of {', '.join(shared)}" if shared else ""
                raise ValueError(f"defaults.{category}: unknown model {name!r}{hint}")
        return self


def load(path: str | None = None) -> Config:
    """Read the config from --config, then MULTIMODAL_MCP_CONFIG, then the default path."""
    resolved = Path(path or os.environ.get("MULTIMODAL_MCP_CONFIG") or DEFAULT_PATH).expanduser()
    with resolved.open("rb") as file:
        return Config.model_validate(tomllib.load(file))
