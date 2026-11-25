"""Install a committed collector release outside the source checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_PREFIX = Path.home() / "Library/Application Support/LPS Boise Collector"
COLLECTOR = Path("scripts/boise_collect")
RUNBOOK = Path("docs/operations/boise-collector.md")
DOCUMENTS = (
    RUNBOOK,
    Path("docs/operations/boise-collector-local-setup.md"),
    Path("docs/research/bruno-collector-bridge.md"),
)
OPTIONAL_DOCUMENTS = (Path("docs/operations/boise-explorer.md"),)
MARKER = "# Installed by LPS Boise Collector"
ENTRYPOINT = """import os, sys
from pathlib import Path
release, data, node_directory = sys.argv[1:4]
arguments = sys.argv[4:]
os.environ['PATH'] = node_directory + os.pathsep + os.environ.get('PATH', os.defpath)
sys.path.insert(0, str(Path(release).resolve(strict=True)))
from scripts.boise_collect.__main__ import main
if arguments and arguments[0] in ('scan', 'resume', 'refresh', 'retry', 'acknowledge-gap', 'report'):
    if not any(value == '--data-dir' or value.startswith('--data-dir=') for value in arguments[1:]):
        arguments.extend(['--data-dir', data])
raise SystemExit(main(arguments))
"""
EXPLORER_ENTRYPOINT = """import sys
from pathlib import Path
release, data = sys.argv[1:3]
arguments = sys.argv[3:]
sys.path.insert(0, str(Path(release).resolve(strict=True)))
from scripts.boise_collect.explorer import main
if not any(value == '--data-dir' or value.startswith('--data-dir=') for value in arguments):
    arguments.extend(['--data-dir', data])
raise SystemExit(main(arguments))
"""


def _run(command: list[str], *, cwd: Path | None = None) -> bytes:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # npm/git output can include machine configuration. Keep it out of logs.
        raise ValueError(f"{Path(command[0]).name} {command[1]} failed with exit code {result.returncode}")
    return result.stdout


def _included(path: Path) -> bool:
    return not any(part in {"tests", "__pycache__", "node_modules"} for part in path.parts) and path.suffix not in {".pyc", ".pyo"}


def _snapshot(source: Path) -> tuple[str, dict[str, bytes]]:
    root = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=source).decode().strip()).resolve()
    if root != source:
        raise ValueError("collector source must be at its repository root")
    paths = [str(COLLECTOR), *(str(path) for path in (*DOCUMENTS, *OPTIONAL_DOCUMENTS)), "scripts/__init__.py"]
    changed = _run(["git", "status", "--porcelain", "--untracked-files=all", "--", *paths], cwd=source)
    if changed:
        raise ValueError("commit collector source and runbook changes before installing; unrelated checkout changes are allowed")
    revision = _run(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
    entries = _run(["git", "ls-tree", "-rz", revision, "--", *paths], cwd=source).split(b"\0")
    files = {}
    for entry in entries:
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        path = Path(os.fsdecode(name))
        if not _included(path):
            continue
        mode, kind, _ = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError("collector releases cannot contain symlinks or submodules")
        files[str(path)] = _run(["git", "show", f"{revision}:{path}"], cwd=source)
    required = {str(COLLECTOR / "__main__.py"), *(str(path) for path in DOCUMENTS), str(COLLECTOR / "bruno/package.json"), str(COLLECTOR / "bruno/package-lock.json")}
    if not required <= files.keys():
        raise ValueError("committed collector source is missing its runbook or pinned Bruno manifests")
    package = json.loads(files[str(COLLECTOR / "bruno/package.json")])
    if package.get("dependencies", {}).get("@usebruno/cli") != "4.4.0":
        raise ValueError("collector release must pin @usebruno/cli to 4.4.0")
    return revision, files


def _directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"installation directory must be a real directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _runtime() -> tuple[Path, Path, Path]:
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    executables = [shutil.which(name) for name in ("node", "npm")]
    if not all(executables):
        raise ValueError("Node.js and npm must be installed before installing the collector")
    node, npm = (Path(value).resolve() for value in executables)
    _run([str(node), "--version"])
    _run([str(npm), "--version"])
    return Path(sys.executable).resolve(), node, npm


def _validate_release(release: Path, expected: dict, node: Path) -> None:
    if release.is_symlink() or not release.is_dir():
        raise ValueError("existing release is not a real directory")
    provenance = release / "release.json"
    if provenance.is_symlink() or not provenance.is_file():
        raise ValueError("existing release has no provenance; it was left untouched")
    actual = json.loads(provenance.read_text())
    if actual.get("commit") != expected["commit"] or actual.get("files") != expected["files"]:
        raise ValueError("existing release provenance differs; it was left untouched")
    for name, digest in expected["files"].items():
        path = release / name
        if path.is_symlink() or not path.resolve().is_relative_to(release.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("existing release source differs; it was left untouched")
    # The package entrypoint is checked through npm's executable link, whose
    # target varies between Bruno releases.
    bru_link = release / COLLECTOR / "bruno/node_modules/.bin/bru"
    if not bru_link.is_file() or not bru_link.resolve().is_relative_to(release.resolve()):
        raise ValueError("release has no local Bruno executable")
    version = _run([str(node), str(bru_link.resolve()), "--version"]).decode().strip()
    if version != "4.4.0":
        raise ValueError("installed Bruno does not match the pinned version 4.4.0")


def _launcher_commands(python: Path, release: Path, data: Path, node: Path) -> dict[str, list[str]]:
    interpreter = [str(python), "-I", "-B", "-S", "-c"]
    return {
        "lps-boise-collector": [*interpreter, ENTRYPOINT, str(release), str(data), str(node.parent)],
        "lps-boise-explorer": [*interpreter, EXPLORER_ENTRYPOINT, str(release), str(data)],
    }


def _validate_launchers(python: Path, release: Path, data: Path, node: Path) -> None:
    for command in _launcher_commands(python, release, data, node).values():
        _run([*command, "--help"])


def install(prefix: Path, *, source: Path | None = None) -> Path:
    source = (source or Path(__file__).resolve().parents[2]).resolve()
    requested = prefix.expanduser().absolute()
    if requested.is_symlink():
        raise ValueError("installation prefix cannot be a symlink")
    prefix = requested.resolve()
    if prefix.is_relative_to(source) or source.is_relative_to(prefix):
        raise ValueError("installation prefix must not overlap the source checkout")
    revision, files = _snapshot(source)
    python, node, npm = _runtime()
    current = prefix / "current"
    releases = prefix / "releases"
    launcher = prefix / "bin/lps-boise-collector"
    data = prefix / "data/boise-history"
    commands = _launcher_commands(python, current, data, node)
    if current.is_symlink():
        target = current.resolve()
        if target.parent != releases or not target.is_dir():
            raise ValueError("current does not point to an existing managed release")
    elif current.exists():
        raise ValueError("current must be a managed symlink; existing content was left untouched")
    for name in commands:
        destination = prefix / "bin" / name
        if destination.is_symlink() or (destination.exists() and (not destination.is_file() or MARKER not in destination.read_text())):
            raise ValueError(f"existing launcher is not managed by this installer: {name}")
    for directory in (prefix, releases, prefix / "bin", prefix / "data", prefix / "data/boise-history", prefix / "backups"):
        _directory(directory)
    release = releases / revision
    expected = {"commit": revision, "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}
    if release.exists() or release.is_symlink():
        _validate_release(release, expected, node)
        _validate_launchers(python, release, data, node)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=releases))
        try:
            for name, body in files.items():
                destination = temporary / name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_bytes(body)
            _run([str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=temporary / COLLECTOR / "bruno")
            provenance = {**expected, "python": str(python), "node": str(node), "npm": str(npm), "bruno_version": "4.4.0"}
            (temporary / "release.json").write_text(json.dumps(provenance, indent=2) + "\n")
            _validate_release(temporary, expected, node)
            _validate_launchers(python, temporary, data, node)
            temporary.rename(release)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    temporary_launchers = []
    temporary_link = None
    try:
        for name, command in commands.items():
            contents = "#!/bin/sh\n" + MARKER + "\nexec " + " ".join(shlex.quote(part) for part in command) + ' "$@"\n'
            descriptor, temporary_name = tempfile.mkstemp(prefix=".launcher-", dir=launcher.parent)
            temporary_launcher = Path(temporary_name)
            temporary_launchers.append((temporary_launcher, launcher.parent / name))
            with os.fdopen(descriptor, "w") as handle:
                handle.write(contents)
            temporary_launcher.chmod(0o755)
        temporary_link = releases / (temporary_launchers[0][0].name + "-current")
        temporary_link.symlink_to(release)
        temporary_link.replace(current)
        for temporary_launcher, destination in temporary_launchers:
            temporary_launcher.replace(destination)
    finally:
        for temporary_launcher, _ in temporary_launchers:
            temporary_launcher.unlink(missing_ok=True)
        if temporary_link is not None:
            temporary_link.unlink(missing_ok=True)
    return launcher


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the committed Boise collector locally; makes no LPS requests")
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    args = parser.parse_args(argv)
    try:
        launcher = install(args.prefix)
    except (OSError, ValueError) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"launcher": str(launcher), "explorer_launcher": str(launcher.parent / "lps-boise-explorer"),
                      "data_directory": str(launcher.parent.parent / "data/boise-history")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
