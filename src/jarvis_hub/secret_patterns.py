"""What counts as a secret in text the Hub keeps or replays: Memory's summaries and `recall`'s results (ADR 0028).

The patterns aim at values, not at talk about them: "I reset my password" and "the token was re-issued" hold no
secret, "the password is h4ppy-days" does. Where a pattern names its `secret` group, only that part is the secret and
the words before it stay readable.
"""

import re

REDACTED = "[secret removed]"

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(pattern) for pattern in (
    # Anthropic, OpenAI and similar keys.
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}",
    # GitHub tokens, classic and fine-grained.
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{35}",
    # A JSON Web Token: three base64url parts, the first two JSON objects.
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*",
    # A paired Client's token, as `pairing.py` issues it: its id, a dot, then the secret.
    r"\b[0-9a-f]{32}\.[A-Za-z0-9_-]{40,}",
    # A random-looking run of 32 or more: upper and lower case and digits, which a commit hash never mixes.
    r"\b(?=[\w-]*[A-Z])(?=[\w-]*[a-z])(?=[\w-]*\d)[A-Za-z0-9_-]{32,}",
    # A pairing code: six digits near the words that name one.
    r"(?i)\b(?:pairing|pair)\s+code\b\D{0,20}\b(?P<secret>\d{6})\b",
    # A value given for a password or key: quoted, or eight or more characters with a digit or a symbol, never a
    # setting's NAME_IN_CAPS, and without the sentence's closing punctuation.
    r"(?i)\b(?:password|passcode|passphrase|parola d'ordine|api[ _-]?key|secret|token)\b\s*"
    r"(?:[:=]|\bis\b|\bè\b|\bwas\b|\bera\b)\s*"
    r"(?P<secret>\"[^\"\s]{4,}\"|'[^'\s]{4,}'|`[^`\s]{4,}`"
    r"|(?-i:(?![A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b))(?=[^\s,;]*?[\d@#$%^&*_+=/\\~])[^\s,;]{7,}[^\s,;.!?:)])",
    r"(?i)\bpin\b\s*(?:[:=]|\bis\b|\bè\b|\bwas\b|\bera\b)\s*(?P<secret>\d{4,})",
))


def find_secret(text: str) -> str | None:
    """The first secret in `text`, or None."""
    for pattern in SECRET_PATTERNS:
        if match := pattern.search(text):
            return _secret(match)
    return None


def redact(text: str) -> str:
    """`text` with every secret replaced in place by REDACTED."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(_redacted, text)
    return text


def _secret(match: re.Match[str]) -> str:
    return match.group("secret") if "secret" in match.re.groupindex else match.group()


def _redacted(match: re.Match[str]) -> str:
    if "secret" not in match.re.groupindex:
        return REDACTED
    start, end = match.span("secret")
    whole = match.group()
    return whole[:start - match.start()] + REDACTED + whole[end - match.start():]
