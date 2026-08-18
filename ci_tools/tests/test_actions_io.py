import pytest

from ci_tools.actions_io import parse_pull_request, read_outputs, set_output
from fakes import pr_event


def test_parse_pull_request():
    ev = pr_event(title="feat: x", labels=["release-minor", "silence-bot"], merged=True, number=42)
    pr = parse_pull_request(ev)
    assert pr.number == 42
    assert pr.title == "feat: x"
    assert pr.labels == ["release-minor", "silence-bot"]
    assert pr.merged is True
    assert pr.base_ref == "main"


def test_parse_empty_event_is_safe():
    pr = parse_pull_request({})
    assert (pr.number, pr.title, pr.labels, pr.merged) == (0, "", [], False)


def test_set_output_roundtrip_simple(tmp_path):
    p = tmp_path / "out"
    set_output("review_ok", "true", str(p))
    set_output("body_b64", "YWJjZA==", str(p))
    assert read_outputs(str(p)) == {"review_ok": "true", "body_b64": "YWJjZA=="}


def test_set_output_handles_multiline_values(tmp_path):
    p = tmp_path / "out"
    set_output("body", "line1\nline2\nline3", str(p))
    assert read_outputs(str(p))["body"] == "line1\nline2\nline3"


def test_set_output_after_a_wellformed_neighbour(tmp_path):
    # Our writer is heredoc-framed and newline-terminated, so it composes safely
    # with other outputs already in the file.
    p = tmp_path / "out"
    p.write_text("released=false\nversion=0.1.0\n", encoding="utf-8")
    set_output("body_b64", "YWJj", str(p))
    out = read_outputs(str(p))
    assert out["body_b64"] == "YWJj"
    assert out["version"] == "0.1.0"


def test_set_output_rejects_delimiter_collision(tmp_path):
    with pytest.raises(ValueError):
        set_output("x", "oops __ghout_x__ here", str(tmp_path / "out"))
