import numpy as np

from jarvis_hub.voice.whisper_features import log_mel

# From transformers 5.22.1's WhisperFeatureExtractor(chunk_length=8), do_normalize=True, on the same tone padded to
# 8 s at the start: frame 750's first twelve mel bands, and mel band 5 every hundredth frame.
FRAME_750 = [-0.0266, 0.1019, 0.1981, 0.3111, 0.449, 0.6283, 0.8949, 1.6274, 1.8188, 1.8762, 1.7542, 1.0216]
BAND_5 = [-0.1238] * 7 + [1.4338]


def test_matches_whisper_feature_extraction_on_a_tone() -> None:
    tone = (np.sin(np.arange(16_000) / 7.0) * 0.3).astype(np.float32)

    features = log_mel(tone)

    assert features.shape == (1, 80, 800)
    np.testing.assert_allclose(features[0, :12, 750], FRAME_750, atol=2e-4)
    np.testing.assert_allclose(features[0, 5, ::100], BAND_5, atol=2e-4)
    assert abs(float(features.mean()) - -0.098444) < 1e-4


def test_keeps_only_the_last_eight_seconds() -> None:
    rng = np.random.default_rng(0)
    long = rng.normal(0, 0.1, 10 * 16_000).astype(np.float32)

    np.testing.assert_allclose(log_mel(long), log_mel(long[-8 * 16_000 :]), atol=1e-6)
