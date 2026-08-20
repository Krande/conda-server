# CHANGELOG



## v0.3.0 (2026-08-20)

### Feature

* feat(helm): add Azure Blob CORS Job for the &#34;Show files&#34; fetch

The SPA&#39;s &#34;Show files&#34; feature fetches the .conda via fetch()/XHR, which
follows the server&#39;s 302 to a cross-origin object-store URL. For Azure
that lands on https://&lt;account&gt;.blob.core.windows.net/... and the browser
blocks it unless the blob service has a CORS rule allowing the app origin
(&#34;No &#39;Access-Control-Allow-Origin&#39; header is present&#34;).

The chart already solves this for S3 via the bucketCors PutBucketCors Job,
but had no Azure equivalent. Azure Blob CORS is a blob-service
(account-level) property, not per-container, set via &#34;Set Blob Service
Properties&#34;. Add a blobCors post-install/post-upgrade Job that runs
`az storage cors clear` + `az storage cors add` (clear-then-add for
idempotency across upgrades), reusing the storage-account key from the
existing s3SecretAccessKey secret. Off by default; gated on
blobCors.enabled with required accountName + allowedOrigin.

Also: NOTES.txt warns when backend=azure and blobCors is disabled, and the
chart README + docs/deploying.md document the Azure variant (plus a
standalone `az storage cors add` command for infra-managed setups).

Validated with `helm lint` and `helm template` (enabled/disabled/missing-
required paths, and that the S3 path is unaffected).

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`391a7d7`](https://github.com/Krande/conda-server/commit/391a7d72691d70b799ec626344a7153c053d7146))

### Unknown

* Merge pull request #8 from Krande/feat/azure-blob-cors

feat: Azure Blob CORS Job for the browser &#34;Show files&#34; fetch ([`15a6165`](https://github.com/Krande/conda-server/commit/15a6165e4f4c7e6dfb20847ad819ec96b2a9a5ce))


## v0.2.1 (2026-08-20)

### Fix

* fix: force fresh frontend build per commit + checkout exact SHA

The per-branch pipeline was shipping a stale UI: images were correctly
tagged/baked with the new GIT_SHA (runtime layers rebuilt), but the frontend
build stage was served from the CI&#39;s persistent DinD BuildKit cache — COPY
frontend ./ and npm run build showed CACHED across commits even though
frontend/ changed. Result: sha-&lt;new&gt; images carried an old dist/, so UI
fixes never reached conda.krande.no despite a green build + gitops bump.

- Dockerfile: add ARG GIT_SHA before COPY frontend ./ in the frontend stage
  so its layer key changes every commit and npm run build always re-runs
  (npm ci stays cached). GIT_SHA is already passed via --build-arg.
- Workflow: git checkout --detach $GITHUB_SHA after clone so the build uses
  the exact triggering commit rather than the clone&#39;s default branch.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`b63f4b9`](https://github.com/Krande/conda-server/commit/b63f4b98dfa8526049b16bc89899a88a366cc78f))

* fix: collapse grid-item min-width so mobile Home stops overflowing

The first mobile-overflow pass fixed the InstallInstructions &lt;code&gt; and the
Channels controls row, but Home still overflowed by ~352px: the Home cards
are grid items (min-width: auto), so each resolved its automatic minimum to
its own max-content — the non-wrapping install command — forcing the single
mobile grid track to ~711px. The inner code&#39;s overflow-x-auto never engaged
because its ancestor card refused to shrink.

- Add min-w-0 to the Home grid Cards (main + search-result panels) so the
  track collapses to the container and the command scrolls internally.
- Add overflow-x-clip on the Layout root as a belt-and-suspenders guard: it
  caps any residual/future horizontal overflow from scrolling the page
  sideways (which is what pushed the account dropdown and Add-channel button
  off-screen), and unlike overflow-x-hidden it leaves overflow-y visible so
  the sticky header keeps working.

Verified at 375px in admin state: Home and Channels both 0px overflow, the
mobile drawer and Add-channel button sit within the viewport, and the header
stays position:sticky at top after scroll.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`ad5af17`](https://github.com/Krande/conda-server/commit/ad5af17c4bbce9b9e5e5332a2d21638c2f5327af))

* fix(frontend): stop mobile horizontal overflow on Home and Channels

Two right-edge elements were unreachable on narrow viewports without
zooming out, both caused by content forcing the page wider than 100vw:

- InstallInstructions: the command &lt;code&gt; is flex-1 + overflow-x-auto +
  whitespace-nowrap but had the default min-width:auto, so it never
  shrank below its full command text and overflow-x-auto never engaged
  — the row pushed the whole page wide, sending the header&#39;s right-
  anchored account dropdown off-screen. Add min-w-0 so it scrolls
  internally instead.
- Channels header controls: a fixed w-60 filter input beside a shrink-0
  &#34;Add channel&#34; button couldn&#39;t fit a ~360px screen, spilling the
  button past the right edge. Make the controls row full-width and let
  the input flex/shrink on mobile (min-w-0 flex-1), restoring the fixed
  240px width at sm+.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`2ff936e`](https://github.com/Krande/conda-server/commit/2ff936e997ac954fdc46f6de1cdaf2c12913f48e))

### Unknown

* Merge pull request #7 from Krande/fix/mobile-ui-overflow

fix: stop mobile horizontal overflow on Home and Channels ([`44dd9f2`](https://github.com/Krande/conda-server/commit/44dd9f26ed80979bc2e36702fa150e54e402fa86))

* Merge pull request #6 from Krande/feat/forgejo-deputy-gitops

chore: build + push + gitops bump on every commit via deputy ([`40e14db`](https://github.com/Krande/conda-server/commit/40e14db7c07493ee550d8d765036486344dfa5d3))

* ci(forgejo): export TAG into deputy container (fix unbound var)

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`f1f015b`](https://github.com/Krande/conda-server/commit/f1f015b5beb42b9a711218ca108688ffaf796c46))

* ci(forgejo): rename HARBOR_* secrets to REGISTRY_* (neutral)

Avoid naming the registry implementation in a public-mirror workflow.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`4f75d07`](https://github.com/Krande/conda-server/commit/4f75d07215bd068126d74310232cb708ec970c7f))

* ci(forgejo): build+push+gitops on every branch via deputy

Trigger on push to any branch (was main-only). Replace the inline sed
gitops bump with `deputy gitops-update`, run in a python container on the
Alpine docker-build runner. The manifest path and in-manifest image paths
move into secrets (GITOPS_MANIFEST, GITOPS_IMAGE_PATHS) so no deployment
detail is committed; the step self-skips when they are unset.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`d4cc6e8`](https://github.com/Krande/conda-server/commit/d4cc6e86689b559cadb2de0a5e8202d05663c63d))

* Merge pull request #5 from Krande/feat/deputy-toml-config

chore: move deputy config to deputy.toml (deputy v0.2.0) ([`487afb8`](https://github.com/Krande/conda-server/commit/487afb809ac51d7aa3882a213b0ff8e2ed123613))

* ci: bump all GitHub Action versions to current majors

- actions/checkout v4 -&gt; v7
- actions/setup-python v5 -&gt; v7
- prefix-dev/setup-pixi v0.8.8 -&gt; v0.10.1
- docker/login-action v3 -&gt; v4
- docker/metadata-action v5 -&gt; v6
- docker/setup-buildx-action v3 -&gt; v4
- docker/build-push-action v6 -&gt; v7

Versions confirmed against each action&#39;s latest release.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`9dee418`](https://github.com/Krande/conda-server/commit/9dee418a09280255868a9cc997de42376eced9fc))

* ci: move deputy config to deputy.toml, drop action_config.toml from CI flow

deputy v0.2.0 reads a deputy.toml and ships the semantic-release defaults
itself, so the workflows no longer need the DEPUTY_MARKER / DEPUTY_CONFIG env
block or action_config.toml for releases.

- Add deputy.toml: [pr_review].marker keeps the original sticky-comment thread,
  [release].version_toml points at pyproject.toml (the rest are deputy defaults).
- pr-review.yaml / tag-on-pr-merge.yaml: pin deputy @v0.2.0, drop the env block.

action_config.toml stays only for lint.yaml&#39;s [tool.python.lint]; nothing in the
deputy flow references it now. Verified: the deputy-rendered release config
resolves the same version as action_config.toml (identical branch matching +
pyproject.toml:project.version location).

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`1c3658b`](https://github.com/Krande/conda-server/commit/1c3658ba759412e91dcf6d9add635c339e3a5543))

* Merge pull request #4 from Krande/feat/use-deputy-ci

chore: use the deputy package instead of in-repo ci_tools ([`17ddaa6`](https://github.com/Krande/conda-server/commit/17ddaa649470180e79be5be9928dddcccfb73096))

* ci: use the deputy package instead of the in-repo ci_tools

The CI logic moved to its own repo (https://github.com/Krande/deputy, tagged
v0.1.0). The workflows now install deputy from the pinned tag and call the
`deputy` CLI instead of the vendored ci_tools package, which is removed.

- pr-review.yaml / tag-on-pr-merge.yaml: pip install deputy@v0.1.0, call
  `deputy pr-review` / `deputy tag-on-merge`. DEPUTY_MARKER keeps the existing
  sticky-comment thread; DEPUTY_CONFIG points at action_config.toml.
- Remove ci_tools/ and its pixi tasks (test-ci) + lint/format paths.

Behaviour is unchanged; deputy is the same code (plus a new gitops-update
command) with its own test suite living in the deputy repo.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`75dd29c`](https://github.com/Krande/conda-server/commit/75dd29c5c8fc4aba27ad58c8690b75f85a0ea690))


## v0.2.0 (2026-08-19)

### Feature

* feat(frontend): amber favicon + restructured channel page

The UI redesign switched the default brand palette to amber and the logo
to an isometric package cube, but favicon.svg was left as the old emerald
hexagon. Redraw it as the amber cube so the browser tab matches the app.

Restructure the channel page around the day-to-day need:
  - Packages overview leads the page.
  - Upload / Import stay visible (writer+ contributor actions).
  - Reindex / delete + member management move into a new collapsible
    &#34;Channel administration&#34; section, collapsed by default.
  - Install instructions anchor the bottom as scroll-to reference.

Add a small CollapsibleSection UI primitive (chevron + aria-expanded,
mirrors the &#34;Add channel&#34; disclosure). Its body is unmounted while
collapsed, so the member-list query only fires once opened.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`b518b19`](https://github.com/Krande/conda-server/commit/b518b196ff8391c25844aa1ba21510b5a8ecf83f))

### Unknown

* Merge pull request #3 from Krande/feat/channel-page-facelift

feat: amber favicon + restructured channel page ([`de669e6`](https://github.com/Krande/conda-server/commit/de669e61bac55a1b0a822e1518562dc06e7e0584))


## v0.1.0 (2026-08-18)

### Chore

* chore(frontend): add ESLint 9 flat config so the lint task runs

The lint npm script referenced ESLint 9 but no eslint.config.js was ever
committed, so `npm run lint` errored out. Add a flat config wiring the
already-installed typescript-eslint, react-hooks, and react-refresh plugins.
Lints clean across the frontend.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt; ([`1d6453c`](https://github.com/Krande/conda-server/commit/1d6453cfda8460ecdc729e5ab526c2ece1b10b1f))

### Feature

* feat(frontend): redesign pages + admin Add-channel on Channels

Apply the refreshed visual language across the pages and expose channel
creation where it&#39;s actually needed.

- InstallInstructions: render install commands as a faux terminal (accent
  prompt, dimmed flags, per-manager rows, inline copy) — the block users copy
- Channels: admin-only &#34;Add channel&#34; button reveals an inline create panel,
  so admins no longer have to detour through /admin
- CreateChannelForm: extract the create-channel form into one shared component
  used by both the Channels page and the Admin dashboard
- Home: gradient hero + search + stats row + terminal quick-install
- ChannelDetail / PackageDetail / Tokens: refined headers, pills, notices, and
  real semantic tables (scrollable, tabular-nums, mono cells)
- Card: softer radius

All accents route through the CSS-variable-backed brand-* ramp, so every page
recolors with the active palette.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt; ([`f8ffe19`](https://github.com/Krande/conda-server/commit/f8ffe1954b732310b9697555b2ae4bc0d95e6ee4))

* feat(frontend): palette-aware theming foundation + refreshed chrome

Add a runtime accent-palette system on top of the existing light/dark
theme. The brand-* Tailwind ramp is now CSS-variable-backed (--brand-*
channels per palette in index.css) and swapped via a data-palette
attribute on &lt;html&gt;, so every brand-* utility recolors app-wide with no
per-component changes. Six palettes ship (amber default, emerald, indigo,
ocean, rose, graphite); the choice persists in localStorage and is applied
pre-paint by the boot script to avoid a flash.

- theme.ts: add Palette type, PALETTES, usePalette hook, boot snippet
- SettingsMenu: gear dropdown with appearance + theme palette picker
- MobileMenu: palette swatches for narrow viewports
- Layout: sticky blurred header, display-font brand, cube BrandMark
- Fonts: Space Grotesk (display) + IBM Plex Sans/Mono

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt; ([`4f25140`](https://github.com/Krande/conda-server/commit/4f25140d623c19dc81820a62470304d3d2f936d7))

### Unknown

* Merge pull request #2 from Krande/feat/ui-redesign

feat: redesign web UI with switchable accent palettes ([`e8be6dc`](https://github.com/Krande/conda-server/commit/e8be6dce91fed51d25b96070666b8bc3381d0e72))


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
