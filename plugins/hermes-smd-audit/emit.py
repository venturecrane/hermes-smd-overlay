"""D1 emission for audit rows.

Stub. §7 ports the implementation from ss-console/ai-employee/adapter/audit_log.py
(plus audit_emit_points.py + audit_log_immutability.py + audit_log_integrity.py).
"""


def emit_tool_event(**kwargs):
    """Write a post_tool_call audit row to per-customer D1. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/audit_log.py")


def emit_llm_event(**kwargs):
    """Write a post_llm_call audit row to per-customer D1. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/audit_log.py")
