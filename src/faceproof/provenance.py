from __future__ import annotations

import re
import shutil

# Fixed-argument Git inspection is required for source provenance.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path


class ProvenanceError(ValueError):
    """Raised when the running source cannot be tied to a clean Git commit."""


@dataclass(frozen=True, slots=True)
class GitProvenance:
    revision: str
    repository_root: Path
    source_path: Path


def verify_git_source_revision(
    expected_revision: str | None,
    *,
    cwd: Path | None = None,
    source_file: Path | None = None,
    require_clean: bool = True,
) -> GitProvenance:
    """Require an exact 40-hex HEAD and, by default, a clean working tree."""

    expected = (expected_revision or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ProvenanceError("FACEPROOF_SOURCE_REVISION must be the full 40-character Git commit")
    git = shutil.which("git")
    if git is None:
        raise ProvenanceError("Git is required to verify FACEPROOF_SOURCE_REVISION")
    # By default, bind the revision to this imported module. Merely running an
    # unrelated or ignored site-packages copy inside a clean repo must not pass.
    # ``cwd`` and ``source_file`` are explicit test hooks.
    source_path = Path(source_file or __file__).resolve()
    working_directory = Path(cwd).resolve() if cwd else source_path.parent

    repository_text = _git_output(
        git,
        working_directory,
        "rev-parse",
        "--show-toplevel",
        error="Current directory is not inside a Git repository",
    )
    repository_root = Path(repository_text).resolve()
    try:
        relative_source = source_path.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise ProvenanceError(
            "The executing FaceProof module is outside the Git repository"
        ) from exc
    _git_output(
        git,
        repository_root,
        "ls-files",
        "--error-unmatch",
        "--",
        relative_source,
        error="The executing FaceProof module is not tracked by Git",
    )
    head = _git_output(
        git,
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD",
        error="Could not resolve the repository HEAD",
    ).lower()
    if head != expected:
        raise ProvenanceError("FACEPROOF_SOURCE_REVISION does not match the checked-out Git HEAD")

    if require_clean:
        status = _git_output(
            git,
            repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error="Could not inspect the Git working tree",
            allow_empty=True,
        )
        if status:
            raise ProvenanceError("Git working tree must be clean before an anchored run")
    return GitProvenance(
        revision=head,
        repository_root=repository_root,
        source_path=source_path,
    )


def _git_output(
    git: str,
    directory: Path,
    *arguments: str,
    error: str,
    allow_empty: bool = False,
) -> str:
    try:
        # No shell is used; the resolved executable and each argument are separate.
        completed = subprocess.run(  # nosec B603
            [git, "-C", str(directory), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvenanceError(error) from exc
    output = completed.stdout.strip()
    if completed.returncode != 0 or (not allow_empty and not output):
        raise ProvenanceError(error)
    return output
