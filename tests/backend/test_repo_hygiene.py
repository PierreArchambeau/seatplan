"""
Repository hygiene: secrets and runtime data must never be tracked by git.

A pretix data directory holds `.secret` (the instance's Django SECRET_KEY,
which signs sessions and password-reset tokens) next to databases and
uploads. It was once committed by accident; this keeps it from happening
again.
"""
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@unittest.skipUnless(shutil.which('git') and os.path.isdir(os.path.join(REPO, '.git')), 'needs a git checkout')
class SecretsAreNotTrackedTests(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(['git', *args], cwd=REPO, capture_output=True, text=True, check=True).stdout

    def test_no_secret_or_data_dir_file_is_tracked(self):
        tracked = self.git('ls-files').splitlines()
        offenders = [
            f for f in tracked
            if f.startswith('data/') or os.path.basename(f) in ('.secret', 'pretix.cfg', '.env')
            or f.endswith(('.sqlite3', '.sqlite', '.pem', '.key'))
        ]
        self.assertEqual(offenders, [], 'secrets / runtime data must not be committed')

    def test_data_directory_is_git_ignored(self):
        for path in ('data/.secret', 'data/db.sqlite3', 'data/media/x.png'):
            out = subprocess.run(['git', 'check-ignore', '-q', path], cwd=REPO)
            self.assertEqual(out.returncode, 0, '%s should be ignored by .gitignore' % path)
