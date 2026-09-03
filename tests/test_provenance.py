from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from faceproof.provenance import ProvenanceError, verify_git_source_revision


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "FaceProof Test")
    _git(repository, "config", "user.email", "faceproof-test@example.invalid")
    (repository / "tracked.txt").write_text("version one\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "test revision")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_source_revision_requires_exact_clean_head(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)

    result = verify_git_source_revision(
        revision,
        cwd=repository,
        source_file=repository / "tracked.txt",
    )

    assert result.revision == revision
    assert result.repository_root == repository.resolve()


def test_source_revision_rejects_dirty_tree(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    (repository / "untracked.txt").write_text("different runtime\n", encoding="utf-8")

    with pytest.raises(ProvenanceError, match="working tree must be clean"):
        verify_git_source_revision(
            revision,
            cwd=repository,
            source_file=repository / "tracked.txt",
        )


def test_source_revision_rejects_wrong_or_short_hash(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)

    with pytest.raises(ProvenanceError, match="full 40-character"):
        verify_git_source_revision(
            revision[:7],
            cwd=repository,
            source_file=repository / "tracked.txt",
        )
    with pytest.raises(ProvenanceError, match="does not match"):
        verify_git_source_revision(
            "0" * 40,
            cwd=repository,
            source_file=repository / "tracked.txt",
        )


def test_source_revision_rejects_untracked_executing_copy(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    ignored_copy = repository / ".venv" / "site-packages" / "faceproof" / "pipeline.py"
    ignored_copy.parent.mkdir(parents=True)
    ignored_copy.write_text("# modified installed copy\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "--quiet", "-m", "ignore environment")
    revision = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(ProvenanceError, match="not tracked by Git"):
        verify_git_source_revision(
            revision,
            cwd=repository,
            source_file=ignored_copy,
        )
