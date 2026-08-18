from ci_tools.github import upsert_sticky_comment
from fakes import FakeGitHubClient

MARK = "<!-- MARK -->"


def test_creates_comment_when_none_exists():
    client = FakeGitHubClient()
    cid = upsert_sticky_comment(client, 1, MARK, MARK + "\nhello")
    assert len(client.comments) == 1
    assert client.comments[0].id == cid


def test_updates_in_place_when_marker_present():
    client = FakeGitHubClient()
    first = client.create_comment(1, MARK + "\nold")
    cid = upsert_sticky_comment(client, 1, MARK, MARK + "\nnew")
    assert cid == first.id
    assert len(client.comments) == 1  # not duplicated
    assert client.comments[0].body == MARK + "\nnew"


def test_ignores_unrelated_comments():
    client = FakeGitHubClient()
    client.create_comment(1, "an ordinary human comment")
    upsert_sticky_comment(client, 1, MARK, MARK + "\nx")
    assert len(client.comments) == 2
