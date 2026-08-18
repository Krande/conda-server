"""Thin adapter over the GitHub Actions runtime: event payload + step outputs.

The Actions "machinery" is just env vars + a JSON event file + an output file, so
everything here is trivially fakeable in tests (pass explicit paths).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class PullRequestEvent:
    number: int
    title: str
    labels: list[str]
    merged: bool
    base_ref: str


def read_event(path: str | None = None) -> dict:
    """Load the event payload JSON (GITHUB_EVENT_PATH). Empty dict if absent."""
    path = path or os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def parse_pull_request(event: dict) -> PullRequestEvent:
    pr = event.get("pull_request", {}) or {}
    base = pr.get("base", {}) or {}
    return PullRequestEvent(
        number=pr.get("number") or event.get("number") or 0,
        title=pr.get("title", "") or "",
        labels=[label["name"] for label in pr.get("labels", []) if "name" in label],
        merged=bool(pr.get("merged", False)),
        base_ref=base.get("ref", "") or "",
    )


def set_output(name: str, value: str, path: str | None = None) -> None:
    """Append a step output using the multiline-safe heredoc form.

    Always uses ``name<<DELIM`` framing and a trailing newline. This is
    deliberately robust: writing bare ``name=value`` lines is what let a prior
    ``semantic-release`` invocation (whose final line lacked a newline) swallow a
    following output and blank the PR comment. The heredoc form can't be
    corrupted by a neighbouring writer.
    """
    path = path or os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    delim = f"__ghout_{name}__"
    if delim in value:
        raise ValueError(f"output {name!r} value collides with delimiter {delim!r}")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def read_outputs(path: str) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file (heredoc and ``k=v`` forms). For tests/callers."""
    result: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            name, delim = line.split("<<", 1)
            i += 1
            buf: list[str] = []
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            result[name] = "\n".join(buf)
        elif "=" in line:
            key, val = line.split("=", 1)
            result[key] = val
        i += 1
    return result
