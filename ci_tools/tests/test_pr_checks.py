from ci_tools.pr_checks import PrChecks, title_ok


def test_valid_title_prefixes():
    assert title_ok("feat: add thing")
    assert title_ok("fix: bug")
    assert title_ok("fix!: breaking bugfix")
    assert title_ok("feat!: breaking feature")
    assert title_ok("chore: bump deps")


def test_invalid_title_prefixes():
    assert not title_ok("update stuff")
    assert not title_ok("Feat: capitalised")
    assert not title_ok("feat:no-space")
    assert not title_ok("docs: not in the allowed set")


def test_ok_requires_title_and_label_but_not_source_key():
    assert PrChecks(title_ok=True, label_ok=True, has_source_key=False).ok is True
    assert PrChecks(title_ok=False, label_ok=True, has_source_key=True).ok is False
    assert PrChecks(title_ok=True, label_ok=False, has_source_key=True).ok is False
