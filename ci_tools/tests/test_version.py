import subprocess

from ci_tools.labels import decide_bump
from ci_tools.version import (
    _isolated_env,
    next_version_noop,
    run_release,
    version_line_for,
)


def cp(stdout="", rc=0):
    return subprocess.CompletedProcess(
        ["semantic-release"], returncode=rc, stdout=stdout, stderr=""
    )


def test_next_version_parses_last_line():
    assert next_version_noop("cfg", "--minor", lambda c: cp("chatter\n0.2.0\n")) == "0.2.0"


def test_next_version_none_when_empty():
    assert next_version_noop("cfg", None, lambda c: cp("")) is None


def test_next_version_none_when_not_semver():
    assert next_version_noop("cfg", None, lambda c: cp("no release will be made")) is None


def test_next_version_none_on_runner_error():
    def boom(cmd):
        raise FileNotFoundError("semantic-release not installed")

    assert next_version_noop("cfg", None, boom) is None


def test_version_line_skip_when_no_release():
    line = version_line_for(decide_bump(["release-skip"]), "cfg", lambda c: cp())
    assert "Skipping release" in line


def test_version_line_reports_calculated_version():
    line = version_line_for(decide_bump(["release-minor"]), "cfg", lambda c: cp("0.2.0"))
    assert "0.2.0" in line and "Calculated next version" in line


def test_version_line_no_release_when_version_missing():
    line = version_line_for(decide_bump(["release-auto"]), "cfg", lambda c: cp(""))
    assert "No release will be issued" in line


def test_isolated_env_strips_the_actions_output_handshake(monkeypatch):
    # This is the fix for the empty-PR-comment bug: semantic-release must not be
    # able to write into (and corrupt) our step's GITHUB_OUTPUT.
    monkeypatch.setenv("GITHUB_OUTPUT", "/tmp/out")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = _isolated_env()
    assert "GITHUB_OUTPUT" not in env
    assert "GITHUB_ACTIONS" not in env
    assert env["KEEP_ME"] == "yes"


def test_run_release_builds_the_command_and_returns_rc():
    seen = {}

    def runner(cmd):
        seen["cmd"] = list(cmd)
        return cp(rc=0)

    rc = run_release("action_config.toml", "--minor", runner)
    assert rc == 0
    assert seen["cmd"] == [
        "semantic-release",
        "-c",
        "action_config.toml",
        "version",
        "--changelog",
        "--vcs-release",
        "--minor",
    ]


def test_run_release_omits_flag_when_none():
    seen = {}

    def runner(cmd):
        seen["cmd"] = list(cmd)
        return cp()

    run_release("cfg", None, runner)
    assert seen["cmd"][-1] == "--vcs-release"
    assert "--minor" not in seen["cmd"]
