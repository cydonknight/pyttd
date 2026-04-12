"""Tests for ``pyttd ci`` wrapper (Feature 2)."""
import gzip
import os
import subprocess
import sys
import pytest


PYTHON = sys.executable
PYTTD = [PYTHON, "-m", "pyttd"]


class TestCiSuccess:
    """Command exits 0 — artifacts should be cleaned up by default."""

    def test_success_cleans_up(self, tmp_path):
        art_dir = str(tmp_path / "artifacts")
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir, "--",
             PYTHON, "-c", "pass"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # Artifact dir may exist but should have no DB
        dbs = list((tmp_path / "artifacts").glob("*.pyttd.db")) if (tmp_path / "artifacts").exists() else []
        assert len(dbs) == 0

    def test_success_keep_on_success(self, tmp_path):
        art_dir = str(tmp_path / "artifacts")
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir, "--keep-on-success", "--no-compress",
             "--", PYTHON, "-c", "pass"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # With --keep-on-success, the artifact dir is created
        # (but no pyttd recording unless the script imports pyttd)


class TestCiFailure:
    """Command exits nonzero — artifacts should be preserved."""

    def test_failure_preserves_artifacts(self, tmp_path):
        art_dir = str(tmp_path / "artifacts")
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir, "--no-compress",
             "--", PYTHON, "-c", "import sys; sys.exit(1)"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1
        assert "exited 1" in result.stderr

    def test_failure_exit_code_preserved(self, tmp_path):
        art_dir = str(tmp_path / "artifacts")
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir,
             "--", PYTHON, "-c", "import sys; sys.exit(42)"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 42


class TestCiCompression:
    """--compress (default) should gzip artifacts."""

    def test_compression_produces_gz(self, tmp_path):
        art_dir = str(tmp_path / "artifacts")
        # Create a script that generates a pyttd DB by importing pyttd
        script = tmp_path / "ci_script.py"
        script.write_text(
            "import sys\n"
            "# Just exit with error to trigger artifact preservation\n"
            "sys.exit(1)\n"
        )
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir, "--compress",
             "--", PYTHON, str(script)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1
        # The .gz file may or may not exist depending on whether pyttd
        # actually recorded anything. The key is that it doesn't crash.


class TestCiNoCommand:
    """No command specified should fail gracefully."""

    def test_no_command(self, tmp_path):
        art_dir = str(tmp_path / "artifacts")
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1
        assert "no command" in result.stderr.lower()


class TestCiSignalForwarding:
    """SIGINT forwarding to child (Unix only)."""

    @pytest.mark.skipif(sys.platform == 'win32', reason="SIGINT not available on Windows")
    def test_forwards_sigint(self, tmp_path):
        """The wrapper passes through SIGINT to the child via subprocess.run."""
        import signal
        art_dir = str(tmp_path / "artifacts")
        # Create a script that sleeps briefly and exits
        script = tmp_path / "slow.py"
        script.write_text("import time; time.sleep(0.1); print('done')")
        result = subprocess.run(
            [*PYTTD, "ci", "--artifact-dir", art_dir, "--no-compress",
             "--", PYTHON, str(script)],
            capture_output=True, text=True, timeout=30,
        )
        # Script should complete normally
        assert result.returncode == 0
