"""
STAGE: package
MODULE: cli
PURPOSE: `python -m skillsig <stage>` command line that dispatches to stage packages.
INPUTS: command-line arguments, config file
OUTPUTS: whatever the dispatched stage writes
RQ LINK: none (infrastructure)
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Callable
from types import ModuleType

from skillsig.config import Config, load_config
from skillsig.provenance import StageResult

log = logging.getLogger(__name__)

COMMAND_PACKAGES: dict[str, str] = {
    "data": "s0_intake",
    "registry": "s1_registry",
    "features": "s2_features",
    "dedup": "s2_features",
    "label": "s3_labeling",
    "sample": "s4_validation",
    "evaluate": "s4_validation",
    "analyze": "s5_analysis",
    "rq2": "s5_analysis",
    "export": "s6_report",
}
COMMANDS = [*COMMAND_PACKAGES, "all"]
EXIT_NOT_IMPLEMENTED = 2

Runner = Callable[..., StageResult]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillsig",
        description="GitSkills authorship-signal pipeline. Stages: " + ", ".join(COMMANDS),
    )
    parser.add_argument(
        "stage",
        choices=COMMANDS,
        metavar="{" + ",".join(COMMANDS) + "}",
        help="stage to run: " + ", ".join(COMMANDS),
    )
    parser.add_argument("--dev-sample", action="store_true", help="run on the dev sample")
    parser.add_argument("--force", action="store_true", help="rebuild even if inputs are unchanged")
    parser.add_argument("--config", metavar="PATH", help="config file (default: $SKILLSIG_CONFIG)")
    parser.add_argument("--only", metavar="MODULE", help="run a single module of the stage")
    return parser


def _import_optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and (name == exc.name or name.startswith(exc.name + ".")):
            return None
        raise


def resolve_runner(command: str, only: str | None) -> Runner | None:
    """Find the run() for a command; None if the stage (or module) is not implemented yet."""
    package = f"skillsig.{COMMAND_PACKAGES[command]}"
    if only:
        module = _import_optional(f"{package}.{only}")
        return getattr(module, "run", None) if module else None
    module = _import_optional(package)
    if module is None:
        return None
    runner = getattr(module, f"run_{command}", None)
    primary = next(c for c, p in COMMAND_PACKAGES.items() if p == COMMAND_PACKAGES[command])
    if runner is None and command == primary:
        runner = getattr(module, "run", None)
    return runner


def run_command(cfg: Config, command: str, *, dev: bool, force: bool, only: str | None) -> int:
    runner = resolve_runner(command, only)
    if runner is None:
        target = f"{command} --only {only}" if only else command
        print(f"not implemented: {target}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED
    result = runner(cfg, dev=dev, force=force)
    state = "skipped (up to date)" if result.skipped else "done"
    log.info("%s %s; rows=%s", result.stage, state, result.rows)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load_config(args.config)
    commands = list(COMMAND_PACKAGES) if args.stage == "all" else [args.stage]
    for command in commands:
        code = run_command(cfg, command, dev=args.dev_sample, force=args.force, only=args.only)
        if code != 0:
            return code
    return 0
