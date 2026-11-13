"""Reading runs back: the rows of `exprun list`, the page of `exprun show`, the table of `exprun compare`.

Everything here is a pure function of manifests, so it is easy to test. The CLI only finds the
runs and prints.

`compare` answers "what is different about these runs?":
- **Identity** is one line: the same fingerprint (and if so, whether the results also match), or
  which parts differ (config, inputs, code).
- **Then only the values that differ:** params by dotted path, the seed, input content, code
  version and environment (Python, platform, packages, recorded variables).
- **Metrics** for every run, with each run's difference from the first.
"""

from __future__ import annotations

import json
from datetime import datetime

ABSENT = object()


def flatten(value, prefix: str = "") -> dict[str, object]:
    """{"model": {"C": 1}} -> {"model.C": 1}. Lists are values, not expanded."""
    if isinstance(value, dict) and value:
        out: dict[str, object] = {}
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value} if prefix else {}


def show_value(value) -> str:
    """How a value prints: JSON, so 1, 1.0, "1" and true are all distinguishable."""
    if value is ABSENT:
        return "–"
    if isinstance(value, float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def show_plain(value) -> str:
    return "–" if value is ABSENT else str(value)


def show_metric(value) -> str:
    if value is ABSENT:
        return "–"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def when(stamp: str | None) -> str:
    if not stamp:
        return "–"
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y-%m-%d %H:%M:%S")


def seconds(value: float | None) -> str:
    if value is None:
        return "–"
    if value < 60:
        return f"{value:.2f} s"
    minutes, rest = divmod(value, 60)
    if minutes < 60:
        return f"{int(minutes)} min {rest:02.0f} s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)} h {int(minutes):02d} min"


def short(sha: str | None, n: int = 12) -> str:
    return "–" if not sha else sha.removeprefix("sha256:")[:n]


def table(rows: list[list[str]], header: list[str] | None = None, indent: str = "") -> list[str]:
    """Left-aligned columns separated by two spaces."""
    everything = ([header] if header else []) + rows
    if not everything:
        return []
    widths = [max(len(row[i]) for row in everything) for i in range(len(everything[0]))]
    return [indent + "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
            for row in everything]


# --- list ---------------------------------------------------------------------------------------

def list_row(run_id: str, manifest: dict | None, status: str, metrics_shown: int = 3) -> list[str]:
    if manifest is None:
        return [run_id, "–", status, "–", "–", ""]
    metrics = manifest.get("metrics") or {}
    shown = [f"{k}={show_metric(v)}" for k, v in list(metrics.items())[:metrics_shown]]
    if len(metrics) > metrics_shown:
        shown.append(f"+{len(metrics) - metrics_shown} more")
    return [run_id, manifest["experiment"]["name"], status, when(manifest.get("started_at")),
            seconds(manifest.get("duration_s")), "  ".join(shown)]


LIST_HEADER = ["RUN", "EXPERIMENT", "STATUS", "STARTED (UTC)", "DURATION", "METRICS"]


# --- show ---------------------------------------------------------------------------------------

def show_lines(manifest: dict, status: str, folder: str) -> list[str]:
    m = manifest
    lines = [f"run         {m['id']}", f"status      {status}"]
    if status != m["status"]:
        lines[-1] += f"  (recorded as {m['status']})"
    exp = m["experiment"]
    lines.append(f"experiment  {exp['name']}" + (f" ({exp['description']})" if exp.get("description") else ""))
    lines.append(f"started     {when(m.get('started_at'))} UTC, took {seconds(m.get('duration_s'))}")
    lines.append(f"config      {m['config']['path']}" + (f"  with --set {' '.join(m['config']['overrides'])}"
                                                        if m["config"].get("overrides") else ""))
    lines.append(f"task        {m['spec']['task']}  ({_code_line(m['code'])})")
    lines.append(f"seed        {m['spec']['seed']}" + ("  (drawn)" if m.get("seed_source") == "drawn" else ""))
    if m.get("rerun_of"):
        outcome = (m.get("reproduction") or {}).get("outcome", "not judged")
        lines.append(f"rerun of    {m['rerun_of']}: {outcome}")
    lines.append(f"fingerprint {short(m['fingerprint'], 16)}   config hash {short(m['config_hash'], 16)}")
    params = flatten(m["spec"]["params"])
    lines.append("params" + ("      none" if not params else ""))
    lines += table([[k, show_value(v)] for k, v in params.items()], indent="  ")
    lines.append("inputs" + ("      none" if not m["inputs"] else ""))
    lines += table([[name, rec["path"], f"{rec['bytes']:,} bytes" + (f", {rec['files']:,} files"
                                                                       if rec["kind"] == "folder" else ""),
                     short(rec["sha256"])] for name, rec in m["inputs"].items()], indent="  ")
    if m.get("metrics"):
        lines.append("metrics")
        lines += table([[k, show_metric(v)] for k, v in m["metrics"].items()], indent="  ")
    if m.get("outputs"):
        lines.append("outputs")
        lines += table([[path, f"{rec['bytes']:,} bytes", short(rec["sha256"])]
                        for path, rec in m["outputs"].items()], indent="  ")
    if m.get("resources"):
        r = m["resources"]
        lines.append(f"resources   cpu {r['cpu_user_s']:.2f} s user + {r['cpu_system_s']:.2f} s system, "
                     f"peak memory {r['max_rss_bytes'] / 2**20:,.1f} MiB")
    env = m["environment"]
    variables = ", ".join(f"{k}={v}" for k, v in env["variables"].items()) or "none recorded"
    lines.append(f"environment Python {env['python']['version']} on {env['platform']['system']} "
                 f"{env['platform']['machine']}, {len(env['packages'])} packages; {variables}")
    if m.get("exit_code") is not None:
        lines.append(f"exit        {m['exit_code']}" + (f" ({m['signal']})" if m.get("signal") else ""))
    if m.get("error"):
        lines.append(f"error       {m['error']['type']}: {m['error']['message']}")
        detail = m["error"].get("traceback") or m["error"].get("stderr_tail")
        if detail:
            lines += ["  " + line for line in detail.rstrip().splitlines()[-12:]]
    lines.append(f"folder      {folder}")
    return lines


def _code_line(code: dict) -> str:
    parts = [code["file"].rsplit("/", 1)[-1]]
    if code.get("distribution"):
        parts.append(f"{code['distribution']} {code['version']}")
    git = code.get("git")
    if git:
        parts.append(f"git {git['commit'][:7]}" if git.get("commit") else "git, no commits")
        if git.get("dirty"):
            parts[-1] += " + uncommitted changes"
    parts.append(f"source {short(code['source_sha256'], 8)}")
    return ", ".join(parts)


# --- compare ------------------------------------------------------------------------------------

def _code_view(code: dict) -> dict:
    git = code.get("git") or {}
    return {"source": short(code.get("source_sha256")), "commit": (git.get("commit") or "–")[:12],
            "uncommitted changes": "yes, " + short(git.get("diff_sha256"), 8) if git.get("dirty") else "no"}


def _env_view(env: dict) -> dict:
    view = {"python": env["python"]["version"],
            "platform": f"{env['platform']['system']} {env['platform']['release']} {env['platform']['machine']}"}
    view.update({f"${name}": value for name, value in env["variables"].items()})
    view.update({f"package {name}": version for name, version in env["packages"].items()})
    return view


def compare(manifests: list[dict], show_all: bool = False) -> dict:
    """The structured comparison: identity verdict, differing sections, and metrics."""
    first = manifests[0]
    fingerprints = {m["fingerprint"] for m in manifests}
    parts = []
    if len({m["config_hash"] for m in manifests}) > 1:
        parts.append("config")
    if len({json.dumps({k: v["sha256"] for k, v in m["inputs"].items()}, sort_keys=True) for m in manifests}) > 1:
        parts.append("inputs")
    if len({json.dumps(_code_view(m["code"]), sort_keys=True) for m in manifests}) > 1:
        parts.append("code")
    metrics_equal = all(m.get("metrics") == first.get("metrics") for m in manifests)
    statuses_ok = all(m["status"] == "succeeded" for m in manifests)
    sections = {
        "params": [flatten(m["spec"]["params"]) for m in manifests],
        "seed": [{"seed": m["spec"]["seed"]} for m in manifests],
        "task": [{"task": m["spec"]["task"]} for m in manifests],
        "inputs": [{name: short(rec["sha256"]) for name, rec in m["inputs"].items()} for m in manifests],
        "code": [_code_view(m["code"]) for m in manifests],
        "environment": [_env_view(m["environment"]) for m in manifests],
    }
    differing = {}
    for name, views in sections.items():
        keys = list(dict.fromkeys(k for view in views for k in view))
        rows = []
        for key in keys:
            values = [view.get(key, ABSENT) for view in views]
            same = all(_same(v, values[0]) for v in values)
            if not same or show_all:
                rows.append({"key": key, "values": values, "same": same})
        hidden = len(keys) - len(rows)
        differing[name] = {"rows": rows, "hidden_same": hidden}
    metric_names = list(dict.fromkeys(k for m in manifests for k in (m.get("metrics") or {})))
    metrics = []
    for key in metric_names:
        values = [(m.get("metrics") or {}).get(key, ABSENT) for m in manifests]
        base = values[0]
        deltas = [_delta(v, base) for v in values]
        metrics.append({"key": key, "values": values, "deltas": deltas, "same": all(_same(v, base) for v in values)})
    return {"same_fingerprint": len(fingerprints) == 1, "differing_parts": parts,
            "metrics_equal": metrics_equal, "all_succeeded": statuses_ok, "sections": differing, "metrics": metrics}


def _same(a, b) -> bool:
    """Equal, and of the same type: 1 and 1.0 (or 1 and true) count as different."""
    return type(a) is type(b) and a == b


def _delta(value, base):
    numeric = (int, float)
    if (isinstance(value, numeric) and not isinstance(value, bool) and isinstance(base, numeric)
            and not isinstance(base, bool)):
        return value - base
    return None


def verdict(result: dict, count: int) -> str:
    if result["same_fingerprint"]:
        if not result["all_succeeded"]:
            return "same fingerprint, but not every run succeeded"
        if result["metrics_equal"]:
            return f"same fingerprint and the same metrics: the {'two runs' if count == 2 else 'runs'} reproduce"
        return "same fingerprint but DIFFERENT metrics: something outside config, data and code changed the result"
    parts = result["differing_parts"]
    if not parts:
        return "different fingerprints"
    named = {"config": "the config", "inputs": "the inputs' content", "code": "the code"}
    words = [named[p] for p in parts]
    joined = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return f"different fingerprints: {joined} {'differs' if len(words) == 1 else 'differ'}"


def section_lines(result: dict, labels: list[str]) -> list[str]:
    """The differing values of a comparison, section by section (metrics aside)."""
    titles = {"params": "params", "seed": "seed", "task": "task", "inputs": "inputs (content)", "code": "code",
              "environment": "environment"}
    lines = []
    for name, section in result["sections"].items():
        rows = section["rows"]
        if not rows:
            continue
        lines.append("")
        hidden = f"  ({section['hidden_same']} identical not shown)" if section["hidden_same"] and name != "seed" else ""
        lines.append(titles[name] + hidden)
        # Params keep JSON quoting (so "1" and 1 differ visibly); hashes, versions and names print plainly.
        shown = show_value if name in ("params", "seed", "task") else show_plain
        lines += table([[row["key"], *map(shown, row["values"])] for row in rows], header=["", *labels], indent="  ")
    return lines


def compare_lines(manifests: list[dict], statuses: list[str], show_all: bool = False) -> list[str]:
    labels = [chr(ord("A") + i) if i < 26 else str(i + 1) for i in range(len(manifests))]
    result = compare(manifests, show_all)
    lines = ["runs"]
    lines += table([[label, m["id"], m["experiment"]["name"], status]
                    for label, m, status in zip(labels, manifests, statuses, strict=True)], indent="  ")
    lines.append("")
    lines.append(verdict(result, len(manifests)))
    lines += section_lines(result, labels)
    if result["metrics"]:
        lines.append("")
        lines.append("metrics" + ("  (change from A in brackets)" if len(manifests) > 1 else ""))
        rows = []
        for metric in result["metrics"]:
            cells = [metric["key"]]
            for i, (value, delta) in enumerate(zip(metric["values"], metric["deltas"], strict=True)):
                cell = show_metric(value)
                if i > 0 and delta is not None and delta != 0:
                    cell += f" ({delta:+.4g})"
                cells.append(cell)
            rows.append(cells)
        lines += table(rows, header=["", *labels], indent="  ")
    return lines
