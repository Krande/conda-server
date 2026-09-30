"""Screenshot the web UI as a set of user stories, for the docs.

    pixi run ui-stories                        # every story
    pixi run ui-stories --list
    pixi run ui-stories --story channel --story package
    pixi run ui-stories --theme light          # *-light.png (the docs use dark)
    pixi run ui-stories --keep                 # leave the server up afterwards

Each story is a function below that opens one screen in the state a user
would see it and writes docs/screenshots/<name>.png.

conda-server has no dev login — signing in takes a real identity provider —
so rather than drive a dev stack, the script boots a throwaway server of its
own: SQLite and local storage in .pixi/ui-stories/, wiped on every run,
serving the SPA from frontend/dist (the pixi task builds it first). Users are
created through the same function an OIDC login calls, and the browser is
handed a session cookie signed with the throwaway server's secret, so every
screen renders exactly as it does for a signed-in user.

Everything else goes through the HTTP API the SPA and rattler-build use: the
channels, their members, API tokens, and a handful of .conda archives that
are assembled here at seed time, so no binary lives in the repo. Starting from
an empty database each time keeps the screenshots the same between runs,
bar the "added" dates.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import zstandard
from itsdangerous import TimestampSigner
from playwright.sync_api import Locator, Page, Route, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
WORK = ROOT / ".pixi" / "ui-stories"
VIEWPORT = {"width": 1440, "height": 900}
# How the README's figures look: dark, with the amber accent.
THEME = "dark"
PALETTE = "amber"
SESSION_SECRET = "ui-stories-throwaway-secret-not-for-production"
# The browser is pointed here and every request rerouted to the local server,
# so the install commands in the figures read like a real deployment's rather
# than 127.0.0.1 and a random port. (.example is reserved; it never resolves.)
PUBLIC_URL = "http://conda.example.com"

ADMIN = {"sub": "demo|ada", "email": "ada@demo.conda-server.example", "name": "ada"}
MEMBERS = [
    {"sub": "demo|grace", "email": "grace@demo.conda-server.example", "name": "grace"},
    {"sub": "demo|alan", "email": "alan@demo.conda-server.example", "name": "alan"},
]


# ── Demo content ─────────────────────────────────────────────────────────────


@dataclass
class Pkg:
    name: str
    version: str
    subdir: str = "noarch"
    build_number: int = 0
    depends: list[str] = field(default_factory=list)
    about: dict = field(default_factory=dict)
    # Bytes of payload; random, so the archive is roughly this size too.
    size: int = 40_000

    @property
    def noarch(self) -> str | None:
        if self.subdir != "noarch":
            return None
        return "python" if any(d.startswith("python") for d in self.depends) else "generic"

    @property
    def build(self) -> str:
        if self.noarch == "python":
            return f"pyhd8ed1ab_{self.build_number}"
        if self.noarch:
            return f"h4616a5c_{self.build_number}"
        return f"h2b9f3c1_{self.build_number}"

    @property
    def filename(self) -> str:
        return f"{self.name}-{self.version}-{self.build}.conda"


WIDGETS_ABOUT = {
    "summary": "Composable engineering widgets for Python.",
    "description": (
        "widgets is a small library of reusable calculation blocks — "
        "beams, plates, bolted joints — that compose into larger checks.\n\n"
        "Each widget validates its inputs and reports the clause it "
        "implements, so a calculation can be traced back to the standard."
    ),
    "home": "https://widgets.example.com",
    "doc_url": "https://widgets.example.com/docs",
    "dev_url": "https://github.com/example/widgets",
    "license": "BSD-3-Clause",
}
CORE_ABOUT = {
    "summary": "Compiled kernels behind widgets.",
    "home": "https://widgets.example.com",
    "dev_url": "https://github.com/example/widgets-core",
    "license": "BSD-3-Clause",
}

CHANNELS = [
    {
        "name": "tools",
        "description": "Libraries and command-line tools built by the platform team.",
        "private": False,
        "packages": [
            *(
                Pkg(
                    "widgets",
                    v,
                    depends=["python >=3.10", "numpy >=1.24", "widgets-core >=0.4"],
                    about=WIDGETS_ABOUT,
                    size=s,
                )
                for v, s in [("1.0.0", 61_000), ("1.1.0", 64_000), ("1.2.0", 68_000)]
            ),
            *(
                Pkg("widgets-core", "0.4.1", subdir=sd, about=CORE_ABOUT, size=s)
                for sd, s in [("linux-64", 410_000), ("osx-arm64", 380_000), ("win-64", 450_000)]
            ),
            Pkg("widgets-core", "0.4.0", subdir="linux-64", about=CORE_ABOUT, size=402_000),
            Pkg(
                "gizmo-cli",
                "2.3.0",
                about={
                    "summary": "A command-line front end for widgets.",
                    "home": "https://widgets.example.com/gizmo",
                    "license": "MIT",
                },
                size=22_000,
            ),
        ],
    },
    {
        "name": "internal",
        "description": "Packages for internal projects. Members only.",
        "private": True,
        "packages": [
            Pkg(
                "acme-report",
                "0.9.0",
                depends=["python >=3.11", "jinja2"],
                about={
                    "summary": "Renders calculation reports from widgets results.",
                    "license": "LicenseRef-Proprietary",
                },
                size=31_000,
            ),
            Pkg(
                "structural-checks",
                "3.1.0",
                depends=["python >=3.11", "widgets >=1.1"],
                about={
                    "summary": "Project-specific structural checks.",
                    "license": "LicenseRef-Proprietary",
                },
                size=45_000,
            ),
        ],
        "members": [
            ("grace@demo.conda-server.example", "writer"),
            ("alan@demo.conda-server.example", "reader"),
        ],
    },
    {
        "name": "conda-forge",
        "description": "Pull-through cache of conda-forge.",
        "private": False,
        "mirror_url": "https://conda.anaconda.org/conda-forge",
        "packages": [],
    },
]

TOKENS = [("ci-runner", 180), ("rattler-build on my laptop", 30)]

# Picked in the upload card, never uploaded.
UPLOAD_PREVIEW = [
    Pkg("acme-report", "0.10.0", depends=["python >=3.11", "jinja2"], size=33_000),
    Pkg("structural-checks", "3.2.0", depends=["python >=3.11", "widgets >=1.2"], size=47_000),
]


def _tar_zst(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for path, data in members.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 1_700_000_000
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor(level=3).compress(raw.getvalue())


def make_conda(pkg: Pkg) -> bytes:
    """A real, minimal .conda: metadata.json plus pkg- and info- tar.zst parts."""
    rng = random.Random(pkg.filename)
    if pkg.noarch == "python":
        module = pkg.name.replace("-", "_")
        payload = {
            f"site-packages/{module}/__init__.py": f'__version__ = "{pkg.version}"\n'.encode(),
            f"site-packages/{module}/_data.bin": rng.randbytes(pkg.size),
        }
    elif pkg.subdir == "win-64":
        payload = {f"Library/bin/{pkg.name}.dll": rng.randbytes(pkg.size)}
    elif pkg.subdir == "noarch":
        payload = {f"bin/{pkg.name}": rng.randbytes(pkg.size)}
    else:
        ext = "dylib" if pkg.subdir.startswith("osx") else "so"
        payload = {f"lib/lib{pkg.name.replace('-', '_')}.{ext}": rng.randbytes(pkg.size)}

    index = {
        "name": pkg.name,
        "version": pkg.version,
        "build": pkg.build,
        "build_number": pkg.build_number,
        "depends": pkg.depends,
        "license": pkg.about.get("license"),
        "subdir": pkg.subdir,
        "timestamp": 1_750_000_000_000,
    }
    if pkg.noarch:
        index["noarch"] = pkg.noarch
    paths = {
        "paths": [
            {"_path": p, "path_type": "hardlink", "size_in_bytes": len(d)}
            for p, d in payload.items()
        ],
        "paths_version": 1,
    }
    info = {
        "info/index.json": json.dumps(index, indent=2).encode(),
        "info/about.json": json.dumps(pkg.about, indent=2).encode(),
        "info/paths.json": json.dumps(paths, indent=2).encode(),
        "info/files": "\n".join(payload).encode() + b"\n",
    }
    stem = pkg.filename.removesuffix(".conda")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("metadata.json", json.dumps({"conda_pkg_format_version": 2}))
        zf.writestr(f"pkg-{stem}.tar.zst", _tar_zst(payload))
        zf.writestr(f"info-{stem}.tar.zst", _tar_zst(info))
    return buf.getvalue()


# ── The throwaway server ─────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_env(port: int) -> dict[str, str]:
    db = (WORK / "conda-server.db").as_posix()
    return {
        **os.environ,
        "CONDA_SERVER_BASE_URL": f"http://127.0.0.1:{port}",
        "CONDA_SERVER_DATABASE__URL": f"sqlite+aiosqlite:///{db}",
        "CONDA_SERVER_STORAGE__BACKEND": "local",
        "CONDA_SERVER_STORAGE__URL": (WORK / "storage").as_posix(),
        "CONDA_SERVER_AUTH__SESSION_SECRET": SESSION_SECRET,
        "CONDA_SERVER_AUTH__SESSION_HTTPS_ONLY": "false",
        "CONDA_SERVER_AUTH__INITIAL_ADMINS": json.dumps([ADMIN["email"]]),
        "CONDA_SERVER_LOGGING__FORMAT": "console",
        "CONDA_SERVER_LOGGING__LEVEL": "WARNING",
    }


def start_server() -> tuple[subprocess.Popen, str]:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        sys.exit("No frontend/dist — build it with `pixi run -e dev frontend-build`.")
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "storage").mkdir(parents=True)

    port = free_port()
    env = server_env(port)
    # The seeding below imports conda_server in this process, so it has to
    # see the same configuration as the server.
    os.environ.update(env)

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    log = (WORK / "server.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "conda_server.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"The server exited; see {WORK / 'server.log'}")
        try:
            if httpx.get(f"{base_url}/health", timeout=2).is_success:
                return proc, base_url
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.terminate()
    sys.exit(f"The server didn't answer /health within 60s; see {WORK / 'server.log'}")


def session_cookie(sub: str) -> str:
    """What Starlette's SessionMiddleware sets after a login: the session dict
    as base64 JSON, signed with the server's secret."""
    data = b64encode(json.dumps({"sub": sub}).encode())
    return TimestampSigner(SESSION_SECRET).sign(data).decode()


# ── Seeding ──────────────────────────────────────────────────────────────────


async def create_users() -> None:
    """Every demo user, through the upsert an OIDC callback runs — which is
    also what promotes the admin, from initial_admins."""
    from conda_server.api.auth import upsert_user_from_userinfo
    from conda_server.db import dispose_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            for u in (ADMIN, *MEMBERS):
                await upsert_user_from_userinfo(
                    session, {"sub": u["sub"], "email": u["email"], "preferred_username": u["name"]}
                )
            await session.commit()
    finally:
        await dispose_engine()


def seed(base_url: str) -> None:
    print("Seeding demo data…")
    asyncio.run(create_users())

    with httpx.Client(
        base_url=base_url, cookies={"session": session_cookie(ADMIN["sub"])}, timeout=60
    ) as api:

        def check(resp: httpx.Response, what: str) -> dict | list | None:
            if not resp.is_success:
                raise RuntimeError(f"{what}: HTTP {resp.status_code} {resp.text[:300]}")
            return resp.json() if resp.content else None

        for ch in CHANNELS:
            body = {k: ch[k] for k in ("name", "description", "private", "mirror_url") if k in ch}
            check(api.post("/api/channels", json=body), f"create channel {ch['name']}")
            for email, role in ch.get("members", []):
                check(
                    api.post(
                        f"/api/channels/{ch['name']}/members", json={"email": email, "role": role}
                    ),
                    f"add {email} to {ch['name']}",
                )
            # One package per request, oldest first, so "Recently uploaded"
            # lists them newest first the way a real history would.
            for pkg in ch["packages"]:
                resp = check(
                    api.post(
                        f"/api/channels/{ch['name']}/packages",
                        files={
                            "files": (pkg.filename, make_conda(pkg), "application/octet-stream")
                        },
                    ),
                    f"upload {pkg.filename}",
                )
                errors = [r for r in resp.get("results", []) if r.get("error")]
                if errors:
                    raise RuntimeError(f"upload {pkg.filename}: {errors}")

        for description, days in TOKENS:
            check(
                api.post(
                    "/api/auth/tokens", json={"description": description, "expires_in_days": days}
                ),
                f"mint token {description}",
            )


# ── Stories ──────────────────────────────────────────────────────────────────


@dataclass
class Story:
    name: str
    summary: str
    # Returns None to shoot the viewport, or a locator to shoot just that.
    run: Callable[[Page], Locator | None]


STORIES: dict[str, Story] = {}


def story(name: str, summary: str):
    def register(fn: Callable[[Page], Locator | None]):
        STORIES[name] = Story(name, summary, fn)
        return fn

    return register


def settle(page: Page) -> None:
    """Let requests finish and transitions end before the shutter."""
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)


@story("home", "The landing page: search, channel counts and recent uploads")
def _home(page: Page) -> None:
    page.goto("/")
    page.get_by_role("heading", name="Recently uploaded").wait_for()
    page.get_by_role("link", name="structural-checks").first.wait_for()
    settle(page)


@story("search", "Searching every channel you can read for a package")
def _search(page: Page) -> None:
    page.goto("/")
    page.get_by_placeholder("Search packages or channels…").fill("widget")
    page.get_by_role("heading", name="Packages (").wait_for()
    settle(page)


@story("channels", "The channels you can see, public, private and mirrored")
def _channels(page: Page) -> None:
    page.goto("/channels")
    page.get_by_role("heading", name="internal").wait_for()
    settle(page)


@story("channel", "A channel's packages, and how to install from it")
def _channel(page: Page) -> None:
    page.goto("/channels/tools")
    page.get_by_role("link", name="widgets-core").first.wait_for()
    settle(page)


@story("package", "A package: its metadata, install command and every build")
def _package(page: Page) -> Locator:
    page.goto("/channels/tools/packages/widgets")
    page.get_by_role("button", name="Expand details for 1.2.0").click()
    settle(page)
    # Taller than the viewport once a build is expanded; take all of it.
    return page.get_by_role("main")


@story("upload", "Uploading archives to a channel from the browser")
def _upload(page: Page) -> Locator:
    page.goto("/channels/internal")
    # The card: the element holding both its heading and its drop zone.
    card = (
        page.locator("div")
        .filter(has=page.get_by_role("heading", name="Upload packages", exact=True))
        .filter(has=page.get_by_text("Drop packages here"))
        .last
    )
    # Two archives picked and waiting for the Upload button.
    card.locator("input[type=file]").set_input_files(
        [
            {"name": p.filename, "mimeType": "application/octet-stream", "buffer": make_conda(p)}
            for p in UPLOAD_PREVIEW
        ]
    )
    card.get_by_text(UPLOAD_PREVIEW[-1].filename).wait_for()
    settle(page)
    return card


@story("channel-members", "Channel administration: who can read and publish")
def _members(page: Page) -> Locator:
    page.goto("/channels/internal")
    page.get_by_role("button", name="Channel administration").click()
    page.get_by_text("grace@demo.conda-server.example").first.wait_for()
    section = page.locator("section").filter(
        has=page.get_by_role("button", name="Channel administration")
    )
    settle(page)
    return section


@story("tokens", "API tokens for rattler-build, CI and scripts")
def _tokens(page: Page) -> Locator:
    page.goto("/tokens")
    page.get_by_role("cell", name="ci-runner").wait_for()
    settle(page)
    # The minted tokens are listed below the fold.
    return page.get_by_role("main")


@story("admin", "The admin page: creating a channel, plain or as a mirror")
def _admin(page: Page) -> None:
    page.goto("/admin")
    page.get_by_role("heading", name="Create channel").wait_for()
    settle(page)


@story("audit", "The audit log of who changed what")
def _audit(page: Page) -> None:
    page.goto("/admin/audit")
    # Not the action filter's <option> of the same name: a row of the log.
    page.get_by_role("main").get_by_text("member.add", exact=True).locator(
        "visible=true"
    ).first.wait_for()
    settle(page)


# ── Runner ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--story", action="append", choices=sorted(STORIES), help="run only this story; repeatable"
    )
    parser.add_argument("--list", action="store_true", help="list the stories and exit")
    parser.add_argument(
        "--theme",
        choices=["dark", "light"],
        help=f"shoot in this theme and write <story>-<theme>.png (default: {THEME}, <story>.png)",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the demo server running afterwards, signed in as the admin",
    )
    parser.add_argument("--out", type=Path, default=OUT, help="default %(default)s")
    args = parser.parse_args()

    if args.list:
        for s in STORIES.values():
            print(f"  {s.name:<18} {s.summary}")
        return 0

    selected = [STORIES[n] for n in args.story] if args.story else list(STORIES.values())
    args.out.mkdir(parents=True, exist_ok=True)
    suffix = f"-{args.theme}" if args.theme else ""
    theme = args.theme or THEME

    proc, base_url = start_server()
    failed: list[str] = []
    try:
        seed(base_url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(base_url=PUBLIC_URL, viewport=VIEWPORT, color_scheme=theme)
            # Playwright makes each request itself and hands the browser the
            # answer. Chromium drops a cookie header set on a rerouted
            # request, so this is also how the admin's session gets through.
            cookie = f"session={session_cookie(ADMIN['sub'])}"

            def reroute(route: Route) -> None:
                resp = route.fetch(
                    url=base_url + route.request.url.removeprefix(PUBLIC_URL),
                    headers={**route.request.headers, "cookie": cookie},
                    max_redirects=0,
                )
                route.fulfill(response=resp)

            ctx.route(f"{PUBLIC_URL}/**", reroute)
            # The SPA's own preferences (frontend/src/lib/theme.ts), which win
            # over the OS color scheme once set. The palette is pinned too, so
            # a change to the app's default accent doesn't recolor the docs.
            ctx.add_init_script(
                f"localStorage.setItem('conda-server:theme', '{theme}');"
                f"localStorage.setItem('conda-server:palette', '{PALETTE}');"
            )
            page = ctx.new_page()
            for s in selected:
                path = args.out / f"{s.name}{suffix}.png"
                try:
                    target = s.run(page)
                    (target or page).screenshot(path=path)
                    shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
                    print(f"  ✓ {s.name:<18} {shown}")
                except Exception as exc:  # keep going; report every broken story
                    failed.append(s.name)
                    print(f"  ✗ {s.name:<18} {exc}", file=sys.stderr)
            ctx.close()
            browser.close()

        if args.keep:
            print(f"\nDemo server at {base_url} — Ctrl-C to stop. To sign in as the admin,")
            print(f"set a cookie `session={session_cookie(ADMIN['sub'])}`.")
            proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
