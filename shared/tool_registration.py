"""Single chokepoint for registering overlay plugin tools with Hermes.

Hermes' tool registry stores whatever ``schema`` dict a plugin passes to
``ctx.register_tool`` and, in ``tools/registry.py::get_definitions()``, emits the
model-facing tool definition as ``{"type": "function", "function": {**entry.schema,
"name": entry.name}}``. The Anthropic/OpenAI serialization in
``model_tools.py`` then reads the parameters as ``schema["parameters"]["properties"]``
and does **not** spread ``entry.description`` — so both the parameter schema and the
description must live *inside* the registered schema dict, in the OpenAI **function
shape**::

    {"description": "...", "parameters": {"type": "object", "properties": {...}}}

Overlay plugins historically authored each schema as a *bare* JSON-schema object
(``{"type": "object", "properties": {...}}``) and passed it straight through. Hermes
then found no top-level ``parameters`` key and advertised every overlay tool to the
model with empty parameters — the model saw the tool name but could not pass any
argument (``query``, ``message_id``, ``mailbox``, ...). Routing every registration
through :func:`register_wrapped_tool` makes that shape impossible to get wrong.
"""

from __future__ import annotations

from typing import Any


def register_wrapped_tool(
    ctx: Any,
    *,
    name: str,
    toolset: str,
    schema: dict[str, Any],
    handler: Any,
    description: str = "",
    **kwargs: Any,
) -> None:
    """Register a tool, wrapping a bare JSON-schema into the function shape.

    ``schema`` may be either a bare JSON-schema object
    (``{"type": "object", "properties": {...}}``) or an already-wrapped function
    schema (``{"description": ..., "parameters": {...}}``). The wrap is
    idempotent: a schema that already carries a ``parameters`` key is passed
    through unchanged (its description is backfilled from ``description`` only if
    absent). Everything else (``requires_env``, ``emoji``, ``check_fn``, ...) is
    forwarded to ``ctx.register_tool`` untouched via ``**kwargs``.
    """
    if "parameters" in schema:
        wrapped: dict[str, Any] = {**schema}
        if description and "description" not in wrapped:
            wrapped["description"] = description
    else:
        wrapped = {"description": description, "parameters": schema}

    ctx.register_tool(
        name=name,
        toolset=toolset,
        schema=wrapped,
        handler=handler,
        description=description,
        **kwargs,
    )
