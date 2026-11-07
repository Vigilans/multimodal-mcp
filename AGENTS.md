# Maintenance

Keep agent-facing text in its owning layer: server instructions carry capabilities, preparation order, cost boundaries and failure handling; tool docstrings carry purpose, lifecycle, results and reference-reading mechanics; parameter descriptions carry meaning, format and applicability; API notes carry interface facts; family notes carry task-specific vendor section pointers and MCP mappings; the multimodal skill carries the ordered workflow and completion criteria. Give each rule one authoritative home. The skill's preparation step intentionally repeats two prerequisites: fetching each model's guide, and reading the named sections by line range.

When changing an adapter, update the affected API notes and capability data together. Give each note and capability list a source comment linking the relevant adapter, pinned upstream source or vendor document.

Download vendor references into the runtime cache. Keep downloaded files out of version control and package archives. For each new reference, add positive task-specific section pointers using actual titles or text anchors.

Development checks: `uv sync --locked`; `uvx ruff check src/multimodal_mcp tests`; `uv run pytest`; `uv build`. Changes to omni agent shell or path handling also require `uv run pytest tests/test_workspace.py` on Windows, where the Git Bash tests run.
