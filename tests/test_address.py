import pytest

from jarvis_hub.address import AddressGuard

END = object()


def guarded(*pieces: object) -> str:
    """Feeds text deltas through one guard; END marks the end of a text block."""
    guard, out = AddressGuard(), ""
    for piece in pieces:
        out += guard.end() if piece is END else guard.feed(str(piece))
    return out + guard.end()


@pytest.mark.parametrize(
    ("pieces", "expected"),
    [
        (["Certainly, sir. I'll look into it.", END, " Under way, sir. I'll report shortly."],
         "Certainly, sir. I'll look into it. Under way. I'll report shortly."),
        (["Very good", ",", " s", "ir", ". On it", ", si", "r", "."], "Very good, sir. On it."),
        (["I can, sir, at least indirectly. Looking now, sir, as asked."],
         "I can, sir, at least indirectly. Looking now, as asked."),
        (["Understood, sir: trigger at two hundred.", END, " Recorded, sir. Question four is next."],
         "Understood, sir: trigger at two hundred. Recorded. Question four is next."),
        (["Molto bene, signore: vado a cercare.", END, " Ho avviato l'indagine, signore."],
         "Molto bene, signore: vado a cercare. Ho avviato l'indagine."),
        (["Right away, sir.", END, " Done, sir", END], "Right away, sir. Done"),
        (["Sir, the build is green.", END, " Yes, all of it, sir."], "Sir, the build is green. Yes, all of it."),
        (["S", "ir, the build is green. Yes, sir."], "Sir, the build is green. Yes."),
    ],
)
def test_only_the_first_sir_of_a_reply_stays(pieces: list[object], expected: str) -> None:
    assert guarded(*pieces) == expected


@pytest.mark.parametrize(
    "text",
    ["Very good, sir.", "Nothing running.", "Ask Sir Walter, sir.", "Rather well, sire. The sirloin, sir, is done."],
)
def test_a_reply_with_at_most_one_address_is_unchanged(text: str) -> None:
    assert guarded(text) == text


@pytest.mark.parametrize(
    "pieces",
    [["Pardon, sir", "loin is on. Very good, sir."], ["Yes, sir", "ius is bright. Done, sir."]],
)
def test_a_word_that_begins_with_sir_across_deltas_does_not_use_up_the_address(pieces: list[str]) -> None:
    assert guarded(*pieces) == "".join(pieces)


def test_text_is_held_only_while_it_may_become_a_second_address() -> None:
    guard = AddressGuard()

    assert guard.feed("Good evening, ") == "Good evening, "
    assert guard.feed("s") == "s"
    assert guard.feed("ir. Three meetings") == "ir. Three meetings"
    assert guard.feed(", s") == ""
    assert guard.feed("ir.") == "."
    assert guard.feed(" Shall I, s") == " Shall I"
    assert guard.feed("ay, go on?") == ", say, go on?"
