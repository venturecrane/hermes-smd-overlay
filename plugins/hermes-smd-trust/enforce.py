"""Trust-ceiling enforcement.

Stub. §7 ports from ss-console/ai-employee/adapter/trust_ceiling.py.
Content classes: autonomous / draft-for-review / refused. The per-customer
ceiling is sourced from customer.yaml.scope and materialized at provisioning.
"""


def evaluate_tool_call(tool_name: str, args: dict, customer_slug: str) -> dict | None:
    """Return block directive or None. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/trust_ceiling.py")
