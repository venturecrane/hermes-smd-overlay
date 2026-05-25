"""hermes-smd CLI entry point.

The ``hermes-smd`` console script (declared in ``pyproject.toml``) dispatches
to ``main`` in this module. Two subcommands are wired:

  hermes-smd bootstrap     — translate customer.yaml → per-profile Hermes config
  hermes-smd customer-sync — long-running R2 polling sidecar for non-structural
                             updates (see ADR 0019)

This module is REAL plumbing: argparse, validation, exit codes, and logging
work today. The underlying translation and sync actions raise
``NotImplementedError`` until §7 of the build plan ports the logic from
ss-console.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from bootstrap import translate

logger = logging.getLogger(__name__)

DEFAULT_CUSTOMER_YAML = "/opt/data/customer.yaml"
DEFAULT_SYNC_INTERVAL = 300


def _default_hermes_home() -> str:
    """Resolve the default Hermes home directory.

    Honors ``HERMES_HOME`` env var if set; otherwise falls back to ``~/.hermes``.
    """
    env_value = os.environ.get("HERMES_HOME")
    if env_value:
        return env_value
    return str(Path.home() / ".hermes")


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="hermes-smd",
        description=(
            "SMD overlay CLI for the Nous Hermes Agent. Translates customer.yaml "
            "into per-profile Hermes configuration and runs the R2 polling sidecar."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Translate customer.yaml into per-profile Hermes config (structural path).",
        description=(
            "Reads the authored customer.yaml from the Fly volume and writes one "
            "profile directory per persona under $HERMES_HOME/profiles/. Run at "
            "Machine boot and after any structural change (persona add/remove, "
            "connector backend swap, OAuth scope change). See ADR 0019."
        ),
    )
    bootstrap_parser.add_argument(
        "--customer-yaml",
        default=DEFAULT_CUSTOMER_YAML,
        help=f"Path to customer.yaml on the Fly volume (default: {DEFAULT_CUSTOMER_YAML}).",
    )
    bootstrap_parser.add_argument(
        "--hermes-home",
        default=None,
        help="Hermes home directory (default: $HERMES_HOME env var or ~/.hermes).",
    )

    sync_parser = subparsers.add_parser(
        "customer-sync",
        help="Run the R2 polling sidecar for non-structural updates.",
        description=(
            "Long-running sidecar that polls R2 for non-structural changes to "
            "customer.yaml (tone, thresholds, voice samples, in-catalog skill "
            "pin bumps) and signals Hermes with SIGHUP to reload without "
            "restart. Structural changes are rejected; re-run `bootstrap` for "
            "those. See ADR 0019."
        ),
    )
    sync_parser.add_argument(
        "--customer-yaml",
        default=DEFAULT_CUSTOMER_YAML,
        help=f"Path to customer.yaml on the Fly volume (default: {DEFAULT_CUSTOMER_YAML}).",
    )
    sync_parser.add_argument(
        "--r2-bucket",
        required=True,
        help="R2 source identifier (URL or bucket reference) to poll.",
    )
    sync_parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_SYNC_INTERVAL,
        help=f"Poll interval in seconds (default: {DEFAULT_SYNC_INTERVAL}).",
    )

    return parser


def _configure_logging(verbose: bool) -> None:
    """Wire root logging once per process invocation."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _run_bootstrap(args: argparse.Namespace) -> int:
    """Dispatch the ``bootstrap`` subcommand."""
    hermes_home = args.hermes_home or _default_hermes_home()
    logger.info(
        "hermes-smd bootstrap: customer_yaml=%s hermes_home=%s",
        args.customer_yaml,
        hermes_home,
    )
    slugs = translate.translate_customer_yaml(
        customer_yaml_path=args.customer_yaml,
        hermes_home=hermes_home,
    )
    logger.info("hermes-smd bootstrap: wrote %d profile(s): %s", len(slugs), ", ".join(slugs))
    return 0


def _run_customer_sync(args: argparse.Namespace) -> int:
    """Dispatch the ``customer-sync`` subcommand."""
    if args.interval <= 0:
        print("error: --interval must be a positive integer", file=sys.stderr)
        return 2
    logger.info(
        "hermes-smd customer-sync: customer_yaml=%s r2_bucket=%s interval=%ds",
        args.customer_yaml,
        args.r2_bucket,
        args.interval,
    )
    translate.start_customer_sync(
        customer_yaml_path=args.customer_yaml,
        r2_bucket=args.r2_bucket,
        interval=args.interval,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Parses argv and dispatches to the chosen subcommand.

    Args:
        argv: Argument list (excluding program name). Defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, non-zero on failure.
    """
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    _configure_logging(args.verbose)

    try:
        if args.command == "bootstrap":
            return _run_bootstrap(args)
        if args.command == "customer-sync":
            return _run_customer_sync(args)
        # argparse(required=True) should make this unreachable.
        parser.error(f"unknown command: {args.command}")
        return 2
    except NotImplementedError as exc:
        print(f"hermes-smd: not yet implemented: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("hermes-smd: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level CLI boundary
        logger.exception("hermes-smd: unexpected error")
        print(f"hermes-smd: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
