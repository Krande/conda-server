"""GitHub REST adapter + the sticky-comment upsert.

``GitHubClient`` is a Protocol so tests can drop in an in-memory fake; the upsert
logic is written against the protocol and is fully unit-testable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Comment:
    id: int
    body: str


class GitHubClient(Protocol):
    def list_comments(self, issue: int) -> list[Comment]: ...
    def create_comment(self, issue: int, body: str) -> Comment: ...
    def update_comment(self, comment_id: int, body: str) -> None: ...
    def ensure_label(self, name: str, color: str) -> None: ...
    def add_labels(self, issue: int, labels: list[str]) -> None: ...


class RestGitHubClient:
    """Minimal REST client over urllib (no third-party deps)."""

    def __init__(self, token: str, repo: str, api: str = "https://api.github.com") -> None:
        self._token = token
        self._repo = repo
        self._api = api.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self._api}/repos/{self._repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def list_comments(self, issue: int) -> list[Comment]:
        comments: list[Comment] = []
        page = 1
        while True:
            batch = self._request("GET", f"/issues/{issue}/comments?per_page=100&page={page}")
            if not batch:
                break
            comments.extend(Comment(c["id"], c.get("body") or "") for c in batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def create_comment(self, issue: int, body: str) -> Comment:
        res = self._request("POST", f"/issues/{issue}/comments", {"body": body})
        return Comment(res["id"], res.get("body") or body)

    def update_comment(self, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/issues/comments/{comment_id}", {"body": body})

    def ensure_label(self, name: str, color: str) -> None:
        try:
            self._request("POST", "/labels", {"name": name, "color": color})
        except urllib.error.HTTPError as exc:
            if exc.code == 422:  # already exists — refresh its colour
                self._request("PATCH", f"/labels/{name}", {"color": color})
            else:
                raise

    def add_labels(self, issue: int, labels: list[str]) -> None:
        self._request("POST", f"/issues/{issue}/labels", {"labels": labels})


def upsert_sticky_comment(client: GitHubClient, issue: int, marker: str, body: str) -> int:
    """Update the single comment containing ``marker``, or create it. Returns id."""
    for comment in client.list_comments(issue):
        if marker in comment.body:
            client.update_comment(comment.id, body)
            return comment.id
    return client.create_comment(issue, body).id
