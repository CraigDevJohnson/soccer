"""Exercise the registry promotion command without AWS credentials or a registry."""

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/archive-image.sh"


class ImagePromotionTests(unittest.TestCase):
    def test_production_copies_the_dev_manifest_unchanged_and_reuses_it_on_retry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = "sha256:" + "a" * 64
            registry = "111122223333.dkr.ecr.us-west-2.amazonaws.com"
            source = f"{registry}/soccer-archive-dev-worker@{digest}"
            destination = f"{registry}/soccer-archive-prod-worker:verified-{digest[7:]}"
            commands = {
                "aws": """case "$1 $2" in
  "ecr get-login-password") echo local-test ;;
  "lambda get-function") echo "$SOURCE" ;;
  "ecr batch-get-image") cat tag 2>/dev/null || echo None ;;
  "ecr describe-images") cat tag ;;
  *) exit 3 ;;
esac""",
                "skopeo": """case "$1" in
  login) cat >/dev/null ;;
  copy)
    test "$2" = "--preserve-digests"
    test "$3" = "docker://$SOURCE"
    test "$4" = "docker://$DESTINATION"
    test ! -e tag
    echo "${SOURCE##*@}" > tag
    echo copy >> operations ;;
  *) exit 3 ;;
esac""",
                # Model a Docker client which reprocesses layers during push.
                "docker": """case "$1" in
  login) cat >/dev/null ;;
  pull|tag) : ;;
  push) echo "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" > tag ;;
  *) exit 3 ;;
esac""",
            }
            for name, shell in commands.items():
                path = root / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n" + shell + "\n")
                path.chmod(0o700)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", TARGET="prod",
                       ARCHIVE_ACCOUNT="111122223333", GITHUB_STEP_SUMMARY=str(root / "summary"),
                       SOURCE=source, DESTINATION=destination)
            for _ in range(2):
                result = subprocess.run(["bash", SCRIPT], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "tag").read_text().strip(), digest)
            self.assertEqual((root / "operations").read_text().splitlines(), ["copy"])
