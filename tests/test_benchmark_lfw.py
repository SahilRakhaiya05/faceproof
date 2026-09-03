from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "benchmark_lfw.py"
SPEC = importlib.util.spec_from_file_location("faceproof_benchmark_lfw", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCHMARK
SPEC.loader.exec_module(BENCHMARK)

PairScore = BENCHMARK.PairScore
best_accuracy_threshold = BENCHMARK.best_accuracy_threshold
confusion_at_threshold = BENCHMARK.confusion_at_threshold
dataset_tree_fingerprint = BENCHMARK.dataset_tree_fingerprint
parse_lfw_pairs = BENCHMARK.parse_lfw_pairs
roc_auc = BENCHMARK.roc_auc
wilson_interval = BENCHMARK.wilson_interval


def test_parse_lfw_pairs_preserves_balanced_fold_protocol(tmp_path: Path) -> None:
    pairs_file = tmp_path / "pairs.txt"
    pairs_file.write_text(
        "2\t1\n"
        "Ada_Lovelace\t1\t2\n"
        "Ada_Lovelace\t1\tAlan_Turing\t1\n"
        "Grace_Hopper\t1\t2\n"
        "Grace_Hopper\t1\tAlan_Turing\t1\n",
        encoding="utf-8",
    )

    pairs = parse_lfw_pairs(pairs_file, tmp_path / "lfw_funneled")

    assert [pair.fold for pair in pairs] == [0, 0, 1, 1]
    assert [pair.same_identity for pair in pairs] == [True, False, True, False]
    assert pairs[0].left.name == "Ada_Lovelace_0001.jpg"
    assert pairs[0].right.name == "Ada_Lovelace_0002.jpg"


def test_confusion_and_best_threshold_are_deterministic() -> None:
    scores = [
        PairScore(0, True, 0.9),
        PairScore(0, True, 0.7),
        PairScore(0, False, 0.6),
        PairScore(0, False, 0.1),
    ]

    assert confusion_at_threshold(scores, 0.65).true_positive == 2
    threshold, confusion = best_accuracy_threshold(scores)

    assert threshold == pytest.approx(0.7)
    assert confusion.correct == 4
    assert confusion.false_positive == 0
    assert confusion.false_negative == 0


@pytest.mark.parametrize(
    ("positive_scores", "negative_scores", "expected"),
    [
        ([0.8, 0.9], [0.1, 0.2], 1.0),
        ([0.1, 0.2], [0.8, 0.9], 0.0),
        ([0.5, 0.5], [0.5, 0.5], 0.5),
    ],
)
def test_roc_auc_handles_ordering_and_ties(
    positive_scores: list[float], negative_scores: list[float], expected: float
) -> None:
    scores = [PairScore(0, True, value) for value in positive_scores]
    scores.extend(PairScore(0, False, value) for value in negative_scores)

    assert roc_auc(scores) == pytest.approx(expected)


def test_wilson_interval_is_bounded_and_contains_observed_rate() -> None:
    lower, upper = wilson_interval(95, 100)

    assert 0 <= lower < 0.95 < upper <= 1


def test_wilson_interval_rejects_invalid_counts() -> None:
    with pytest.raises(ValueError, match="total must be positive"):
        wilson_interval(0, 0)
    with pytest.raises(ValueError, match="between zero and total"):
        wilson_interval(11, 10)


def test_dataset_tree_fingerprint_covers_paths_and_bytes(tmp_path: Path) -> None:
    first = tmp_path / "One" / "One_0001.jpg"
    second = tmp_path / "Two" / "Two_0001.jpg"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    initial = dataset_tree_fingerprint(tmp_path)
    second.write_bytes(b"changed")
    changed = dataset_tree_fingerprint(tmp_path)

    assert initial[1:] == (2, 11)
    assert changed[1:] == (2, 12)
    assert initial[0] != changed[0]
