"""Packaged overlay contracts (consumes.yaml).

Made an importable package so contracts/consumes.yaml ships in the wheel and is
readable at runtime via importlib.resources — the env-presence allow-list and
the boot-time conformance WARN both need it after a pip install, not just in the
repo checkout.
"""
