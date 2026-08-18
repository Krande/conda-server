from ci_tools.comment import MARKER, render_body, render_sticky
from ci_tools.pr_checks import PrChecks


def test_passing_body_is_friendly_and_lists_checks():
    checks = PrChecks(title_ok=True, label_ok=True, has_source_key=True)
    body = render_body(checks, ' * ✅ Calculated next version: "1.2.0"')
    assert "found no issues" in body
    assert "PR title is ok" in body
    assert "Exactly one release label" in body
    assert "1.2.0" in body


def test_failing_body_flags_problems():
    checks = PrChecks(title_ok=False, label_ok=False, has_source_key=False)
    body = render_body(checks, " * ✅ Skipping release (release-skip)")
    assert "things to address" in body
    assert "PR title must start with" in body
    assert "exactly one release-* label" in body


def test_sticky_prepends_marker():
    checks = PrChecks(title_ok=True, label_ok=True, has_source_key=True)
    sticky = render_sticky(checks, " * ✅ Skipping release (release-skip)")
    assert sticky.startswith(MARKER + "\n")
