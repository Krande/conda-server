from ci_tools.comment import MARKER
from ci_tools.flows import pr_review, tag_on_merge
from fakes import FakeGitHubClient, OutputRecorder, pr_event


def run_review(event, *, has_source_key=True, version_line=" * (version line)"):
    client = FakeGitHubClient()
    out = OutputRecorder()
    rc = pr_review(
        event,
        client,
        has_source_key=has_source_key,
        version_line_fn=lambda decision: version_line,
        seed_fn=lambda title: None,
        set_output_fn=out,
    )
    return rc, client, out


def test_pr_review_happy_path_posts_sticky_and_passes():
    rc, client, out = run_review(pr_event(title="feat: x", labels=["release-minor"]))
    assert rc == 0
    assert out.values["review_ok"] == "true"
    assert len(client.comments) == 1
    assert client.comments[0].body.startswith(MARKER)


def test_pr_review_defaults_missing_label_to_skip():
    rc, client, out = run_review(pr_event(title="feat: x", labels=[]))
    assert "release-skip" in client.added_labels
    assert out.values["review_ok"] == "true"
    assert rc == 0


def test_pr_review_bad_title_fails_but_still_comments():
    rc, client, out = run_review(pr_event(title="nope, not conventional", labels=["release-skip"]))
    assert rc == 1
    assert out.values["review_ok"] == "false"
    assert "things to address" in client.comments[0].body


def test_pr_review_multiple_release_labels_fails():
    rc, _, out = run_review(pr_event(title="feat: x", labels=["release-minor", "release-major"]))
    assert rc == 1
    assert out.values["review_ok"] == "false"


def test_pr_review_silence_bot_suppresses_comment_but_still_sets_output():
    _rc, client, out = run_review(
        pr_event(title="feat: x", labels=["release-minor", "silence-bot"])
    )
    assert client.comments == []
    assert "review_ok" in out.values


def test_pr_review_updates_existing_sticky_rather_than_duplicating():
    client = FakeGitHubClient()
    client.create_comment(7, MARKER + "\nstale body")
    out = OutputRecorder()
    pr_review(
        pr_event(title="feat: x", labels=["release-minor"]),
        client,
        has_source_key=True,
        version_line_fn=lambda d: "v",
        seed_fn=lambda t: None,
        set_output_fn=out,
    )
    assert len(client.comments) == 1


def test_tag_on_merge_skips_when_not_merged():
    calls = []
    rc = tag_on_merge(
        pr_event(merged=False, labels=["release-minor"]),
        release_fn=lambda flag: (calls.append(flag), 0)[1],
    )
    assert rc == 0
    assert calls == []


def test_tag_on_merge_skips_on_release_skip():
    calls = []
    rc = tag_on_merge(
        pr_event(merged=True, labels=["release-skip"]),
        release_fn=lambda flag: (calls.append(flag), 0)[1],
    )
    assert rc == 0
    assert calls == []


def test_tag_on_merge_releases_with_the_forced_flag():
    calls = []
    rc = tag_on_merge(
        pr_event(merged=True, labels=["release-minor"]),
        release_fn=lambda flag: (calls.append(flag), 0)[1],
    )
    assert rc == 0
    assert calls == ["--minor"]
