"""The Barge-in decision as a table: what the User said over Jarvis, when, and whether Jarvis resumes or stops."""

import pytest

from jarvis_hub.voice.bargein import LEXICONS, Decision, Partial, decide

ONSET = 50.0


def heard(text: str, at_ms: int, speech_ms: int, silent_ms: int) -> Partial:
    """A partial transcript `at_ms` after the onset, of `speech_ms` of speech that went quiet `silent_ms` ago."""
    return Partial(text, ONSET + at_ms / 1000, speech_ms / 1000, silent_ms / 1000)


# (what was said, partials in order, the decision)
TABLE: list[tuple[str, list[Partial], Decision]] = [
    # Backchannels, said and done: resume as soon as the speech has stopped.
    ("okay, still saying it", [heard("okay", 400, 350, 50)], "wait"),
    ("okay", [heard("okay", 400, 350, 50), heard("okay", 700, 350, 350)], "resume"),
    ("Okay.", [heard("Okay.", 700, 300, 400)], "resume"),
    ("mhm", [heard("Mhm.", 400, 250, 150)], "wait"),
    ("mhm, then quiet", [heard("Mhm.", 700, 250, 450)], "resume"),
    ("uh-huh", [heard("Uh-huh.", 700, 400, 300)], "resume"),
    ("yeah, right", [heard("Yeah, right.", 700, 500, 200)], "resume"),
    ("okay okay okay", [heard("Okay, okay, okay.", 700, 600, 250)], "resume"),
    ("sì", [heard("Sì.", 700, 250, 450)], "resume"),
    ("certo", [heard("Certo.", 700, 300, 400)], "resume"),
    ("va bene", [heard("Va bene.", 700, 450, 250)], "resume"),
    ("sì, sì, esatto", [heard("Sì, sì, esatto.", 700, 600, 200)], "resume"),
    # Noise: a cough or a door is speech to VAD and nothing to STT.
    ("cough", [heard("", 400, 250, 150)], "wait"),
    ("cough, then quiet", [heard("", 700, 250, 450)], "resume"),
    ("a breath STT spells", [heard("Hmm.", 700, 220, 480)], "resume"),
    # Hard interrupts stop at once, even mid-word and even after a Backchannel.
    ("wait", [heard("Wait", 400, 350, 0)], "stop"),
    ("wait, stop", [heard("Wait, stop.", 700, 650, 50)], "stop"),
    ("hold on", [heard("Hold on", 400, 400, 0)], "stop"),
    ("no", [heard("No.", 400, 200, 200)], "stop"),
    ("okay, but wait", [heard("Okay, but wait", 700, 650, 0)], "stop"),
    ("aspetta", [heard("Aspetta", 400, 380, 20)], "stop"),
    ("un attimo", [heard("Un attimo.", 700, 500, 200)], "stop"),
    ("fermo", [heard("Fermo!", 400, 300, 100)], "stop"),
    ("Jarvis, heard by STT before the Wake word", [heard("Jarvis", 400, 380, 20)], "stop"),
    ("okay, no", [heard("Okay, no.", 700, 450, 250)], "stop"),
    # A Backchannel phrase wins over a hard interrupt inside it; the same word anywhere else still stops.
    ("no problem", [heard("No problem.", 700, 450, 250)], "resume"),
    ("no, no problem", [heard("No, no problem.", 700, 600, 100)], "stop"),
    ("no problem, but stop", [heard("No problem, but stop", 700, 680, 0)], "stop"),
    # More Backchannels, said and done.
    ("all right", [heard("All right.", 700, 400, 300)], "resume"),
    ("alright", [heard("Alright.", 700, 350, 350)], "resume"),
    ("good", [heard("Good.", 700, 250, 450)], "resume"),
    ("perfect", [heard("Perfect.", 700, 350, 350)], "resume"),
    ("exactly", [heard("Exactly.", 700, 400, 300)], "resume"),
    ("oh okay", [heard("Oh, okay.", 700, 450, 250)], "resume"),
    ("yes sir", [heard("Yes, sir.", 700, 400, 300)], "resume"),
    ("ho capito", [heard("Ho capito.", 700, 450, 250)], "resume"),
    ("vabbè", [heard("Vabbè.", 700, 350, 350)], "resume"),
    ("va benissimo", [heard("Va benissimo.", 700, 500, 200)], "resume"),
    ("ok grazie", [heard("Ok, grazie.", 700, 450, 250)], "resume"),
    # STT's marks for sounds that are not words count as nothing said.
    ("blank audio", [heard("[BLANK_AUDIO]", 700, 250, 450)], "resume"),
    ("a cough STT names", [heard("(coughs)", 700, 300, 400)], "resume"),
    ("music", [heard("[Music]", 400, 380, 20)], "wait"),
    ("okay among marks", [heard("[BLANK_AUDIO] Okay. (coughs)", 700, 350, 350)], "resume"),
    # Taking the floor: three words or more of something else.
    ("actually, open an issue", [heard("Actually", 400, 380, 20), heard("Actually, open an", 700, 680, 20)], "stop"),
    ("anzi, apri una issue", [heard("Anzi, apri una", 700, 680, 20)], "stop"),
    ("okay but what about", [heard("Okay but what", 700, 680, 20)], "stop"),
    # A word or two that is no Backchannel: undecided while it goes on, the floor once it has stopped.
    ("the build", [heard("The build", 400, 380, 20)], "wait"),
    ("the build, then quiet", [heard("The build.", 700, 450, 250)], "stop"),
    ("quale PR", [heard("Quale PR?", 700, 450, 250)], "stop"),
    # Long speech that is still going takes the floor even before STT has the words.
    ("a long word still going", [heard("Supercali", 700, 700, 20)], "stop"),
    # VAD reports speech a chunk short, so at the 700 ms partial speech held throughout reads a little under.
    ("a long word, as VAD reports it", [heard("Supercali", 695, 670, 20)], "stop"),
]


@pytest.mark.parametrize(("said", "partials", "expected"), TABLE, ids=[row[0] for row in TABLE])
def test_decision_on_each_partial(said: str, partials: list[Partial], expected: Decision) -> None:
    assert decide(partials, final=False) == expected


# At the deadline nothing waits: still talking with words other than Backchannels is an Interruption, anything else
# resumes.
DEADLINE: list[tuple[str, list[Partial], Decision]] = [
    ("no partial came in time", [], "resume"),
    ("a stretched okay still going", [heard("Okaaay", 700, 680, 20)], "stop"),
    ("a word still going", [heard("The build", 700, 450, 20)], "stop"),
    ("noise still going", [heard("", 700, 680, 20)], "resume"),
    ("a Backchannel, done", [heard("Mhm", 700, 300, 400)], "resume"),
    ("sì, certo, said slowly", [heard("Sì, certo", 700, 600, 30)], "resume"),
    ("okay so, said slowly", [heard("Okay so", 700, 600, 30)], "stop"),
]


@pytest.mark.parametrize(("said", "partials", "expected"), DEADLINE, ids=[row[0] for row in DEADLINE])
def test_decision_at_the_deadline(said: str, partials: list[Partial], expected: Decision) -> None:
    assert decide(partials, final=True) == expected


def test_only_the_latest_partial_counts() -> None:
    partials = [heard("Wait", 400, 350, 0), heard("Okay.", 700, 350, 350)]

    assert decide(partials, final=False) == "resume"


def test_the_lexicons_come_from_their_data_files_in_both_languages() -> None:
    assert {"okay", "mhm", "sì", "certo", "va bene"} <= LEXICONS.backchannel
    assert {"wait", "stop", "hold on", "aspetta", "un attimo", "jarvis"} <= LEXICONS.interrupt
