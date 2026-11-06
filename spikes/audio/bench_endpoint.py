"""Opt-in saved-audio endpoint replay; no devices, subscription turns or transcript output."""

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

import numpy as np

from jarvis_hub.voice.input import FRAME, SAMPLE_RATE, Engines, InputPipeline, Listening
from jarvis_hub.voice.state import CAPTURING, ReplyDone, SendTurn, Tick, Timing, Voice, step

NAMO_REVISION = '59245aac1f0a0f170277ebecc32ddaea418083b4'
METHODS = ('baseline', 'acoustic-margin', 'joint', 'joint-margin', 'fused', 'fixed-1500')


def distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        'n': len(ordered),
        'p50': round(statistics.median(ordered), 1) if ordered else None,
        'p90': round(ordered[math.ceil(.9 * len(ordered)) - 1], 1) if ordered else None,
    }


def replay(pcm: np.ndarray, engines: Engines, settings: Listening) -> dict[str, Any]:
    """Advance the real pipeline/state machine in audio time; scripted replies finish after 400 ms."""
    energy = np.sqrt(((pcm[:len(pcm) // FRAME * FRAME].reshape(-1, FRAME).astype(np.float64) / 32767) ** 2).mean(axis=1))
    active = np.flatnonzero(energy > .01)
    speech_end = (int(active[-1]) + 1) / 100 if len(active) else 0.
    waveform = np.concatenate([np.zeros(4800, np.int16), pcm, np.zeros(48000, np.int16)])
    rows: list[dict[str, Any]] = []
    pipeline = InputPipeline(engines, settings, rows.append)
    timing = Timing(6., 4., 30., .5)
    voice = Voice(state='listening', deadline=100 + timing.listen_s)
    capture_setting: bool | None = None

    def sync_capture() -> None:
        nonlocal capture_setting
        enabled = voice.state in CAPTURING
        if enabled != capture_setting:
            pipeline.capture(enabled)
            capture_setting = enabled

    sync_capture()
    requests = 0
    pending: tuple[float, float] | None = None
    for start in range(0, len(waveform), FRAME):
        at = 100 + (start + FRAME) / SAMPLE_RATE
        if pending is not None and at >= pending[0]:
            voice, _ = step(voice, ReplyDone(pending[0], pending[1]), timing)
            sync_capture()
            pending = None
        if voice.deadline is not None and voice.deadline < at - 1e-9:
            voice, _ = step(voice, Tick(voice.deadline), timing)
            sync_capture()
        frame = waveform[start:start + FRAME]
        frame = np.pad(frame, (0, FRAME - len(frame)))
        for event in pipeline.push(frame, at):
            voice, actions = step(voice, event, timing)
            for action in actions:
                if isinstance(action, SendTurn):
                    requests += 1
                    pending = (at + .4, action.at)
            sync_capture()
        # Like VoiceMode's event race, input ready exactly at the deadline wins over its Tick.
        if voice.deadline is not None and voice.deadline <= at + 1e-9:
            voice, _ = step(voice, Tick(max(at, voice.deadline)), timing)
            sync_capture()
    turns = [row for row in rows if row['kind'] == 'turn']
    endpoints = [row['endpoint_at'] - 100.3 for row in turns]
    return {
        'speech_end_s': round(speech_end, 3),
        'completed_turns': len(turns), 'requests': requests,
        'premature_commit': any(at < speech_end - .05 for at in endpoints),
        'endpoints_s': [round(at, 3) for at in endpoints],
        'end_wait_ms': [row['end_wait_ms'] for row in turns],
        'commit_causes': [
            'max_turn' if row['endpoint_reason'] == 'max_turn' else
            'fixed_silence' if getattr(engines.turn, 'method', None) == 'fixed-1500' else
            'silence_fallback' if row['assessments'] and row['endpoint_at'] > row['assessments'][-1]['captured_through_at'] + 1e-9 else
            'completion_score' for row in turns
        ],
        'final_endpoint_after_speech_ms': round((endpoints[-1] - speech_end) * 1000, 1) if endpoints else None,
        'decision_scores': [row['smart_turn'] for row in turns],
        'completion_stage_ms': [row['smart_turn_ms'] for row in turns],
        'stt_ms': [row['stt_ms'] for row in turns],
        'limit_commits': sum(row['endpoint_reason'] == 'max_turn' for row in turns),
        'final_state': voice.state,
    }


class NoWake:
    chunk = 1280

    def score(self, chunk: np.ndarray) -> float:
        return 0.


class TextCompletion:
    def __init__(self, folder: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(folder / 'model_quant.onnx'), sess_options=options,
                                            providers=['CPUExecutionProvider'])
        self.tokenizer = Tokenizer.from_file(str(folder / 'tokenizer.json'))

    def score(self, text: str) -> float:
        tokens = self.tokenizer.encode(text)
        if len(tokens.ids) > 8192:
            raise ValueError('Namo context limit: refusing to truncate the opening')
        ids = np.array([tokens.ids], np.int64)
        logits = self.session.run(None, {'input_ids': ids, 'attention_mask': np.ones_like(ids)})[0][0]
        probabilities = np.exp(logits - logits.max())
        probability = float((probabilities / probabilities.sum())[1])
        if not math.isfinite(probability):
            raise ValueError('Namo returned a nonfinite probability')
        return probability


class ComparedTurn:
    def __init__(self, acoustic: Any, stt: Any, text: TextCompletion | None, method: str) -> None:
        self.acoustic, self.stt, self.text, self.method = acoustic, stt, text, method

    def complete(self, audio: np.ndarray) -> float:
        if self.method == 'fixed-1500':
            return 1.
        probability = float(self.acoustic.complete(audio))
        if self.text is None:
            return probability
        if self.method == 'fused':
            if probability >= .987:
                return 1.
            return float(self.text.score(self.stt.transcribe(audio)) >= .9)
        return min(probability, self.text.score(self.stt.transcribe(audio))) if probability >= .5 else probability


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True, help='Folder containing pauses/ and/or clips/ WAVs')
    parser.add_argument('--method', choices=METHODS, default='baseline')
    parser.add_argument('--model', type=Path, help='Existing local Namo snapshot; never downloaded by replay')
    parser.add_argument('--out', type=Path, required=True, help='Aggregate-only JSON output')
    parser.add_argument('--extension-ms', type=int, default=1500)
    args = parser.parse_args()
    if args.method in ('joint', 'joint-margin', 'fused') and args.model is None:
        parser.error('This comparison requires --model pointing to an existing Namo snapshot')
    if args.extension_ms < 0:
        parser.error('--extension-ms must be nonnegative')
    import soundfile as sf
    import soxr
    from jarvis_hub.voice.engines import EveryoneGate, NoEchoCancellation, Parakeet, SileroVad, SmartTurn

    files = sorted((args.data / 'pauses').glob('*.wav')) + sorted((args.data / 'clips').glob('*.wav'))
    if not files:
        parser.error('No WAV files found; no samples are silently excluded')
    acoustic, stt = SmartTurn(), Parakeet()
    text = TextCompletion(args.model) if args.method in ('joint', 'joint-margin', 'fused') else None
    threshold = .987 if args.method == 'acoustic-margin' else .8 if args.method == 'joint-margin' else .5
    settings = Listening(smart_turn_threshold=threshold, end_extend_s=args.extension_ms / 1000)
    if args.method == 'fixed-1500':
        settings = Listening(end_silence_s=1.5, end_extend_s=0.)
    results = []
    for path in files:
        try:
            audio, rate = sf.read(path, dtype='float32', always_2d=True)
            audio = audio.mean(axis=1)
            if rate != SAMPLE_RATE:
                audio = soxr.resample(audio, rate, SAMPLE_RATE)
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            engines = Engines(NoEchoCancellation(), NoWake(), SileroVad(),
                              ComparedTurn(acoustic, stt, text, args.method), stt, EveryoneGate())
            result = {'id': path.stem, **replay(pcm, engines, settings)}
        except Exception as error:
            result = {'id': path.stem, 'failure': type(error).__name__}
        results.append(result)
        print(path.stem, 'failed' if 'failure' in result else f"premature={result['premature_commit']} requests={result['requests']}", flush=True)
    successful = [row for row in results if 'failure' not in row]
    payload = {
        'method': args.method, 'selected_for_runtime': False,
        'namo_revision': NAMO_REVISION if text is not None else None,
        'timing': '10 ms audio-time replay; scripted 400 ms reply; excludes real Hub/device/generation latency',
        'percentiles': 'p50 median; p90 nearest rank',
        'samples': len(results), 'failures': len(results) - len(successful),
        'premature_commits': sum(row['premature_commit'] for row in successful),
        'end_wait_ms': distribution([wait for row in successful for wait in row['end_wait_ms']]),
        'settings': {'silence_s': settings.end_silence_s, 'extension_s': settings.end_extend_s,
                     'score_threshold': settings.smart_turn_threshold},
        'rows': results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + '\n')


if __name__ == '__main__':
    main()
