"""The bundle must be able to import itself.

This exists because of a real regression. Adding the memory layer gave
``notify.py`` two new imports and ``hooks.py`` a sixth hook that runs
``notify.py context`` -- but the installer's file list was not updated, so a
teammate's laptop would have got a SessionStart hook that raised ImportError on
every session. ``notify.py``'s top-level guard turns that into ``exit(0)``, so
it would have failed *silently*, which is the worst available outcome.

A list of filenames in two places cannot be trusted to stay right by hand, so
it is checked instead: walk the imports of everything shipped, and assert the
closure is satisfied by what is shipped.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)


def local_modules() -> set[str]:
    return {f[:-3] for f in os.listdir(PROJECT) if f.endswith(".py")}


def imports_of(filename: str) -> set[str]:
    """Top-level module names imported by one file, including lazy ones.

    Deliberately includes imports inside functions. The bug this guards against
    was exactly that: ``import context`` sits inside a branch of
    ``notify.main``, so nothing at module scope revealed the dependency.
    """
    path = os.path.join(PROJECT, filename)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=filename)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def closure(seed: set[str]) -> set[str]:
    """Every local module reachable from ``seed`` by following imports."""
    known = local_modules()
    seen: set[str] = set()
    queue = [m for m in seed]
    while queue:
        name = queue.pop()
        if name in seen or name not in known:
            continue
        seen.add(name)
        queue.extend(m for m in imports_of(name + ".py") if m in known)
    return seen


def installer_list(block: str) -> set[str]:
    """Parse one @( ... ) array of filenames out of install.ps1."""
    with open(os.path.join(PROJECT, "install", "install.ps1"),
              encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r"\$" + block + r"\s*=\s*@\((.*?)\)", text, re.S)
    if not match:
        return set()
    return {m.group(1) for m in re.finditer(r"'([^']+\.py)'", match.group(1))}


class BundleClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        import sys

        if PROJECT not in sys.path:
            sys.path.insert(0, PROJECT)
        import deploy

        self.deploy = deploy

    def test_reporter_bundle_is_import_closed(self):
        shipped = {f[:-3] for f in self.deploy.REPORTER_FILES if f.endswith(".py")}
        missing = closure(shipped) - shipped
        self.assertEqual(
            missing, set(),
            "REPORTER_FILES in deploy.py is missing modules its own files "
            "import: %s" % ", ".join(sorted(missing)))

    def test_full_bundle_is_import_closed(self):
        shipped = {
            f[:-3]
            for f in self.deploy.REPORTER_FILES + self.deploy.FULL_EXTRA_FILES
            if f.endswith(".py")
        }
        missing = closure(shipped) - shipped
        self.assertEqual(
            missing, set(),
            "the full bundle is missing: %s" % ", ".join(sorted(missing)))

    def test_notify_dependencies_are_shipped(self):
        """notify.py is what every hook runs, so its imports are the ones that
        matter most -- and two of them are inside function bodies."""
        shipped = {f[:-3] for f in self.deploy.REPORTER_FILES}
        needed = {m for m in imports_of("notify.py") if m in local_modules()}
        self.assertTrue(needed.issubset(shipped),
                        "notify.py imports %s" % ", ".join(sorted(needed - shipped)))

    def test_every_listed_file_exists(self):
        for name in self.deploy.REPORTER_FILES + self.deploy.FULL_EXTRA_FILES:
            self.assertTrue(os.path.exists(os.path.join(PROJECT, name)), name)

    def test_installer_agrees_with_deploy(self):
        """Two hand-maintained lists of the same thing is one too many; at
        least make them disagree loudly."""
        self.assertEqual(
            installer_list("reporterFiles"),
            {f for f in self.deploy.REPORTER_FILES if f.endswith(".py")})
        self.assertEqual(
            installer_list("fullFiles"),
            {f for f in self.deploy.FULL_EXTRA_FILES if f.endswith(".py")})

    def test_every_hook_action_is_handled_by_notify(self):
        """A registered hook that notify does not understand is a silent no-op."""
        import hooks

        with open(os.path.join(PROJECT, "notify.py"), encoding="utf-8") as fh:
            source = fh.read()
        for _event, action, _why, _is_async in hooks.HOOKS:
            self.assertIn('"%s"' % action, source,
                          "hooks.py registers %r but notify.py never matches it"
                          % action)


class WriteResilienceTest(unittest.TestCase):
    """``agent_alert.write`` promises never to raise. Hold it to that."""

    def setUp(self) -> None:
        from ._env import Sandbox

        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_unwritable_directory_returns_empty_not_an_exception(self):
        import agent_alert

        original = agent_alert.ALERT_DIR
        agent_alert.ALERT_DIR = original
        real_replace = os.replace

        def boom(*_args, **_kwargs):
            raise PermissionError("held open by a virus scanner")

        os.replace = boom
        try:
            self.assertEqual(
                self.sb.agent_alert.write(
                    self.sb.agent_alert.WORKING, "x", "p", key="k"),
                "")
        finally:
            os.replace = real_replace

    def test_normal_write_still_returns_its_path(self):
        path = self.sb.agent_alert.write(
            self.sb.agent_alert.WORKING, "x", "p", key="k")
        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
