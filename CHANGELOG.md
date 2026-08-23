# CHANGELOG



## v0.8.2 (2026-08-23)

### Fix

* fix: capture info/about.json only for the version that is rendered (#19)

The indexer opened an archive for every version it added, and a package
page renders exactly one of them. _about_source picks the newest version
by conda ordering and reads no other, so every capture of an older
version bought metadata nothing asks for. A channel holds several
versions of each package, and that multiple was the whole waste: on a
channel of 125 artifacts spanning 20 packages, roughly five archives were
opened for every one whose contents could ever be displayed.

Capture is now decided per package rather than per row. The upsert loop
records which packages moved; once every subdir has been read — and only
then, because a package&#39;s version list is not complete until it has —
the newest version is computed from the package&#39;s whole set of rows and
its artifacts are the ones opened.

Computing it from the whole set, rather than from the rows the loop
happened to touch, is what makes &#34;newest&#34; safe to depend on. It is not a
fixed property: a rebuild of an older version can land after a newer one
already shipped, and it is the most recent upload while being the least
interesting version. The same rule covers the other direction — a version
that becomes the newest is captured on the pass that adds it, because its
package is touched and the list is recomputed.

Artifacts, plural: one version can be several artifacts across subdirs,
and all of them are opened. Opening only one would leave a package&#39;s
metadata depending on which subdir happened to sort first and on that
artifact never being removed, which is a lot of fragility to buy back a
read that now costs a few KB.

Versions that are not the newest are left uninspected and unstamped.
That is deliberate: conda_server.backfill still inspects every version,
so a channel that wants full coverage asks for it explicitly, and the
rows it fills are what keep _about_source&#39;s older-version fallback
meaningful rather than dead. The fallback also still covers the case its
docstring did not: a newest version whose recipe simply omitted
about.json, which is permanent rather than transitional.

The steady-state guarantee is unchanged — an untouched package is never
looked at, so a reindex of an unchanged channel still opens zero archives
— and so is the recapture rule for replaced bytes, which now applies to
the newest version, the one whose metadata is on screen. ([`abe7efb`](https://github.com/Krande/conda-server/commit/abe7efb5adc204a6d2bb31fd36084d670c5a6c1d))


## v0.8.1 (2026-08-23)

### Fix

* fix: read info/about.json through ranged storage reads (#18)

Capturing a package&#39;s info/about.json spooled the entire artifact to a
temporary file on local disk and then read a few hundred bytes out of it.
That is the wrong shape of cost: the metadata is tiny and fixed-size, the
archive is not, and ephemeral disk is a small hard limit that several
captures can be competing for at once. A server indexing a channel of
large artifacts can exhaust its own disk allowance reading documentation
links.

A .conda is a zip, and a zip records where every member lives in a
central directory at its tail. So the whole archive never has to move:
head for the size, one ranged read of the tail to parse the directory,
one ranged read of the info-*.tar.zst member it points at. Measured
against real conda-forge packages that is 0.5-1.3% of the bytes, two
requests, and nothing written to disk:

    nodejs 25.8.2        31,271,315 B  -&gt;    171,545 B  (0.549%)
    python 3.12.13       15,840,187 B  -&gt;    199,735 B  (1.261%)
    py-rattler 0.23.2    12,459,191 B  -&gt;    126,095 B  (1.012%)
    alembic 1.18.4          184,763 B  -&gt;     78,395 B  (42.43%)

The cost is flat rather than proportional, so it is small packages that
show a large percentage — and they were never the problem.

Storage grows one method, get_range, with HTTP range semantics. All four
backends are obstore-backed, so it is obstore.get_range_async for every
one of them, including the local filesystem backend, which serves real
ranges rather than reading the file and slicing it.

Parsing goes through zipfile rather than a hand-written End of Central
Directory reader, over a small file-like object that presents the archive
at its true length while holding only the ranges actually fetched. A read
outside them raises rather than returning short data, and the caller
widens the tail window and retries — so archive comments and Zip64 are
handled by the stdlib, and a wrong first guess costs a request rather
than the metadata.

.tar.bz2 is deliberately left alone. The legacy format is one solid bz2
stream with no index, so there is nothing for a ranged read to seek to
and the only route to info/about.json is decompressing from the start.
That path keeps spooling to a temporary file, and MAX_ABOUT_ARCHIVE_BYTES
now guards it and only it — dropped from 512 MiB to 64 MiB, because it is
the path whose cost really is the size of the package and several can be
in flight at once. Legacy archives past the cap are stamped, not retried,
exactly as before.

The cap no longer applies to .conda. It existed because reading the
member meant pulling the object out of storage first; that is no longer
true, and a ranged read of a 2 GB package costs the same as one of a 2 MB
package, so declining the large ones would buy nothing.

Unchanged: an object that cannot be fetched is still counted as failed
and left unstamped so a later pass retries it, a pass is still never
aborted by one bad archive, and archives that are simply unparseable are
still stamped as &#34;no metadata&#34; rather than retried forever. A fetch
failure is now a distinct exception type precisely so those two outcomes
cannot be confused. ([`368d404`](https://github.com/Krande/conda-server/commit/368d4043e5c66eef4dd31a81352703107d5074e9))


## v0.8.0 (2026-08-23)

### Feature

* feat: backfill info/about.json metadata for already-indexed versions (#17)

The indexer only opens an archive for a version it just added or whose
bytes just changed, which is what keeps a routine reindex from
downloading the whole channel. The cost of that bound is that versions
indexed before metadata capture existed stay blank forever: nothing
about them ever changes, so nothing ever re-reads them, and their
package pages show no documentation, homepage or repository link.

Until now the only fix was remembering to run the backfill-about CLI
command. Add the same pass as a mechanism the server offers:

* A &#34;Backfill package metadata&#34; button on the channel admin page,
  backed by a background job that reports real progress. The work is
  one object-storage download per version and can run for minutes, so
  a fire-and-forget &#34;queued, check the logs&#34; would be useless.
* An opt-in trickle in the cleanup loop that opens a few archives per
  channel per tick, so a deployment can heal without anyone pressing
  anything. Off by default: unlike every other sweep it downloads
  package archives, which costs bandwidth, and nobody should discover
  that by upgrading.

All three entry points share one runner, so they cannot drift, and the
about_fetched_at stamp means they can never duplicate each other&#39;s
work. Progress is now committed as it goes rather than once at the end,
so an interrupted pass keeps the archives it already paid to download —
previously a killed run discarded every stamp in the batch and the next
run re-read all of it.

Jobs are tracked in a new generic maintenance_jobs table rather than
reusing import_jobs, which requires an upstream URL and would put the
status endpoint under an import-shaped path. A partial index on the
un-inspected rows keeps the driving query proportional to the work
left rather than to the size of the channel.

Concurrency defaults to 2: archives are spooled to local disk before
their metadata member is read, so the limit is free disk rather than
CPU or bandwidth, and containers often run with a modest
ephemeral-storage allowance. ([`a5e020a`](https://github.com/Krande/conda-server/commit/a5e020a12583c392a0beed4f461b632721f63346))


## v0.7.0 (2026-08-22)

### Feature

* feat: read info/about.json so package pages can show links

The package detail page had no documentation link, no repository link
and no description, and could not have them: Package.description had no
writer anywhere in the codebase and was always null, and nothing in the
source referenced about.json at all.

The reason is structural. doc_url, home, dev_url, summary and description
live in a conda archive&#39;s info/about.json. This server only ever read
info/index.json -- the repodata record -- which carries none of them, and
rattler.AboutJson has no from_package_archive helper to make reading the
other member a one-liner.

New module package_about.py pulls the member out of both container
formats. A .conda is a zip whose metadata sits in a single info-*.tar.zst
entry, so the central directory lets us seek straight to a few KB and
never touch the payload entry. A .tar.bz2 is one solid stream, so it
decompresses forward and stops at the member. Parsing goes through
rattler.AboutJson, which validates the URL fields -- a recipe that wrote
a bare hostname yields nothing rather than an href the browser resolves
as a relative path. about.json is optional and routinely partial, so
every failure mode (member absent, unreadable archive, malformed JSON)
resolves to &#34;no metadata&#34; and never to an exception.

Stored on PackageVersion, not Package, because that is where the data
is: every artifact ships its own about.json and a project can move its
docs between releases. The page needs one value per package, so the API
resolves it by conda version ordering -- the newest version that carries
metadata wins, explicitly NOT the most recently uploaded row. Those two
disagree when a rebuild of an older version lands after a newer one has
shipped, which is the same trap that was just fixed in recent uploads;
ordering by upload date there had advertised the older release. Versions
carrying nothing are skipped rather than blanking the page, which is what
makes the rollout period behave.

Reindex cost is bounded deliberately. The indexer opens an archive only
for a version it just added or whose bytes genuinely changed, so a
reindex of an unchanged channel reads zero archives and the steady-state
cost is one archive read per newly indexed artifact -- not one per
artifact in the channel. Archives past a size cap are skipped but still
stamped, and the import-from-upstream path reads its metadata from the
copy already spooled to /tmp, costing no extra fetch at all.

That bound means existing rows stay blank rather than being backfilled
by a reindex, so backfill is explicit: conda-server backfill-about
&lt;channel&gt; opens the archives already in storage. It is resumable and
--limit bounds one run&#39;s egress, because every row it inspects is
stamped with about_fetched_at whether or not the archive had an
about.json -- which is also what stops metadata-less archives from being
re-downloaded on every run.

The page gains Documentation / Homepage / Repository buttons, each
rendered only when the recipe declared that URL, and leads with the
summary above the install commands (building on the lead paragraph added
when the conda-forge header link was dropped).

Co-Authored-By: Claude Opus 5 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01Mdyz12Wh4LQgzdAN1DYneo ([`6c2dc13`](https://github.com/Krande/conda-server/commit/6c2dc13e103ff4d955d95a578b8710488541ec4e))

### Unknown

* Merge pull request #16 from Krande/feat/package-about-metadata

feat: read info/about.json so package pages can show docs, homepage and repository links ([`d6f9e2d`](https://github.com/Krande/conda-server/commit/d6f9e2d3e2569d4695c302e644bd3ced061f8363))


## v0.6.1 (2026-08-22)

### Chore

* chore(helm): use a neutral storage-account example

The blobCors.accountName comment carried a real-looking Azure storage account
name as its example. This is a public repository, and an account name is more
identifying than most placeholders -- Azure account names are globally unique,
so a plausible one either names a real account or squats a namespace.

Replaced with a neutral example, matching the convention the very next line
already uses (allowedOrigin&#39;s `https://conda.example.com`). Comment only; no
value or behaviour changes, and the field is still an empty default.

Co-Authored-By: Claude Opus 5 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01Mdyz12Wh4LQgzdAN1DYneo ([`030d4c0`](https://github.com/Krande/conda-server/commit/030d4c0b330723d2d7b3ee77622d6f3aa94edc9a))

### Fix

* fix: show the newest version, not the newest row, in recent uploads

The home page &#34;Recently uploaded&#34; panel could show an OLDER version than the
channel listing and the package page, which both showed the newer one.

GET /api/search/recent ordered PackageVersion rows by created_at DESC and kept
the first row per (channel, package). That answers &#34;which artifact was uploaded
most recently&#34;, not &#34;which version is newest&#34;. The two diverge whenever an
older version is (re)published after a newer one has already shipped -- for
example when a recipe moves from per-platform builds to `noarch: python` and a
build runs on the merge commit before the version bump, so the same version
exists as both a platform artifact and a noarch one, with the noarch republish
landing last.

Neither of the assumptions the old query relied on holds in practice:

  * one artifact per version -- false for a noarch package that ships an
    __unix and a __win build of the same version, conda&#39;s standard pattern for
    platform-gated dependencies;
  * one subdir per version -- false during a per-platform to noarch migration.

The fix:

  * /search/recent picks the displayed version with sort_versions, so the panel,
    the channel listing and the package endpoint agree by construction rather
    than by coincidence.
  * Package ranking moves into SQL as a grouped max(created_at) with a
    Package.id tiebreak, replacing an over-fetch of limit * 4 rows filtered in
    Python. That also removes a real source of nondeterminism: rows sharing an
    identical created_at (a batch import, say) had no secondary ordering, so the
    winner was whatever the database happened to return.
  * The reported timestamp is the displayed version&#39;s own created_at, so a row
    describes one artifact instead of pairing one version&#39;s number with a
    different version&#39;s upload time.

This is a gap in the 0.5.x version-ordering work rather than a regression from
it: that change routed the package page and the mirror listing through the
shared version module but never touched api/search.py. The existing tests
seeded data where newest-by-date and newest-by-version coincided, so they could
not catch it.

Four new tests, two of which fail before this change and pass after. They cover
both shapes above plus a direct assertion that the panel and the package
endpoint agree. ([`0894ae2`](https://github.com/Krande/conda-server/commit/0894ae2ad911cc22ab130bd7af64c7728225fcab))

### Unknown

* Merge pull request #14 from Krande/fix/recent-uploads-latest-version

fix: show the newest version, not the newest row, in recent uploads ([`4c932f3`](https://github.com/Krande/conda-server/commit/4c932f37e9fad2e94cb6e8535741be59125fdff1))

* Merge branch &#39;main&#39; into fix/recent-uploads-latest-version ([`e95715d`](https://github.com/Krande/conda-server/commit/e95715d924df5923d805ec9be44af8dc4baf22b0))


## v0.6.0 (2026-08-22)

### Feature

* feat: drop the conda-forge link from the package header

Every package with a detail page on this server is hosted *on this
server*. Linking its header to anaconda.org/channels/conda-forge/
packages/&lt;name&gt; was only ever right for mirrored public packages; for
an internal package like structural-codecheck it points at a page for
a package that isn&#39;t on conda-forge at all. anaconda.org answers 200
for any name and renders an empty shell, so it doesn&#39;t even fail
visibly — it just looks like the package exists upstream.

The description moves out of the title block into a lead paragraph of
its own, directly above the install commands, which is where a reader
looking for &#34;what is this and how do I get it&#34; expects it.

Dependency links keep their conda-forge fallback. That case is the
opposite one: a dep this server doesn&#39;t host really does live on
conda-forge, so pointing there is genuinely useful. Comment on
condaForgeUrl now says which of the two it&#39;s for.

Note that Package.description has no writer anywhere in the codebase
today — the column exists and is always null — so this renders nothing
until package metadata is actually captured. Reading info/about.json
(which also carries doc_url/home, the natural source for a docs link)
needs the archive bytes, and neither the upload path nor the indexer
opens an archive for anything but index.json today. That&#39;s a separate
change with its own cost trade-off, deliberately not bundled here.

Co-Authored-By: Claude Opus 5 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01Mdyz12Wh4LQgzdAN1DYneo ([`587dec5`](https://github.com/Krande/conda-server/commit/587dec5299e05a330d676dbfcedbb100d7e45b78))

### Unknown

* Merge pull request #15 from Krande/feat/package-header-drop-conda-forge

feat: drop the conda-forge link from the package header ([`7db9337`](https://github.com/Krande/conda-server/commit/7db93378e4783a390abf79d11ced2f1d16f12e4d))


## v0.5.1 (2026-08-21)

### Chore

* chore: drop remnant action_config.toml (deputy uses deputy.toml)

deputy (https://github.com/Krande/deputy) reads deputy.toml for pr-review
and release config; the old action_config.toml is no longer read by
anything — the CI-pinned ruff version lives in pixi.toml
[feature.lint.dependencies], and lint.yaml just runs `pixi run lint`.
Delete the file and refresh the now-stale comments that pointed at it.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`13eaa5d`](https://github.com/Krande/conda-server/commit/13eaa5d210738c403da03fe1770d5df7793930bc))

### Fix

* fix: order package versions by conda version, add sortable columns

The package page listed versions in whatever order the rows came back
from the DB — effectively upload order — so a package that had 0.10.0
uploaded before 0.9.0 rendered a garbled sequence. Sorting the strings
would not have fixed it: &#34;0.10.0&#34; &lt; &#34;0.9.0&#34; lexicographically, which is
exactly backwards, and conda&#39;s real ordering also has to account for
epochs (1!1.0), .post/.dev suffixes, and the rule that 2.31 and 2.31.0
are the same version rather than adjacent ones.

New conda_server.versions module delegates the comparison to
rattler.Version — the reference implementation of the ordering conda
itself uses, already a dependency here for the solver and archive
reader. The module adds a parse cache, a total order for strings rattler
can&#39;t parse (they sort last instead of raising mid-request), and the
artifact-level tiebreak: version descending, then build number
descending, then subdir and build ascending so row order is stable
across requests.

The package endpoints now sort through that, which also fixes the
channel list page — it reads versions[0] as &#34;latest version&#34;. Mirror
channels had the same bug in their own filename-derived listing
(mirror_listing sorted the version strings directly); they go through
the shared sort now too.

Sorting stays server-side but in Python, not SQL: conda ordering isn&#39;t
expressible as an ORDER BY, and the endpoint already materialises every
version of the package to serialize it. Cost is bounded by the
per-package version count, not the channel size.

Frontend: version, build, subdir, size and the new &#34;Added&#34; column are
click-to-sort, clicking again reverses. Rather than reimplement conda&#39;s
ordering rules in TypeScript, the server sends a dense version_order
rank (0 = newest, shared by every build of one version) and the client
sorts on that integer, so both ends agree by construction. Ties fall
back to the server&#39;s canonical order in both directions.

The &#34;Added&#34; column reads PackageVersion.created_at, which has existed
since the initial schema — no migration needed. Mirror channels have no
row to read, so they fall back to the storage object&#39;s last-modified.

Co-Authored-By: Claude Opus 5 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01Mdyz12Wh4LQgzdAN1DYneo ([`ee159ae`](https://github.com/Krande/conda-server/commit/ee159ae902affe3f77e604668d227e08792af145))

### Unknown

* Merge pull request #13 from Krande/fix/package-version-sorting

fix: order package versions by conda version, add sortable columns ([`927bd88`](https://github.com/Krande/conda-server/commit/927bd88d8e15354b7121a32c25fcd040d7644302))

* Merge pull request #12 from Krande/chore/drop-action-config

chore: drop remnant action_config.toml (deputy uses deputy.toml) ([`12ff63d`](https://github.com/Krande/conda-server/commit/12ff63d9f273580ad830a32b5d8008532be09e72))


## v0.5.0 (2026-08-21)

### Chore

* chore: satisfy ruff format + UP017 in recent-uploads test

ruff format wants single-space inline comments, and UP017 prefers the
datetime.UTC alias over datetime.timezone.utc.

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`93a9825`](https://github.com/Krande/conda-server/commit/93a9825e03281e059711ba3db433c401042a24fa))

### Feature

* feat: replace redundant channels card with recent uploads on home

The bottom-left home card duplicated what the &#34;Browse channels&#34; button
and the channel-count stat row already convey. Replace it with a
&#34;Recently uploaded&#34; card that surfaces the newest package uploads with a
relative &#34;time since upload&#34; (minutes/hours/days, falling back to the
calendar date once older than a week).

Backend: new GET /api/search/recent endpoint returns the most recently
uploaded package versions across visible channels, ordered by server-side
created_at, deduped to one entry per (channel, package), respecting the
same ACL as search and excluding mirror channels (which have no
PackageVersion rows).

Co-Authored-By: Claude Opus 4.8 &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_013XewjLYVb35vQqLdWmamkq ([`eb7b5c7`](https://github.com/Krande/conda-server/commit/eb7b5c71d008b10a58d9347eea6f093d8abea5d1))

### Unknown

* Merge pull request #11 from Krande/feat/home-recent-uploads

feat: show recent package uploads on the home page ([`fe73d0a`](https://github.com/Krande/conda-server/commit/fe73d0a250ab91ad9e313779c2789dc3ac3ee97d))


## v0.4.0 (2026-08-20)

### Feature

* feat: actionable CORS hint when browser &#34;Show files&#34; fetch is blocked

The &#34;Show files&#34; view downloads a .conda in the browser and unpacks it
client-side. The download endpoint 302-redirects that fetch to a
cross-origin presigned storage URL (S3 / Azure Blob / GCS); without a
CORS rule allowing the site origin, the browser blocks the response and
fetch throws an opaque TypeError. Previously this surfaced as a bare
&#34;Failed to fetch&#34; with no path forward.

- condaFiles.ts: wrap the fetch in a typed CondaFilesFetchError that
  distinguishes network/CORS failures (kind: &#34;network&#34;) from HTTP
  errors, plus an isNetworkFetchError helper.
- PackageDetail.tsx: on a network-kind failure, render an amber hint
  naming the site origin, the (backend-specific) CORS remedy, and a link
  to the docs, with a retry. Falls back to a generic cloud-storage hint
  when the backend is unknown, and to a plain network error for the
  local backend (same-origin, so CORS can&#39;t be the cause).
- Backend-aware: /about now returns storage_backend (read-only,
  non-sensitive); the SPA fetches it lazily (only on error, cached and
  shared with the About page) to tailor the hint per S3/Azure/GCS.
- docs/deploying.md: add a &#34;Troubleshooting: browser file listing
  (CORS)&#34; section (the hint&#39;s link target) covering the per-backend fix,
  adding GCS alongside the existing S3/Azure guidance.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`fac6ce9`](https://github.com/Krande/conda-server/commit/fac6ce9bcea57dbd971ebfdc299da52b417fc0dc))

### Unknown

* Merge pull request #10 from Krande/feat/cors-file-listing-hint

feat: actionable CORS hint when browser &#34;Show files&#34; fetch is blocked ([`ac9675e`](https://github.com/Krande/conda-server/commit/ac9675e9660605e1ced8933caaf9a4e8a20a23eb))

* Merge branch &#39;main&#39; into feat/cors-file-listing-hint ([`2d43017`](https://github.com/Krande/conda-server/commit/2d4301717680a82d104fb56f97484ddf0cf0edc2))

* Merge pull request #9 from Krande/ci/bump-deputy-v0.3.0

chore: bump deputy pin v0.2.0 -&gt; v0.3.0 ([`d5d3327`](https://github.com/Krande/conda-server/commit/d5d3327786f2f02a352422fc88c45ca4adadfcc9))

* ci: bump deputy pin v0.2.0 -&gt; v0.3.0

v0.3.0 is a drop-in superset tagged on Krande/deputy.

Co-Authored-By: Claude Opus 4.8 (1M context) &lt;noreply@anthropic.com&gt;
Claude-Session: https://claude.ai/code/session_01J3zfaYytWJnEeZrNGo3aup ([`525744c`](https://github.com/Krande/conda-server/commit/525744c5c3e8526abdc4357b8092a52ea6477a9c))


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
