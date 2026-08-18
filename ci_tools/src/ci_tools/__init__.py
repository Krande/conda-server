"""conda-server CI tooling.

The logic that used to live as inline ``shell: python`` blocks inside GitHub
Actions workflows, extracted into a small, unit-testable package. The workflows
now just ``pip install ./ci_tools`` and call ``python -m ci_tools.cli <command>``.

Design: pure decision logic (:mod:`ci_tools.labels`, :mod:`ci_tools.pr_checks`,
:mod:`ci_tools.comment`) with no I/O, plus thin, injectable adapters for the
GitHub API (:mod:`ci_tools.github`), the Actions runtime (:mod:`ci_tools.actions_io`),
git, and semantic-release. :mod:`ci_tools.flows` wires them together and takes
every side-effecting dependency as an argument so the whole thing runs under
pytest with fakes — no GitHub, no live Actions runner.
"""

__version__ = "0.1.0"
