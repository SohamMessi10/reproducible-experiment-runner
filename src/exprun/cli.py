"""The `exprun` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from exprun import __version__

EXIT_OK = 0
EXIT_FAILED = 1        # a run failed or was interrupted, or a file couldn't be read or written
EXIT_USAGE = 2         # bad usage or an invalid config: refused before anything runs
EXIT_NOT_REPRODUCED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exprun", description="Run experiments from a config file and record "
                                     "what is needed to reproduce and compare them.")
    parser.add_argument("--version", action="version", version=f"exprun {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    check = commands.add_parser("check", help="validate a config and show what would identify its run")
    check.add_argument("config", type=Path, help="an experiment config (TOML)")
    check.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                       help="override a param, such as model.C=10 (a TOML value; quote strings); repeatable")
    check.add_argument("--json", action="store_true", help="print the result as JSON")

    run = commands.add_parser("run", help="run an experiment and record it")
    run.add_argument("config", type=Path, help="an experiment config (TOML)")
    run.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                     help="override a param, such as model.C=10 (a TOML value; quote strings); repeatable")
    run.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    run.add_argument("--timeout", type=float, metavar="SECONDS", help="stop the task after this long")
    run.add_argument("--quiet", action="store_true", help="don't echo the task's output (it is still logged)")
    return parser


def _short(sha: str | None) -> str:
    return "–" if sha is None else sha.removeprefix("sha256:")[:12]


def check_command(args) -> int:
    from exprun.codeversion import TaskError
    from exprun.config import ConfigError, load
    from exprun.hashing import InputError
    from exprun.identity import identify

    try:
        config = load(args.config, args.overrides)
        identity = identify(config)
    except (ConfigError, TaskError, InputError) as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"config": str(config.path), "name": config.name, "description": config.description,
                          "seed": config.seed, **identity.to_dict()}, indent=1, ensure_ascii=False))
        return EXIT_OK
    code = identity.code
    git = code.get("git")
    where = Path(code["file"]).name
    if "distribution" in code:
        where += f", {code['distribution']} {code['version']}"
    if git is not None:
        where += f", git {git['commit'][:7] if git['commit'] else 'with no commits'}"
        if git["dirty"]:
            where += " + uncommitted changes"
    print(f"config:      {config.path}")
    print(f"experiment:  {config.name}" + (f" ({config.description})" if config.description else ""))
    print(f"task:        {config.task}  ({where})")
    print(f"seed:        {config.seed if config.seed is not None else 'not set: one is drawn and recorded at run time'}")
    print(f"params:      {json.dumps(config.params, ensure_ascii=False)}")
    if identity.inputs:
        print("inputs:")
        width = max(len(n) for n in identity.inputs)
        for name, record in identity.inputs.items():
            files = f", {record['files']:,} files" if record["kind"] == "folder" else ""
            print(f"  {name:<{width}}  {record['path']}  ({record['bytes']:,} bytes{files})  {_short(record['sha256'])}")
    else:
        print("inputs:      none")
    if identity.fingerprint is None:
        print("fingerprint: computed at run time, once the seed is drawn")
    else:
        print(f"config hash: {_short(identity.config_hash)}")
        print(f"fingerprint: {_short(identity.fingerprint)}")
    return EXIT_OK


def _metric(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def run_command(args) -> int:
    from exprun.codeversion import TaskError
    from exprun.config import ConfigError, load
    from exprun.hashing import InputError
    from exprun.runner import execute
    from exprun.store import RunStore

    if args.timeout is not None and not args.timeout > 0:
        print("exprun: --timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_USAGE
    try:
        config = load(args.config, args.overrides)
    except ConfigError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED

    def started(folder, manifest) -> None:
        seed = manifest["spec"]["seed"]
        print(f"run {folder.id}  {config.name}  (seed {seed}{', drawn' if manifest['seed_source'] == 'drawn' else ''})",
              file=sys.stderr, flush=True)

    try:
        folder, manifest = execute(config, RunStore(args.runs), overrides=args.overrides, timeout=args.timeout,
                                   quiet=args.quiet, on_start=started)
    except (ConfigError, TaskError, InputError) as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    status, duration = manifest["status"], manifest["duration_s"]
    print(f"{status} in {duration:.2f} s", file=sys.stderr)
    if manifest["metrics"]:
        print("metrics: " + "  ".join(f"{k}={_metric(v)}" for k, v in manifest["metrics"].items()))
    if manifest["error"]:
        print(f"error: {manifest['error']['type']}: {manifest['error']['message']}", file=sys.stderr)
    outputs = manifest["outputs"]
    print(f"run folder: {folder.path}" + (f"  ({len(outputs)} output file{'s' if len(outputs) != 1 else ''})"
                                          if outputs else ""), file=sys.stderr)
    return EXIT_OK if status == "succeeded" else EXIT_FAILED


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return run_command(args)
    return check_command(args)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
