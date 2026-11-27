"""Voice packs: a reference clip and its `voice.toml`, which Qwen3-TTS clones (ADR 0029, ADR 0035).

The shipped packs live in `assets/voice/`; the User's own live in `.jarvis/voices/` and win a name clash.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import shutil
import tomllib
from typing import Any, get_args

from jarvis_hub.voice.language import Lang

SHIPPED_VOICES = Path(__file__).resolve().parents[3] / "assets" / "voice"
DEFAULT_VOICES: dict[Lang, str] = {"en": "jarvis-en", "it": "jarvis-it"}
# The names Qwen3-TTS takes for its `lang_code`.
LANGUAGE_NAMES: dict[Lang, str] = {"en": "English", "it": "Italian"}
CLIP = "reference.wav"
MANIFEST = "voice.toml"
# Also a folder name and an .env value, so nothing that could leave the voices folder or need quoting.
VOICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


LANGUAGES: tuple[Lang, ...] = get_args(Lang)


class VoicePackError(ValueError):
    pass


def parse_language(value: object) -> Lang | None:
    return next((lang for lang in LANGUAGES if lang == value), None)


def check_new_name(name: str, user_voices: Path | None) -> None:
    if not VOICE_NAME.match(name):
        raise VoicePackError(f"{name!r} is not a voice name: use lowercase letters, digits and hyphens")
    if name in _folders(user_voices):
        raise VoicePackError(f"a voice named {name!r} already exists; pick another name")


@dataclass(frozen=True)
class VoicePack:
    name: str
    folder: Path
    language: Lang
    # Word for word what the clip says; cloning needs it exact.
    transcript: str
    # Brings this voice's clone to SPEECH_LUFS.
    trim_db: float
    description: str | None = None
    model: str | None = None

    @property
    def clip(self) -> Path:
        return self.folder / CLIP


def load_pack(folder: Path) -> VoicePack:
    where = f"voice {folder.name!r} ({folder})"
    try:
        data: dict[str, Any] = tomllib.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VoicePackError(f"{where} has no {MANIFEST}") from None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
        raise VoicePackError(f"{where} has an unreadable {MANIFEST}: {e}") from None
    transcript, trim = data.get("transcript"), data.get("trim_db")
    language = parse_language(data.get("language"))
    if language is None:
        raise VoicePackError(f"{where}: language {data.get('language')!r} is not one Jarvis speaks ({', '.join(LANGUAGES)})")
    if not isinstance(transcript, str) or not transcript.strip():
        raise VoicePackError(f"{where}: {MANIFEST} needs the clip's exact transcript")
    if isinstance(trim, bool) or not isinstance(trim, int | float) or not math.isfinite(trim):
        raise VoicePackError(f"{where}: trim_db must be a number of dB")
    if not (folder / CLIP).is_file():
        raise VoicePackError(f"{where} has no {CLIP}")
    description, model = data.get("description"), data.get("model")
    return VoicePack(folder.name, folder, language, transcript, float(trim),
                     description if isinstance(description, str) else None, model if isinstance(model, str) else None)


def _folders(user_voices: Path | None) -> dict[str, Path]:
    folders: dict[str, Path] = {}
    for root in (SHIPPED_VOICES, user_voices):
        if root is not None and root.is_dir():
            folders.update({p.name: p for p in sorted(root.iterdir()) if p.is_dir() and VOICE_NAME.match(p.name)})
    return folders


def find_pack(name: str, user_voices: Path | None) -> VoicePack:
    folders = _folders(user_voices)
    if name not in folders:
        raise VoicePackError(f"no voice named {name!r}; there are {', '.join(sorted(folders))}. See ./jarvis voice list.")
    return load_pack(folders[name])


def list_packs(user_voices: Path | None) -> list[tuple[str, VoicePack | str]]:
    """Every pack by name, or why it cannot be used."""
    listed: list[tuple[str, VoicePack | str]] = []
    for name, folder in sorted(_folders(user_voices).items()):
        try:
            listed.append((name, load_pack(folder)))
        except VoicePackError as e:
            listed.append((name, str(e)))
    return listed


def save_pack(
    user_voices: Path, name: str, clip: Path, language: Lang, transcript: str, trim_db: float, *,
    description: str | None = None, model: str | None = None,
) -> VoicePack:
    check_new_name(name, user_voices)
    fields: dict[str, object] = {"language": language, "transcript": " ".join(transcript.split()),
                                 "trim_db": round(trim_db, 1), "description": description, "model": model}
    # A JSON string or number is valid TOML for these values.
    manifest = "".join(f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in fields.items()
                       if value is not None)
    folder = user_voices / name
    staging = user_voices / f".{name}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        shutil.copyfile(clip, staging / CLIP)
        (staging / MANIFEST).write_text(manifest, encoding="utf-8")
        staging.rename(folder)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_pack(folder)
