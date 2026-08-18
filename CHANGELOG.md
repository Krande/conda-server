# CHANGELOG



## v0.0.1 (2026-08-18)

### Fix

* fix(ci): stop semantic-release from blanking the PR-review sticky comment

On a release branch, `semantic-release version --print` (used by the pr-review
version calc) writes its own outputs (released/version/tag) to GITHUB_OUTPUT,
and the final `tag=` line has no trailing newline. Our subsequent `body_b64=`
line got concatenated onto it, so GitHub never parsed body_b64 and the sticky
comment rendered as just the marker (empty).

Run the subprocess with GITHUB_OUTPUT and GITHUB_ACTIONS scrubbed from its env
so semantic-release can&#39;t touch this step&#39;s outputs.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`eae73b0`](https://github.com/Krande/conda-server/commit/eae73b06e8c45dd566be3ac537889823340b7880))

### Unknown

* Merge pull request #1 from Krande/feat/standalone-ci-workflows

chore: make release workflows self-contained (drop AibelDevs/action-toolbox) ([`bcbf523`](https://github.com/Krande/conda-server/commit/bcbf523c47ad84b0de4a099b4467d0880bbb47e9))

* refactor(ci): extract PR-review + release logic into a tested ci_tools package

Move the logic that lived in inline `shell: python` workflow blocks into a small,
locally runnable, unit-tested package (ci_tools/), so CI behaviour can be
developed and debugged without pushing commits and reading Actions logs.

- Pure decision logic (labels, pr_checks, comment) + thin injectable adapters
  (github REST, actions_io, git, semantic-release). flows.py wires them with
  every side effect passed as an argument, so pytest exercises the whole
  pr-review / tag-on-merge flow with in-memory fakes — no GitHub, git,
  semantic-release, or Actions runner needed.
- pr-review.yaml and tag-on-pr-merge.yaml collapse to: checkout, install, run
  the CLI. No more inline Python.
- 39 unit tests (`pixi run test-ci`); `pixi run lint` now covers ci_tools too.
- Self-contained (own pyproject, package name conda-server-ci-tools, no imports
  from conda_server) so it can be lifted into its own repo later; the marker and
  semantic-release config path are overridable via CI_TOOLS_MARKER/CI_TOOLS_CONFIG.

The GITHUB_OUTPUT-corruption fix (isolating semantic-release from the Actions
output file) is now enforced by a unit test rather than a comment.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`78860f9`](https://github.com/Krande/conda-server/commit/78860f9f2999667009361a6926d4c1a8428348bf))

* ci: make release workflows self-contained (drop AibelDevs/action-toolbox)

Replace the two workflows that called AibelDevs/action-toolbox reusable
workflows with local, self-contained equivalents:

- tag-on-pr-merge.yaml: inline the semantic-version tagger. On a merged PR it
  reads the release-* label and runs python-semantic-release (config in
  action_config.toml) to bump pyproject.toml, create a vX.Y.Z tag, and cut a
  GitHub Release. The tag is pushed over SSH with the SOURCE_KEY deploy key
  (not GITHUB_TOKEN) so it triggers build-and-push.yaml.
- Remove pre-release-dispatch.yaml: its PyPI/conda/gitops targets don&#39;t apply to
  this container-first app, and build-and-push.yaml already offers on-demand
  image builds via workflow_dispatch. This also drops the last QUETZ_* refs.

No remaining dependency on AibelDevs/action-toolbox.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`ef0b393`](https://github.com/Krande/conda-server/commit/ef0b3933812e24416698559f883df0db17aa49b7))

* Initial commit: conda-server

A modern, open-source conda package server built on the rattler ecosystem.

- FastAPI backend + React 19/Vite SPA served from one origin
- Pluggable object storage (S3 / Azure / GCS / local) via obstore
- OIDC login + bearer tokens; per-channel member ACLs, mirror/import,
  upload quotas, audit log
- Multi-stage Docker image and a packaged Helm chart for Kubernetes
- CI: build → GHCR on release tag, ruff lint, pytest, and a
  self-contained PR-review workflow

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`5e93512`](https://github.com/Krande/conda-server/commit/5e935129a6692aa40f5c8903ca27da945d33e0b8))
