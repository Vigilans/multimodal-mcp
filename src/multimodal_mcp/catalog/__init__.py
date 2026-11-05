"""Model data looked up by endpoint and model ID: API notes in api-notes.toml."""

import re

# A trailing date version: -YYMMDD on Ark, -YYYY-MM-DD on OpenAI snapshots.
DATE = re.compile(r"-(\d{6}|\d{4}-\d{2}-\d{2})$")


def notes_name(model: str) -> str:
    """Catalog names compare case-insensitively, after the last "/" of a repo ID or path."""
    return model.rsplit("/", 1)[-1].lower()


def matching_names(model: str) -> set[str]:
    """A model ID's names to match: with and without a trailing date version."""
    name = notes_name(model)
    return {name, DATE.sub("", name)}
