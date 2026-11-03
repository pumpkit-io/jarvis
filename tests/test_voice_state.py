"""The voice state machine on its own: events and their times in, the next state and the actions out."""

import pytest

from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.state import (
    Chime,
    ChimeStarted,
    EndConversation,
    Event,
    Heard,
    Interrupt,
    ReplyAudio,
    ReplyDone,
    Say,
    SendTurn,
    SpeechStarted,
    Tick,
    Timing,
    Voice,
    VoiceState,
    WakeWord,
    step,
)

TIMING = Timing(follow_up_s=6.0, announce_follow_up_s=4.0, listen_s=6.0, chime_s=0.5)


def run(*events: Event, start: Voice | None = None) -> tuple[Voice, list[object]]:
    voice = start or Voice()
    actions: list[object] = []
    for event in events:
        voice, out = step(voice, event, TIMING)
        actions += out
    return voice, actions


def states(*events: Event) -> list[VoiceState]:
    voice, seen = Voice(), list[VoiceState]()
    for event in events:
        voice, _ = step(voice, event, TIMING)
        seen.append(voice.state)
    return seen


def test_starts_idle() -> None:
    assert Voice().state == "idle"


def test_the_wake_word_chimes_and_listens() -> None:
    voice, actions = run(WakeWord(10.0))
    assert voice.state == "listening"
    assert actions == [Chime()]


def test_a_full_exchange_ends_in_the_follow_up_window_then_idles_silently() -> None:
    events = [
        WakeWord(10.0),
        SpeechStarted(10.5),
        Heard("What time is it?", "en", 12.0),
        ReplyAudio(13.0, 12.0),
        ReplyDone(15.0, 12.0),
        Tick(20.9),
        Tick(21.0),
    ]
    assert states(*events) == ["listening", "listening", "thinking", "speaking", "follow_up", "follow_up", "idle"]
    _, actions = run(*events)
    assert actions == [Chime(), SendTurn("What time is it?", "en", 12.0)]


def test_speech_in_the_follow_up_window_is_a_turn_without_the_wake_word() -> None:
    voice, actions = run(
        WakeWord(10.0),
        Heard("Open a PR", "en", 12.0),
        ReplyDone(14.0, 12.0),
        SpeechStarted(19.0),
        Tick(25.0),
        Heard("And merge it later", "en", 26.0),
    )
    assert voice.state == "thinking"
    assert actions[-1] == SendTurn("And merge it later", "en", 26.0)


def test_the_follow_up_window_is_a_setting() -> None:
    timing = Timing(follow_up_s=2.0, announce_follow_up_s=4.0, listen_s=6.0, chime_s=0.5)
    voice = Voice()
    for event in [WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyDone(12.0, 11.0), Tick(14.0)]:
        voice, _ = step(voice, event, timing)
    assert voice.state == "idle"


def test_after_the_wake_word_silence_goes_idle_without_a_word() -> None:
    voice, actions = run(WakeWord(10.0), Tick(15.9))
    assert voice.state == "listening"
    voice, actions = run(Tick(16.0), start=voice)
    assert voice.state == "idle"
    assert actions == []


def test_a_turn_in_progress_holds_the_window_open() -> None:
    voice, _ = run(WakeWord(10.0), SpeechStarted(15.0), Tick(40.0))
    assert voice.state == "listening"


@pytest.mark.parametrize("state_events", [[], [WakeWord(1.0), Tick(100.0)]], ids=["at start", "after idling"])
def test_nothing_heard_while_idle_reaches_the_model(state_events: list[Event]) -> None:
    voice, actions = run(*state_events, SpeechStarted(200.0), Heard("Open a PR on pumpkit", "en", 201.0))
    assert voice.state == "idle"
    assert not any(isinstance(a, SendTurn) for a in actions)


@pytest.mark.parametrize("text", ["", "   ", ".", "...", "Uh.", "Hmm", "um, uh", "Hey Jarvis.", "Jarvis?"])
def test_empty_or_noise_only_transcripts_are_dropped_and_listening_goes_on(text: str) -> None:
    voice, actions = run(WakeWord(10.0), SpeechStarted(11.0), Heard(text, None, 12.0))
    assert voice.state == "listening"
    assert actions == [Chime()]
    # A dropped turn restarts the wait, so the User still has the whole window to speak.
    assert run(Tick(17.9), start=voice)[0].state == "listening"
    assert run(Tick(18.0), start=voice)[0].state == "idle"


@pytest.mark.parametrize(
    ("heard", "turn"),
    [
        ("Hey Jarvis, open a PR for the login bug.", "open a PR for the login bug."),
        ("Hi Jarvis open a PR", "open a PR"),
        ("Ehi Jarvis, apri una issue.", "apri una issue."),
        ("Jarvis, what's up?", "what's up?"),
        ("hey, jarvis. What's up?", "What's up?"),
        ("Okay, hey Jarvis, status please", "status please"),
        ("So I told her hey Jarvis would do it", "So I told her hey Jarvis would do it"),
        ("Tell Jarvis's tests to run", "Tell Jarvis's tests to run"),
        ("What did Jarvis say?", "What did Jarvis say?"),
        ("Harvis. Open a PR for the typo fix.", "Open a PR for the typo fix."),
        ("Jarvid, show me the open issues.", "show me the open issues."),
        ("Jorvis! Sì, procedi pure.", "Sì, procedi pure."),
        ("Travis said the build is red", "Travis said the build is red"),
        ("Hey Jarvis, Jervis needs the deploy log", "Jervis needs the deploy log"),
    ],
)
def test_the_wake_phrase_said_in_one_breath_is_cut_from_the_turn(heard: str, turn: str) -> None:
    _, actions = run(WakeWord(10.0), Heard(heard, None, 12.0))
    assert actions[-1] == SendTurn(turn, None, 12.0)


@pytest.mark.parametrize(
    ("heard", "lang"),
    [
        ("That's all.", "en"),
        ("that's all, thanks", "en"),
        ("Thanks, Jarvis!", "en"),
        ("Thank you Jarvis.", "en"),
        ("Stand by.", "en"),
        ("Go to sleep", "en"),
        ("Grazie Jarvis.", "it"),
        ("Grazie, Jarvis", "it"),
        ("Basta così.", "it"),
        ("basta cosi", "it"),
        ("Hey Jarvis, that's all.", "en"),
        ("Thanks Jarvis, that's all.", "en"),
        ("Grazie Jarvis, basta così.", "it"),
    ],
)
def test_a_stop_phrase_on_its_own_says_very_good_and_idles_without_the_model(heard: str, lang: Lang) -> None:
    voice, actions = run(WakeWord(10.0), Heard(heard, None, 12.0))
    assert voice.state == "idle"
    assert actions == [Chime(), Say("very_good", lang)]


@pytest.mark.parametrize(
    "heard", ["That's all I needed to know about the CI.", "Thanks Jarvis, now merge it", "Stand by for the deploy"]
)
def test_a_stop_phrase_inside_a_longer_utterance_goes_to_the_talker(heard: str) -> None:
    voice, actions = run(WakeWord(10.0), Heard(heard, "en", 12.0))
    assert voice.state == "thinking"
    assert actions[-1] == SendTurn(heard, "en", 12.0)


def test_a_stop_phrase_in_the_follow_up_window_idles_too() -> None:
    voice, actions = run(WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyDone(12.0, 11.0), Heard("That's all", "en", 13.0))
    assert voice.state == "idle"
    assert actions[-1] == Say("very_good", "en")


def test_end_conversation_idles_once_the_reply_has_played() -> None:
    events = [WakeWord(10.0), Heard("We're done here", "en", 11.0), ReplyAudio(12.0, 11.0), EndConversation(12.5)]
    voice, _ = run(*events)
    assert voice.state == "speaking"
    voice, actions = run(ReplyDone(14.0, 11.0), start=voice)
    assert voice.state == "idle"
    assert actions == []
    # The next exchange gets its Follow-up window again.
    voice, _ = run(WakeWord(30.0), Heard("Hi", "en", 31.0), ReplyDone(32.0, 31.0), start=voice)
    assert voice.state == "follow_up"


@pytest.mark.parametrize("state", ["thinking", "speaking"])
def test_the_wake_word_while_jarvis_replies_interrupts_it_and_listens(state: VoiceState) -> None:
    start = Voice(state=state)
    voice, actions = run(WakeWord(10.0), start=start)
    assert voice.state == "listening"
    assert actions == [Interrupt(), Chime()]


def test_the_wake_word_in_the_follow_up_window_chimes_and_listens() -> None:
    voice, actions = run(WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyDone(12.0, 11.0), WakeWord(13.0))
    assert voice.state == "listening"
    assert actions[-1] == Chime()


def test_a_wake_word_while_already_listening_restarts_the_wait_without_a_chime() -> None:
    voice, actions = run(WakeWord(10.0), WakeWord(14.0), Tick(19.0))
    assert voice.state == "listening"
    assert actions == [Chime()]


def test_the_wake_word_is_suppressed_during_the_hubs_own_chime() -> None:
    voice, actions = run(WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyAudio(11.5, 11.0), WakeWord(12.0), WakeWord(12.4))
    assert actions == [Chime(), SendTurn("Hi", "en", 11.0), Interrupt(), Chime()]
    assert voice.state == "listening"
    _, actions = run(Heard("Again", "en", 13.0), ReplyAudio(13.2, 13.0), WakeWord(13.3), start=voice)
    assert actions == [SendTurn("Again", "en", 13.0), Interrupt(), Chime()]


def test_audio_that_starts_while_the_user_has_the_floor_keeps_listening() -> None:
    voice, _ = run(WakeWord(10.0), ReplyAudio(11.0), ReplyDone(12.0))
    assert voice.state == "listening"


def test_a_reply_that_starts_while_idle_is_an_announcement_with_a_shorter_window() -> None:
    voice, _ = run(ReplyAudio(10.0))
    assert voice.state == "announce"
    voice, _ = run(ReplyDone(12.0), Tick(15.9), start=voice)
    assert voice.state == "follow_up"
    assert run(Tick(16.0), start=voice)[0].state == "idle"


def test_a_reply_that_ends_without_audio_still_opens_the_follow_up_window() -> None:
    voice, _ = run(WakeWord(10.0), Heard("Do it", "en", 11.0), ReplyDone(12.0, 11.0))
    assert voice.state == "follow_up"


def test_a_late_reply_done_after_an_interruption_changes_nothing() -> None:
    voice, _ = run(WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyAudio(11.5, 11.0), WakeWord(12.0), ReplyDone(12.1))
    assert voice.state == "listening"


def test_a_turn_heard_while_jarvis_replies_is_dropped() -> None:
    voice, actions = run(WakeWord(10.0), Heard("Hi", "en", 11.0), Heard("ok", "en", 11.5))
    assert voice.state == "thinking"
    assert actions == [Chime(), SendTurn("Hi", "en", 11.0)]


def test_the_language_of_the_turn_carries_through_unknown() -> None:
    _, actions = run(WakeWord(10.0), Heard("Apri una issue", None, 11.0))
    assert actions[-1] == SendTurn("Apri una issue", None, 11.0)


def test_end_conversation_during_an_announcement_idles_once_it_has_played() -> None:
    voice, _ = run(ReplyAudio(10.0), EndConversation(10.5), ReplyDone(12.0))
    assert voice.state == "idle"
    # The User's next exchange gets its Follow-up window.
    voice, _ = run(WakeWord(30.0), Heard("Hi", "en", 31.0), ReplyDone(32.0, 31.0), start=voice)
    assert voice.state == "follow_up"


def test_end_conversation_with_no_spoken_reply_in_progress_changes_nothing() -> None:
    voice, _ = run(WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyDone(12.0, 11.0), EndConversation(13.0))
    assert voice.state == "follow_up"
    voice, _ = run(Heard("And the CI?", "en", 14.0), ReplyDone(15.0, 14.0), start=voice)
    assert voice.state == "follow_up"


def test_a_wake_word_while_the_user_speaks_does_not_cut_the_turn_short() -> None:
    voice, actions = run(WakeWord(10.0), SpeechStarted(10.5), WakeWord(13.0), Tick(19.5))
    assert voice.state == "listening"
    voice, actions = run(Heard("Tell Ada that Jarvis will handle the deploy", "en", 20.0), start=voice)
    assert voice.state == "thinking"
    assert actions == [SendTurn("Tell Ada that Jarvis will handle the deploy", "en", 20.0)]


def test_a_noise_only_turn_in_the_follow_up_window_keeps_the_time_left() -> None:
    voice, _ = run(WakeWord(10.0), Heard("Hi", "en", 11.0), ReplyDone(12.0, 11.0), SpeechStarted(14.0), Heard("Hmm", None, 15.0))
    # 4 s of the window were left when the noise began.
    assert voice.state == "follow_up"
    assert run(Tick(18.9), start=voice)[0].state == "follow_up"
    assert run(Tick(19.0), start=voice)[0].state == "idle"


def test_the_chime_guard_counts_from_when_the_chime_starts_playing() -> None:
    voice, _ = run(WakeWord(10.0), ChimeStarted(11.0), WakeWord(11.4))
    # The hit at 11.4 s is the late chime's echo: it does not restart the wait.
    assert run(Tick(16.0), start=voice)[0].state == "idle"


def test_an_event_reply_ending_while_a_spoken_turn_waits_does_not_end_that_turn() -> None:
    # The announcement began while the User spoke and ended after their turn was heard.
    voice, actions = run(WakeWord(10.0), SpeechStarted(10.5), ReplyAudio(11.0), Heard("Is the CI green?", "en", 12.0))
    assert voice.state == "thinking"
    assert actions[-1] == SendTurn("Is the CI green?", "en", 12.0)
    voice, _ = run(ReplyDone(13.0), start=voice)
    assert voice.state == "thinking"
    voice, _ = run(ReplyAudio(14.0, 12.0), start=voice)
    assert voice.state == "speaking"
    voice, _ = run(ReplyDone(15.0, 12.0), start=voice)
    assert voice.state == "follow_up"


def test_audio_of_an_event_reply_while_a_spoken_turn_waits_is_not_its_answer() -> None:
    voice, _ = run(WakeWord(10.0), Heard("Is the CI green?", "en", 12.0), ReplyAudio(13.0))
    assert voice.state == "thinking"
