"""The API notes of api-notes.toml, whose header describes their scope and layout."""

import fnmatch
import tomllib
from pathlib import Path

from . import matching_names, notes_name

API_NOTES = tomllib.loads((Path(__file__).parent / "api-notes.toml").read_text(encoding="utf-8"))


def model_entry(endpoint: str, model: str) -> dict:
    """The model's entry under the endpoint, the first whose name or an alias matches it as a glob; empty when it has
    none."""
    matching = matching_names(model)
    return next(
        (
            entry
            for key, entry in API_NOTES.get(endpoint, {}).get("models", {}).items()
            if any(
                fnmatch.fnmatchcase(name, notes_name(glob))
                for name in matching
                for glob in (key, *entry.get("aliases", []))
            )
        ),
        {},
    )


def rules(endpoint: str, model: str, category: str, tool: str) -> list[str]:
    """The API notes for a tool on this model: the endpoint's, then the model's, each by category, then by tool."""
    table = API_NOTES.get(endpoint, {})
    entry = model_entry(endpoint, model)
    return [rule for scope in (table, entry) for key in (category, tool) for rule in scope.get(key, [])]
