"""Synthetic evaluation through the candidate benchmark's replay seam."""

from bench_wake_candidate import summarize


def test_joint_gate_rejects_missing_takes_even_with_perfect_observed_recall():
    result = summarize({"hey": [True] * 19, "hi": [True] * 20,
                        "ehi": [True] * 20, "bare": [True] * 20}, 0, 3600)
    assert result["joint_gate_pass"] is False
    assert result["per_phrasing"]["Hey Jarvis"]["takes"] == 19


def test_joint_gate_requires_negative_evidence_and_all_four_phrases():
    positives = {key: [True] * 20 for key in ("hey", "hi", "ehi", "bare")}
    assert summarize(positives, 0, 0)["joint_gate_pass"] is False
    assert summarize(positives, 0, 300)["joint_gate_pass"] is False
    positives["ehi"] = [True] * 18 + [False] * 2
    assert summarize(positives, 0, 3600)["joint_gate_pass"] is False
    positives["ehi"] = [True] * 19 + [False]
    assert summarize(positives, 1, 3600)["joint_gate_pass"] is True
    assert summarize(positives, 2, 3600)["joint_gate_pass"] is False


def test_replay_counts_saved_takes_and_negatives_without_publishing_clip_details(tmp_path):
    import numpy as np
    from bench_wake_candidate import evaluate
    from common import SR, save_16k

    class Detector:
        def hits(self, audio):
            return [1.0] if len(audio) == 3 * SR else []

    tone = np.full(SR, 0.1, dtype=np.float32)
    for key in ("hey", "hi", "ehi", "bare"):
        for index in range(20):
            save_16k(tmp_path / "wake" / f"{key}_{index:02d}.wav", tone)
    save_16k(tmp_path / "room" / "room_01.wav", tone)
    save_16k(tmp_path / "speaker-run" / "video.wav", tone)
    result = evaluate(Detector(), tmp_path)
    assert result["per_phrasing"]["Ehi Jarvis"] == {"detected": 20, "takes": 20}
    assert result["negative_files"] == 2
    assert result["negative_seconds_with_sound"] == 2.0
    assert result["false_wakes"] == 0
    assert result["joint_gate_pass"] is False
    assert "samples" not in result
