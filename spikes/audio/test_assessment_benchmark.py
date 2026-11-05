"""Validate model response framing without changing the semantic decision."""

from bench_assessment import decode_verdict


def test_empty_reasoning_envelope_preserves_label_but_prose_and_nonempty_reasoning_are_invalid():
    assert decode_verdict("<think>\n</think>\n\ndirected") == "directed"
    assert decode_verdict(" uncertain ") == "uncertain"
    assert decode_verdict("<think>This must be directed</think>directed") == "invalid"
    assert decode_verdict("The answer is directed") == "invalid"


def test_constrained_labels_mask_the_vocabulary_axis_for_batched_logits():
    import mlx.core as mx
    from bench_assessment import restrict_labels

    logits = mx.array([[100.0, 3.0, 2.0, 4.0, 200.0]])
    masked = restrict_labels(logits, mx.array([1, 2, 3]))
    assert mx.argmax(masked, axis=-1).item() == 3
    assert mx.argmax(restrict_labels(logits[0], mx.array([1, 2, 3]))).item() == 3
