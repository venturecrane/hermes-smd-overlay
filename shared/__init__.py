"""Shared helpers imported by plugins; NOT a Hermes plugin itself (no register entry point).

Modules here back the per-customer D1 namespace, customer.yaml + per-profile config
loading, and env-var credential access. Plugins import from this package; nothing
in here registers with the Hermes plugin dispatcher.
"""
