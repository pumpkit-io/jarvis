import pytest

from jarvis_hub.voice.splitter import SentenceSplitter

BLOCK = object()


def split(*pieces: object) -> list[str]:
    """Deltas as they stream, BLOCK where a text block ends, then the end of the reply."""
    splitter = SentenceSplitter()
    out: list[str] = []
    for piece in pieces:
        out += splitter.end_block() if piece is BLOCK else splitter.feed(str(piece))
    return out + splitter.flush()


@pytest.mark.parametrize(
    ("pieces", "chunks"),
    [
        # English
        (["Good evening, sir. All quiet."], ["Good evening, sir.", "All quiet."]),
        (["Good ", "evening", ", sir", ". All", " quiet", "."], ["Good evening, sir.", "All quiet."]),
        (["Really? ", "Yes! ", "Quite so."], ["Really?", "Yes!", "Quite so."]),
        (["Well... perhaps."], ["Well...", "perhaps."]),
        (['He said "done." Then left.'], ['He said "done."', "Then left."]),
        (["It costs 3.5 dollars. Fine."], ["It costs 3.5 dollars.", "Fine."]),
        (["It costs 3.", "5 dollars. Fine."], ["It costs 3.5 dollars.", "Fine."]),
        (["Version 1.2.3 is out. Good."], ["Version 1.2.3 is out.", "Good."]),
        (["Ask Mr. Stark and Dr. Banner. Now."], ["Ask Mr. Stark and Dr. Banner.", "Now."]),
        (["Small things, e.g. typos, i.e. trivia. Done."], ["Small things, e.g. typos, i.e. trivia.", "Done."]),
        (["Tea vs. coffee, etc. are fine. Next."], ["Tea vs. coffee, etc. are fine.", "Next."]),
        (["J. R. R. Tolkien wrote it. Yes."], ["J. R. R. Tolkien wrote it.", "Yes."]),
        (["It is on pumpkit.io/docs?page=2. Have a look."], ["It is on pumpkit.io/docs?page=2.", "Have a look."]),
        (["See https://github.com/pumpkit-io/jarvis. Done."], ["See https://github.com/pumpkit-io/jarvis.", "Done."]),
        (["Run `uv sync. Then test` now. Next."], ["Run `uv sync. Then test` now.", "Next."]),
        (["Carry on. Fine art. Next."], ["Carry on.", "Fine art.", "Next."]),
        (["1. Open the PR. 2. Merge it."], ["1. Open the PR.", "2. Merge it."]),
        (["The meeting is at 10:30. Fine."], ["The meeting is at 10:30.", "Fine."]),
        (["Right away, sir.", BLOCK, "The task is queued."], ["Right away, sir.", "The task is queued."]),
        (["Right away, sir", BLOCK, BLOCK, "Done"], ["Right away, sir", "Done"]),
        (["   ", BLOCK, "Hi. "], ["Hi."]),
        (["No trailing stop"], ["No trailing stop"]),
        # The first clause goes early once it has six words; later ones wait for the sentence.
        (
            ["I checked the six open pull requests, and two of them fail, sadly. Shall I look?"],
            ["I checked the six open pull requests,", "and two of them fail, sadly.", "Shall I look?"],
        ),
        (["Sir, the build is green. Yes, all of it."], ["Sir, the build is green.", "Yes, all of it."]),
        (
            ["There is one thing worth noting here: the deploy failed."],
            ["There is one thing worth noting here:", "the deploy failed."],
        ),
        (["Done. I checked the six open pull requests, and all pass."], ["Done.", "I checked the six open pull requests, and all pass."]),
        (["That is 1,000,000 dollars in round numbers, sir."], ["That is 1,000,000 dollars in round numbers,", "sir."]),
        # Italian
        (["Buonasera, signore. Tutto tranquillo."], ["Buonasera, signore.", "Tutto tranquillo."]),
        (["Costa 3,5 euro. Bene."], ["Costa 3,5 euro.", "Bene."]),
        (["Chieda al Sig. Rossi o al Dott. Bianchi. Subito."], ["Chieda al Sig. Rossi o al Dott. Bianchi.", "Subito."]),
        (["Pagine, bozze ecc. sono pronte. Altro?"], ["Pagine, bozze ecc. sono pronte.", "Altro?"]),
        (["Vedi la pag. 4, per es. il titolo. Fatto."], ["Vedi la pag. 4, per es. il titolo.", "Fatto."]),
        (
            ["Ho controllato le sei pull request aperte, e due falliscono. Le guardo?"],
            ["Ho controllato le sei pull request aperte,", "e due falliscono.", "Le guardo?"],
        ),
        (["Subito, signore.", BLOCK, "L'attività è in coda."], ["Subito, signore.", "L'attività è in coda."]),
        (["Davvero? Sì! Certo."], ["Davvero?", "Sì!", "Certo."]),
    ],
)
def test_splits_into_sentences(pieces: list[object], chunks: list[str]) -> None:
    assert split(*pieces) == chunks


def test_a_sentence_goes_as_soon_as_the_space_after_it_arrives() -> None:
    splitter = SentenceSplitter()

    assert splitter.feed("Good evening, sir.") == []
    assert splitter.feed(" All") == ["Good evening, sir."]


def test_a_number_cut_between_deltas_is_not_a_sentence_end() -> None:
    splitter = SentenceSplitter()

    assert splitter.feed("It costs 3.") == []
    assert splitter.feed("5 dollars") == []
    assert splitter.flush() == ["It costs 3.5 dollars"]


def test_the_end_of_a_text_block_ends_a_sentence() -> None:
    splitter = SentenceSplitter()

    assert splitter.feed("Right away, sir.") == []
    assert splitter.end_block() == ["Right away, sir."]
    assert splitter.feed("The task") == []


@pytest.mark.parametrize(
    ("text", "ends"),
    [
        ("Good evening, sir.", True),
        ("Really?", True),
        ("Indeed!", True),
        ('He said "done."', True),
        ("Buonasera, signore.", True),
        ("It costs 3.", False),
        ("Version 1.", False),
        ("Ask Mr.", False),
        ("Chieda al Dott.", False),
        ("Run `uv sync.", False),
        ("1.", False),
        ("Good evening, sir. ", False),
        ("Good evening", False),
        ("", False),
    ],
)
def test_knows_when_the_text_so_far_could_end_on_a_sentence(text: str, ends: bool) -> None:
    splitter = SentenceSplitter()
    splitter.feed(text)

    assert splitter.at_sentence_end() is ends
