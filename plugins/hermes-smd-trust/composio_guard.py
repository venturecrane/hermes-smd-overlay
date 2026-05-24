"""Composio per-connection isolation runtime guard.

Stub. §7 ports from ss-console/ai-employee/adapter/connectors/composio_assertion.py.

The guard verifies that a Composio tool call's response carries the expected
connection_id for the current customer. Composio uses a shared API key per
account; tenant cross-contamination is a real risk on misconfigured calls. The
guard rejects (replaces with a refusal-result string) any response whose
connection_id does not match the per-Machine expected value.
"""


def verify_composio_response(tool_name: str, result: str, expected_connection_id: str) -> str | None:
    """Return replacement-result string on mismatch, None when valid. Stub."""
    raise NotImplementedError(
        "ported in §7 from ss-console/ai-employee/adapter/connectors/composio_assertion.py"
    )
