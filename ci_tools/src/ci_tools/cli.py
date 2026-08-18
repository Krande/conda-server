"""Command-line entrypoints wiring the real adapters to the flows.

Usage (in a workflow, after `pip install ./ci_tools`):

    python -m ci_tools.cli pr-review
    python -m ci_tools.cli tag-on-merge

Env used:
    GITHUB_TOKEN        REST auth for comments/labels (pr-review)
    GITHUB_REPOSITORY   "owner/repo" (provided by Actions)
    GITHUB_EVENT_PATH   event payload JSON (provided by Actions)
    HAS_SOURCE_KEY      "true"/"false" — informational SOURCE_KEY presence (pr-review)
    CI_TOOLS_MARKER     override the sticky-comment marker (default: conda-server's)
    CI_TOOLS_CONFIG     override the semantic-release config file (default action_config.toml)
"""

from __future__ import annotations

import argparse
import os
import sys

from .actions_io import read_event
from .comment import MARKER
from .flows import pr_review, tag_on_merge
from .github import RestGitHubClient


def _config_file() -> str:
    return os.environ.get("CI_TOOLS_CONFIG", "action_config.toml")


def cmd_pr_review() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    client = RestGitHubClient(token, repo)
    return pr_review(
        read_event(),
        client,
        has_source_key=os.environ.get("HAS_SOURCE_KEY") == "true",
        marker=os.environ.get("CI_TOOLS_MARKER", MARKER),
        config_file=_config_file(),
    )


def cmd_tag_on_merge() -> int:
    return tag_on_merge(read_event(), config_file=_config_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pr-review", help="check a PR and post the sticky review comment")
    sub.add_parser("tag-on-merge", help="tag/release a merged PR per its release label")
    args = parser.parse_args(argv)

    if args.command == "pr-review":
        return cmd_pr_review()
    if args.command == "tag-on-merge":
        return cmd_tag_on_merge()
    return 2


if __name__ == "__main__":
    sys.exit(main())
