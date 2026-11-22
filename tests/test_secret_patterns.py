"""The shared secret patterns: they find secret-looking values and leave ordinary talk about secrets alone."""

import pytest

from jarvis_hub.secret_patterns import REDACTED, find_secret, redact

SECRETS = {
    "a GitHub token": ("Use ghp_abcdefghijklmnopqrstuvwxyz0123 for it.", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
    "a fine-grained GitHub token": ("github_pat_11ABCDEFG0123456789_abcdefghij", "github_pat_11ABCDEFG0123456789_abcdefghij"),
    "an Anthropic key": ("my key is sk-ant-api03-abcdefghijklmnop", "sk-ant-api03-abcdefghijklmnop"),
    "a Slack token": ("xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
    "an AWS key id": ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    "a long random string": ("it is Zx81Qp0LmN3vR7tY2wK9sD4fG6hJ8kLb ok", "Zx81Qp0LmN3vR7tY2wK9sD4fG6hJ8kLb"),
    "a password with a digit": ("The wifi password is h4ppy-days.", "h4ppy-days"),
    "a password with a symbol": ("password: correct#horse", "correct#horse"),
    "a token given with =": ("token=Ab3dEf7hIj", "Ab3dEf7hIj"),
    "an Italian password": ("La password è gatto2024!", "gatto2024"),
    "a quoted secret": ('the secret is "swordfish"', '"swordfish"'),
    "a pairing code": ("The pairing code is 482913.", "482913"),
    "a PIN": ("il pin è 4821", "4821"),
}

ORDINARY = {
    "a token re-issued": "The GitHub token was re-issued this morning.",
    "an environment variable named": "secret: JARVIS_REMOTE_TOKEN",
    "an Italian expired token": "Il token è scaduto-ieri, ne serve uno nuovo.",
    "a password reset": "I reset my password yesterday.",
    "a short word after the keyword": "The secret is out.",
    "a commit hash": "Merged at 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39.",
    "a date": "The token expires on 2026-12-01.",
    "Italian talk about a key": "La chiave API è nel portachiavi, non nel repo.",
}


@pytest.mark.parametrize(("text", "secret"), SECRETS.values(), ids=SECRETS.keys())
def test_a_secret_looking_value_is_found_and_redacted_in_place(text: str, secret: str) -> None:
    assert find_secret(text) == secret
    assert secret not in redact(text) and REDACTED in redact(text)


@pytest.mark.parametrize("text", ORDINARY.values(), ids=ORDINARY.keys())
def test_ordinary_talk_about_secrets_holds_none(text: str) -> None:
    assert find_secret(text) is None
    assert redact(text) == text
