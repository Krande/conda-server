# ci_tools

The logic behind conda-server's CI, as a small, unit-testable Python package
instead of Python smeared across `shell: python` blocks in YAML. The workflows
just `pip install ./ci_tools` and call it.

Two commands:

| Command | Used by | What it does |
|---|---|---|
| `python -m ci_tools.cli pr-review` | `.github/workflows/pr-review.yaml` | Check a PR (conventional title + exactly one `release-*` label), compute the next version for information, post/update one sticky comment, set the `review_ok` output, exit non-zero if a check fails. |
| `python -m ci_tools.cli tag-on-merge` | `.github/workflows/tag-on-pr-merge.yaml` | On a merged PR, run semantic-release per the release label to bump `pyproject.toml`, tag `vX.Y.Z`, push it, and cut a GitHub Release. |

## Why

Logic in YAML can't be run, tested, or debugged locally — the empty-PR-comment
bug (semantic-release's newline-less `tag=` line corrupting the next step's
`GITHUB_OUTPUT`) needed a live PR and log forensics to find. Here it's a
one-line unit test (`test_version.py::test_isolated_env_strips_the_actions_output_handshake`).

## Design

Pure decision logic with no I/O, plus thin injectable adapters:

```
labels.py      release-* label -> bump decision        (pure)
pr_checks.py   conventional-title + one-label checks    (pure)
comment.py     render the sticky markdown body          (pure)
version.py     semantic-release wrappers (env-isolated, injectable runner)
actions_io.py  read the event payload; write GITHUB_OUTPUT (heredoc-safe)
github.py      GitHubClient protocol + REST impl + sticky upsert
gitutils.py    git helpers (injectable runner)
flows.py       pr_review()/tag_on_merge() — every side effect is a parameter
cli.py         wire the real adapters from env; argparse entrypoints
```

`flows.py` takes the GitHub client, output writer, version calculator, and git
seeder as arguments, so the whole flow runs under pytest with in-memory fakes
(`tests/fakes.py`) — no GitHub, no git, no semantic-release, no Actions runner.

## Develop & test

```sh
pixi run test-ci          # pytest ci_tools/tests
pixi run lint             # ruff (also covers ci_tools)
```

The tests need nothing installed — `tests/conftest.py` puts `src/` on the path.

## Extending

Add a command: a pure function or two for the decision, an adapter method if it
needs a new side effect, a `flows.py` function that takes those as parameters, a
`cli.py` subcommand that wires the real ones, and tests using the fakes. Keep
side effects out of the pure modules.

## Future

This is intentionally self-contained (its own `pyproject.toml`, package name
`conda-server-ci-tools`, no imports from `conda_server`) so it can be lifted into
its own repo/package later. The only conda-server-specific defaults — the
comment marker and the semantic-release config path — are overridable via the
`CI_TOOLS_MARKER` and `CI_TOOLS_CONFIG` environment variables.
