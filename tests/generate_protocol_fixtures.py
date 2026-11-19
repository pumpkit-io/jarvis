"""Regenerate the single shared fixture from actual loopback exchanges with fake agents."""
import asyncio
import tempfile
from pathlib import Path

from tests.protocol_exchanges import FIXTURE, encoded, exchanges


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        value = asyncio.run(exchanges(Path(directory)))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(encoded(value), encoding="utf-8")
    print(FIXTURE)


if __name__ == "__main__":
    main()
