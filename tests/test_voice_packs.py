from pathlib import Path

import pytest

from jarvis_hub.voice.packs import (
    DEFAULT_VOICES,
    SHIPPED_VOICES,
    VoicePackError,
    find_pack,
    list_packs,
    load_pack,
    save_pack,
)


def make_pack(root: Path, name: str, manifest: str, clip: bool = True) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "voice.toml").write_text(manifest, encoding="utf-8")
    if clip:
        (folder / "reference.wav").write_bytes(b"RIFF")
    return folder


ENGLISH = 'language = "en"\ntranscript = "Of course, sir."\ntrim_db = -1.5\n'


def test_a_pack_carries_its_clip_language_transcript_and_trim(tmp_path: Path) -> None:
    folder = make_pack(tmp_path, "butler", ENGLISH + 'description = "A dry butler."\n')

    pack = load_pack(folder)

    assert (pack.name, pack.clip, pack.language, pack.transcript, pack.trim_db, pack.description) == (
        "butler", folder / "reference.wav", "en", "Of course, sir.", -1.5, "A dry butler.")


@pytest.mark.parametrize(("manifest", "clip", "message"), [
    (ENGLISH, False, "reference.wav"),
    ('language = "fr"\ntranscript = "Bien sûr."\ntrim_db = 0\n', True, "en, it"),
    ('language = "en"\ntrim_db = 0\n', True, "transcript"),
    ('language = "en"\ntranscript = "Yes."\ntrim_db = "loud"\n', True, "trim_db"),
    ('language = "en"\ntranscript = "Yes."\ntrim_db = nan\n', True, "trim_db"),
    ('language = "en"\ntranscript = "Yes.\n', True, "voice.toml"),
], ids=["no clip", "unsupported language", "no transcript", "trim not a number", "trim not finite", "broken toml"])
def test_a_broken_pack_is_refused_naming_what_is_wrong(tmp_path: Path, manifest: str, clip: bool, message: str) -> None:
    folder = make_pack(tmp_path, "broken", manifest, clip=clip)

    with pytest.raises(VoicePackError, match=message):
        load_pack(folder)


def test_the_default_voices_ship_with_the_checkout_at_their_measured_trims() -> None:
    english, italian = find_pack(DEFAULT_VOICES["en"], None), find_pack(DEFAULT_VOICES["it"], None)

    assert english.folder.parent == italian.folder.parent == SHIPPED_VOICES
    assert (english.language, english.trim_db, italian.language, italian.trim_db) == ("en", -2.0, "it", -4.3)
    assert english.clip.stat().st_size > 100_000 and italian.clip.stat().st_size > 100_000


def test_a_users_voice_is_found_beside_the_shipped_ones_and_wins_a_name_clash(tmp_path: Path) -> None:
    make_pack(tmp_path, "butler", ENGLISH)
    make_pack(tmp_path, "jarvis-en", ENGLISH)

    assert find_pack("butler", tmp_path).folder == tmp_path / "butler"
    assert find_pack("jarvis-en", tmp_path).folder == tmp_path / "jarvis-en"
    assert find_pack("jarvis-it", tmp_path).folder.parent == SHIPPED_VOICES


def test_an_unknown_voice_names_the_ones_there_are(tmp_path: Path) -> None:
    with pytest.raises(VoicePackError, match=r"no voice named 'nobody'.*jarvis-en.*jarvis-it"):
        find_pack("nobody", tmp_path)


def test_list_shows_every_pack_and_why_a_broken_one_cannot_be_used(tmp_path: Path) -> None:
    make_pack(tmp_path, "butler", ENGLISH)
    make_pack(tmp_path, "broken", ENGLISH, clip=False)

    listed = dict(list_packs(tmp_path))

    assert {"jarvis-en", "jarvis-it", "butler", "broken"} <= set(listed)
    assert isinstance(listed["broken"], str) and "reference.wav" in listed["broken"]
    assert not isinstance(listed["butler"], str) and listed["butler"].language == "en"


def test_a_saved_pack_loads_back_with_its_clip_copied_and_tricky_text_intact(tmp_path: Path) -> None:
    source = tmp_path / "take.wav"
    source.write_bytes(b"RIFF take")
    transcript = 'L\'unica domanda è "stasera"?\\ Sì.'

    pack = save_pack(tmp_path / "voices", "mio", source, "it", transcript, -3.25,
                     description="Calmo.\nPreciso.", model="some/model")

    assert load_pack(tmp_path / "voices" / "mio") == pack
    assert (pack.transcript, pack.trim_db, pack.description, pack.model) == (transcript, -3.2, "Calmo.\nPreciso.", "some/model")
    assert pack.clip.read_bytes() == b"RIFF take"


@pytest.mark.parametrize("name", ["jarvis-en", "Bad Name", "../escape", ""])
def test_saving_refuses_a_taken_or_unsafe_name(tmp_path: Path, name: str) -> None:
    source = tmp_path / "take.wav"
    source.write_bytes(b"RIFF")

    with pytest.raises(VoicePackError):
        save_pack(tmp_path / "voices", name, source, "en", "Yes.", 0.0)
    assert not (tmp_path / "voices").exists() or not any((tmp_path / "voices").iterdir())
