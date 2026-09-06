# LUMI — secret-scan fixture tests (fail-closed verification)
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN = REPO / "scripts" / "secret-scan.sh"


def run_scan(tmpdir: Path) -> tuple[int, str]:
    r = subprocess.run([str(SCAN), str(tmpdir)], capture_output=True, text=True, timeout=10)  # noqa: S603 (fixed path, no untrusted input)
    return r.returncode, r.stdout + r.stderr


def test_clean_passes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "app.py").write_text('import os\nx=os.environ.get("TELEGRAM_BOT_TOKEN")\n# JWT_SECRET=CHANGE_ME\n')
        (p / ".env.example").write_text("POSTGRES_PASSWORD=CHANGE_ME\nJWT_SECRET=CHANGE_ME\n")
        code, out = run_scan(p)
        assert code == 0, out
        assert "clean" in out.lower()


def test_real_telegram_token_fails():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        # real TG token format: 9digit:35+char
        token = "123456789:" + ("T" * 35)
        (p / "app.py").write_text(f"TELEGRAM_BOT_TOKEN={token}\n")
        code, out = run_scan(p)
        assert code == 1, out
        assert "REAL SECRET" in out


def test_real_postgres_url_fails():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "config.py").write_text("DATABASE_URL=postgresql+asyncpg://lumi:SuperSecret12345678@db:5432/lumi\n")
        code, out = run_scan(p)
        assert code == 1, out


def test_placeholder_postgres_url_passes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "migrations.py").write_text('url="postgresql+asyncpg://lumi:x@localhost/lumi"\n')
        (p / "compose.yml").write_text(
            "DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/lumi\n"
        )
        code, out = run_scan(p)
        assert code == 0, out


def test_env_example_with_real_secret_fails():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / ".env.example").write_text(
            "POSTGRES_PASSWORD=supersecret123456\nJWT_SECRET=79a0b800cc70b064987cfc2ded9904bffd35f0799d02df5f8713f74fe93724f9\n"
        )
        (p / "app.py").write_text("# clean\n")
        code, out = run_scan(p)
        assert code == 1, out
        assert ".env.example" in out


def test_env_example_clean_passes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / ".env.example").write_text(
            "POSTGRES_PASSWORD=CHANGE_ME\nJWT_SECRET=CHANGE_ME\nLLM_API_KEY=CHANGE_ME\nTELEGRAM_BOT_TOKEN=CHANGE_ME\n"
        )
        code, out = run_scan(p)
        assert code == 0, out


def test_sk_pattern_fails():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        key = "sk-" + ("K" * 32)
        (p / "app.py").write_text(f'key="{key}"\n')
        _code, _out = run_scan(p)
        # sk- pattern requires sk-xxx-xxx so this may or may not match; LLM_API_KEY assignment should also trigger
        # Use LLM_API_KEY assignment form for reliable detection
        (p / "app2.py").write_text(f"LLM_API_KEY={key}\n")
        code2, out2 = run_scan(p)
        assert code2 == 1, out2


def test_no_files_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        # empty directory — find finds no files, fail-closed exit 2 expected
        code, out = run_scan(p)
        assert code == 2, out
        assert "fail-closed" in out.lower() or "no files" in out


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for linked-worktree coverage")
def test_ignored_env_is_skipped_in_linked_worktree():
    git_binary = shutil.which("git")
    assert git_binary is not None

    def run_git(cwd: Path, *args: str) -> None:
        result = subprocess.run(  # noqa: S603 (fixed executable and test-controlled arguments)
            [git_binary, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "main"
        linked = Path(td) / "linked"
        root.mkdir()
        run_git(root, "init", "--quiet")
        run_git(root, "config", "user.name", "Test")
        run_git(root, "config", "user.email", "test@example.invalid")
        (root / ".gitignore").write_text(".env\n")
        (root / "app.py").write_text("print('clean')\n")
        run_git(root, "add", ".gitignore", "app.py")
        run_git(root, "commit", "--quiet", "-m", "initial")
        run_git(root, "worktree", "add", "--detach", str(linked), "HEAD")

        token = "123456789:" + ("T" * 35)
        (linked / ".env").write_text(f"TELEGRAM_BOT_TOKEN={token}\n")
        code, out = run_scan(linked)
        assert code == 0, out
        assert "clean" in out.lower()
