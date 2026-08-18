from ci_tools.labels import decide_bump


def test_no_labels_defaults_to_skip():
    d = decide_bump([])
    assert d.release is False
    assert d.label == "release-skip"
    assert d.multiple is False


def test_unknown_labels_are_ignored():
    d = decide_bump(["bug", "documentation"])
    assert d.label == "release-skip"
    assert d.release is False


def test_auto_releases_with_no_forced_flag():
    d = decide_bump(["release-auto"])
    assert d.release is True
    assert d.flag == ""  # let semantic-release decide the bump


def test_patch_minor_major_force_flags():
    assert decide_bump(["release-patch"]).flag == "--patch"
    assert decide_bump(["release-minor"]).flag == "--minor"
    assert decide_bump(["release-major"]).flag == "--major"
    assert decide_bump(["release-minor"]).release is True


def test_multiple_release_labels_is_invalid_and_not_a_release():
    d = decide_bump(["release-minor", "release-major"])
    assert d.multiple is True
    assert d.release is False
    assert d.reason == "multiple release-* labels"
