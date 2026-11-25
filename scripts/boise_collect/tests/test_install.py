from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.boise_collect.collector import ORIGIN
from scripts.boise_collect.install import DOCUMENTS, OPTIONAL_DOCUMENTS, install
from scripts.boise_collect.store import Store


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="collector install ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source checkout"
        self.source.mkdir()
        self.prefix = self.root / "Application Support/LPS Boise Collector"
        collector = self.source / "scripts/boise_collect"
        collector.mkdir(parents=True)
        original = Path(__file__).resolve().parents[1]
        for path in original.glob("*.py"):
            shutil.copyfile(path, collector / path.name)
        if (original / "web").is_dir():
            shutil.copytree(original / "web", collector / "web")
        (collector / "tests").mkdir()
        (collector / "tests/not-installed.py").write_text("# fixture\n")
        bruno = collector / "bruno"
        bruno.mkdir()
        (bruno / "package.json").write_text(json.dumps({"dependencies": {"@usebruno/cli": "4.4.0"}}))
        (bruno / "package-lock.json").write_text("{}")
        for document in DOCUMENTS:
            destination = self.source / document
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("# Local collector\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Collector installer test")
        self.git("config", "user.email", "collector-test@example.invalid")
        self.commit()
        self.bin = self.root / "tools"
        self.bin.mkdir()
        self.calls = self.root / "npm-calls"
        self.fail_npm = self.root / "npm-fails"
        self.executable("node", """import sys
print('v26.13.0' if sys.argv[1:] == ['--version'] else '4.4.0')
""")
        self.executable("npm", f"""from pathlib import Path
import json, sys
if sys.argv[1:] == ['--version']:
    print('11.20.0')
    raise SystemExit(0)
if Path({str(self.fail_npm)!r}).exists():
    raise SystemExit(1)
with Path({str(self.calls)!r}).open('a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
binary = Path('node_modules/.bin/bru')
binary.parent.mkdir(parents=True)
binary.write_text('// mock npm-installed CLI\\n')
""")
        self.environment = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def executable(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def git(self, *arguments):
        result = subprocess.run(["git", *arguments], cwd=self.source, capture_output=True, check=True)
        return result.stdout.decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "test: collector fixture")

    def database(self, path):
        path.mkdir(parents=True, exist_ok=True)
        store = Store(path, ORIGIN)
        store.queue_interval(date(2026, 1, 1), date(2026, 1, 1))
        store.close()

    def run_launcher(self, launcher, *arguments):
        return subprocess.run([str(launcher), *arguments], cwd=self.root, capture_output=True, text=True, check=True)

    def test_install_copies_committed_source_and_launcher_reports_durable_data(self):
        (self.source / "unrelated.txt").write_text("uncommitted unrelated work\n")
        launcher = install(self.prefix, source=self.source)
        release = (self.prefix / "current").resolve()
        self.assertEqual(release.name, self.git("rev-parse", "HEAD"))
        self.assertFalse((release / "scripts/boise_collect/tests").exists())
        self.assertFalse((release / "unrelated.txt").exists())
        self.assertEqual(json.loads(self.calls.read_text()), ["ci", "--ignore-scripts", "--no-audit", "--no-fund"])
        self.assertIn("scan", self.run_launcher(launcher, "--help").stdout)
        explorer = launcher.parent / "lps-boise-explorer"
        self.assertIn("--port", self.run_launcher(explorer, "--help").stdout)
        for asset in (self.source / "scripts/boise_collect/web").glob("*"):
            if asset.is_file():
                self.assertEqual((release / "scripts/boise_collect/web" / asset.name).read_bytes(), asset.read_bytes())
        data = self.prefix / "data/boise-history"
        self.database(data)
        output = json.loads(self.run_launcher(launcher, "report").stdout)
        self.assertEqual(output["report_directory"], str(data / "reports"))
        self.assertTrue((data / "reports/coverage.json").is_file())
        custom = self.root / "custom data"
        self.database(custom)
        for arguments in (("--data-dir", str(custom)), (f"--data-dir={custom}",)):
            output = json.loads(self.run_launcher(launcher, "report", *arguments).stdout)
            self.assertEqual(output["report_directory"], str(custom / "reports"))

    def test_reinstall_preserves_data_and_reuses_verified_release(self):
        launcher = install(self.prefix, source=self.source)
        data = self.prefix / "data/boise-history/keep.txt"
        data.write_text("existing dataset\n")
        first_release = (self.prefix / "current").resolve()
        self.assertEqual(install(self.prefix, source=self.source), launcher)
        self.assertEqual((self.prefix / "current").resolve(), first_release)
        self.assertEqual(data.read_text(), "existing dataset\n")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_explorer_launcher_defaults_to_durable_data_and_preserves_explicit_arguments(self):
        # A tiny CLI receiver isolates argument forwarding from the HTTP server.
        explorer_source = self.source / "scripts/boise_collect/explorer.py"
        explorer_source.write_text("import json\ndef main(argv):\n    print(json.dumps(argv))\n    return 0\n")
        self.commit()
        collector = install(self.prefix, source=self.source)
        self.assertEqual(collector, self.prefix / "bin/lps-boise-collector")
        explorer = collector.parent / "lps-boise-explorer"
        arguments = json.loads(self.run_launcher(explorer).stdout)
        self.assertEqual(arguments, ["--data-dir", str(self.prefix / "data/boise-history")])
        custom = self.root / "custom archive"
        for arguments in (("--data-dir", str(custom), "--port", "9876"), (f"--data-dir={custom}",)):
            self.assertEqual(json.loads(self.run_launcher(explorer, *arguments).stdout), list(arguments))

    def test_acknowledgment_launcher_defaults_to_durable_data(self):
        receiver = self.source / "scripts/boise_collect/__main__.py"
        receiver.write_text("import json\ndef main(argv):\n    print(json.dumps(argv))\n    return 0\n")
        self.commit()
        launcher = install(self.prefix, source=self.source)
        arguments = ("acknowledge-gap", "--job-id", "refresh", "--kind", "day", "--key", "2026-11-09",
                     "--body-sha256", "a" * 64, "--reason", "confirmed_empty_game_slot")
        self.assertEqual(json.loads(self.run_launcher(launcher, *arguments).stdout),
                         [*arguments, "--data-dir", str(self.prefix / "data/boise-history")])
        custom = self.root / "custom archive"
        for data_argument in (("--data-dir", str(custom)), (f"--data-dir={custom}",)):
            supplied = (*arguments, *data_argument)
            self.assertEqual(json.loads(self.run_launcher(launcher, *supplied).stdout), list(supplied))

    def test_dirty_collector_source_refuses_install_before_creating_prefix(self):
        with (self.source / "scripts/boise_collect/__main__.py").open("a") as handle:
            handle.write("\n# uncommitted change\n")
        with self.assertRaisesRegex(ValueError, "commit collector source"):
            install(self.prefix, source=self.source)
        self.assertFalse(self.prefix.exists())

    def test_failed_new_release_preserves_current_launcher_and_data(self):
        launcher = install(self.prefix, source=self.source)
        previous = (self.prefix / "current").resolve()
        original_launcher = launcher.read_bytes()
        explorer = launcher.parent / "lps-boise-explorer"
        original_explorer = explorer.read_bytes()
        with (self.source / "scripts/boise_collect/__main__.py").open("a") as handle:
            handle.write("\n# next committed release\n")
        self.commit()
        self.fail_npm.touch()
        with self.assertRaisesRegex(ValueError, "npm ci failed"):
            install(self.prefix, source=self.source)
        self.assertEqual((self.prefix / "current").resolve(), previous)
        self.assertEqual(launcher.read_bytes(), original_launcher)
        self.assertEqual(explorer.read_bytes(), original_explorer)
        self.assertEqual(list((self.prefix / "releases").iterdir()), [previous])

    def test_broken_explorer_help_preserves_both_launchers_and_selected_release(self):
        launcher = install(self.prefix, source=self.source)
        explorer = launcher.parent / "lps-boise-explorer"
        previous = (self.prefix / "current").resolve()
        original = {path: path.read_bytes() for path in (launcher, explorer)}
        (self.source / "scripts/boise_collect/explorer.py").write_text("def main(argv):\n    raise RuntimeError('fixture failure')\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "failed with exit code"):
            install(self.prefix, source=self.source)
        self.assertEqual((self.prefix / "current").resolve(), previous)
        self.assertEqual(list((self.prefix / "releases").iterdir()), [previous])
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)

    def test_optional_explorer_guide_is_copied_and_dirty_edits_block_install(self):
        guide = self.source / OPTIONAL_DOCUMENTS[0]
        guide.parent.mkdir(parents=True, exist_ok=True)
        guide.write_text("# Local archive explorer\n")
        self.commit()
        install(self.prefix, source=self.source)
        self.assertEqual((self.prefix / "current" / OPTIONAL_DOCUMENTS[0]).read_text(), guide.read_text())
        guide.write_text("# Uncommitted guide change\n")
        with self.assertRaisesRegex(ValueError, "commit collector source"):
            install(self.prefix, source=self.source)

    def test_unmanaged_explorer_launcher_and_symlinks_are_preserved(self):
        explorer = self.prefix / "bin/lps-boise-explorer"
        explorer.parent.mkdir(parents=True)
        explorer.write_text("unrelated executable\n")
        with self.assertRaisesRegex(ValueError, "not managed.*lps-boise-explorer"):
            install(self.prefix, source=self.source)
        self.assertEqual(explorer.read_text(), "unrelated executable\n")
        explorer.unlink()
        target = self.root / "other executable"
        target.write_text("other content\n")
        explorer.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "not managed.*lps-boise-explorer"):
            install(self.prefix, source=self.source)
        self.assertTrue(explorer.is_symlink())
        self.assertEqual(target.read_text(), "other content\n")
        self.assertFalse((self.prefix / "current").exists())

    def test_modified_existing_release_is_not_overwritten(self):
        install(self.prefix, source=self.source)
        source = self.prefix / "current/scripts/boise_collect/__main__.py"
        source.write_text("# unexpected local edit\n")
        with self.assertRaisesRegex(ValueError, "release source differs"):
            install(self.prefix, source=self.source)
        self.assertEqual(source.read_text(), "# unexpected local edit\n")

    def test_unsafe_prefixes_and_existing_paths_are_refused(self):
        for prefix in (self.source, self.source / "runtime", self.root):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "overlap"):
                install(prefix, source=self.source)
        self.prefix.mkdir(parents=True)
        (self.prefix / "current").write_text("unrelated file\n")
        with self.assertRaisesRegex(ValueError, "current must be a managed symlink"):
            install(self.prefix, source=self.source)
        self.assertEqual((self.prefix / "current").read_text(), "unrelated file\n")

    def test_symlinked_data_directory_is_refused(self):
        self.prefix.mkdir(parents=True)
        other = self.root / "other-data"
        other.mkdir()
        (self.prefix / "data").symlink_to(other)
        with self.assertRaisesRegex(ValueError, "real directory"):
            install(self.prefix, source=self.source)
        self.assertEqual(list(other.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
