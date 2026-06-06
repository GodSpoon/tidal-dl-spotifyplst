"""Safe Git versioning for the music library directory.

Wraps subprocess git calls with non-fatal error handling so that git
failures never crash the main pipeline.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_GITIGNORE_CONTENT = """\
# Temporary and partial download files
*.tmp
*.part

# macOS metadata
.DS_Store
"""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand in *cwd* and return the result without raising."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def init_library_repo(library_dir: Path) -> bool:
    """Initialise a git repo in *library_dir* if one does not already exist.

    Creates a ``.gitignore`` that excludes ``*.tmp``, ``*.part``, and
    ``.DS_Store``.  Safe to call on an already-initialised directory.

    Returns:
        True if the repo was newly initialised, False if it already existed.
    """
    git_dir = library_dir / ".git"
    already_exists = git_dir.exists()

    if not already_exists:
        result = _git(["init"], cwd=library_dir)
        if result.returncode != 0:
            log.error(
                "git init failed in %s: %s", library_dir, result.stderr.strip()
            )
            return False
        log.info("Initialised git repo in %s", library_dir)

    gitignore = library_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE_CONTENT)
        log.debug("Wrote .gitignore in %s", library_dir)

    return not already_exists


def commit_library_changes(library_dir: Path, message: str) -> bool:
    """Stage all changes in *library_dir* and create a commit.

    If the working tree is clean (nothing to commit) this function succeeds
    silently.  Any git error is logged but never re-raised.

    Returns:
        True if a commit was created, False if there was nothing to commit or
        an error occurred.
    """
    # Stage everything, respecting .gitignore
    add_result = _git(["add", "--all"], cwd=library_dir)
    if add_result.returncode != 0:
        log.error(
            "git add failed in %s: %s", library_dir, add_result.stderr.strip()
        )
        return False

    # Attempt the commit; exit code 1 with "nothing to commit" is not an error
    commit_result = _git(
        ["commit", "--message", message, "--allow-empty-message"],
        cwd=library_dir,
    )
    if commit_result.returncode == 0:
        log.info("git commit in %s: %s", library_dir, message)
        return True

    stderr = commit_result.stderr.strip()
    stdout = commit_result.stdout.strip()
    combined = f"{stdout}\n{stderr}".lower()

    if "nothing to commit" in combined or "nothing added to commit" in combined:
        # Clean tree — treat as success
        log.debug("Nothing to commit in %s", library_dir)
        return False

    log.error(
        "git commit failed in %s: %s", library_dir, commit_result.stderr.strip()
    )
    return False


def has_uncommitted_changes(library_dir: Path) -> bool:
    """Return True if *library_dir* has any staged or unstaged modifications.

    Returns False when the repo does not exist or git reports an error.
    """
    result = _git(["status", "--porcelain"], cwd=library_dir)
    if result.returncode != 0:
        log.debug(
            "git status failed in %s (repo may not exist): %s",
            library_dir,
            result.stderr.strip(),
        )
        return False
    return bool(result.stdout.strip())


def ensure_library_versioned(library_dir: Path) -> None:
    """Ensure *library_dir* is a git repo; warn if uncommitted changes exist.

    Intended as a lightweight integration helper to call at startup or after
    a sync run.  Never raises.
    """
    if not library_dir.exists():
        log.debug("Library dir %s does not exist; skipping git setup", library_dir)
        return

    init_library_repo(library_dir)

    if has_uncommitted_changes(library_dir):
        log.warning(
            "Library directory %s has uncommitted changes. "
            "Run commit_library_changes() to snapshot the current state.",
            library_dir,
        )
