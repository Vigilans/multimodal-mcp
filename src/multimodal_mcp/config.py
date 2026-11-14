import os
import tomllib
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from .catalog.api_notes import model_entry

Category = Literal["image", "asr", "ocr", "tts", "video", "omni"]

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
    endpoint: Literal["openai", "minimax", "ark", "gemini", "deepseek"]
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
                if category == "omni" and self.providers[model.provider].endpoint != "openai":
                    raise ValueError(f"models.omni[{index}].provider: omni models need an openai endpoint")
                if model.capabilities is None:
                    endpoint = self.providers[model.provider].endpoint
                    model.capabilities = model_entry(endpoint, model.model).get("capabilities")
                    if model.capabilities is None:
                        raise ValueError(
                            f"models.{category}[{index}]: the API notes give no {endpoint} capabilities for "
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
