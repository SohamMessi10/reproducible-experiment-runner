# Progress

## Status

**Milestone 0 complete: setup and design.** Next: milestone 1, config and hashing. The plan is in
[`docs/design.md`](docs/design.md).

## Milestones

### Milestone 0: setup and design

- Packaging (`pyproject.toml`): the runner itself uses the standard library only. The demo task's
  scikit-learn and NumPy are an optional `demo` extra; pytest, Hypothesis and ruff are the `dev`
  extra. Lower bounds are releases at least two weeks old on 2026-11-11.
- `docs/design.md`: the problem, scope, run folder and manifest, reproducibility rules, exit
  codes, security, testing and milestones.
- CI: lint and tests on Python 3.12, 3.13 and 3.14 (Ubuntu) and 3.13 (macOS).

## Commands verified

```
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m pytest
```

## Concepts to review

- What a seed does and doesn't fix; content hashes versus file names.
