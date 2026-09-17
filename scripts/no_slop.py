"""Fail the build on the punctuation and phrasing that mark text as machine-written.

Prose in this repository is read by people deciding whether to trust the agent, so it is held to
the same gate as the code. The two rules below are the ones a model breaks and a person does not:

1. Typographic punctuation nobody reaches for on a keyboard: em and en dashes, curly quotes.
   Write an ASCII hyphen, a comma, a colon, or two sentences.
2. A lexicon of filler that survives no edit: words chosen to sound weighty rather than to say
   something. Each entry is here because it carries no information a plain word would not.

Escape one deliberately with `slop-ok: <reason>` on the same line, and say why it has to stay.

    uv run python scripts/no_slop.py [PATH ...]
"""

import re
import subprocess
import sys
from pathlib import Path

SUFFIXES = frozenset({".py", ".md", ".toml", ".yml", ".yaml", ".html", ".sh", ".txt", ".json", ".cfg"})

# Built from code points so this table is not itself a line of the punctuation it bans.
CHARACTERS = {
    chr(0x2014): "em dash",
    chr(0x2013): "en dash",
    chr(0x2015): "horizontal bar",
    chr(0x201C): "curly double quote",
    chr(0x201D): "curly double quote",
    chr(0x2018): "curly single quote",
    chr(0x2019): "curly apostrophe",
}

PHRASES = (
    "delve",
    "seamless",
    "tapestry",
    "testament to",
    "game-chang",
    "unleash",
    "supercharge",
    "effortless",
    "cutting-edge",
    "state-of-the-art",
    "ever-evolving",
    "in today's",
    "look no further",
    "dive into",
    "harness the power",
    "unlock the",
    "elevate your",
    "at the end of the day",
    "it's not just",
    "meticulous",
    "leverage",
    "robust",
)
PHRASE_RE = re.compile("|".join(re.escape(phrase) for phrase in PHRASES), re.IGNORECASE)

ALLOW = re.compile(r"slop-ok:\s*\S")


def tracked(roots: list[str]) -> list[Path]:
    listing = subprocess.run(["git", "ls-files", "-z", *roots], capture_output=True, text=True, check=True)
    return [path for name in listing.stdout.split("\0") if name and (path := Path(name)).suffix in SUFFIXES]


def offences(path: Path) -> list[str]:
    found: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if ALLOW.search(line):
            continue
        for column, character in enumerate(line, start=1):
            if name := CHARACTERS.get(character):
                found.append(f"{path}:{number}:{column}: {name} ({character!r})")
        for match in PHRASE_RE.finditer(line):
            found.append(f"{path}:{number}:{match.start() + 1}: {match.group()!r} says nothing a plain word would not")
    return found


def main(argv: list[str]) -> int:
    here = Path(__file__)
    found = [
        offence
        for path in tracked(argv)
        # This file names every banned string, so checking it would always fail.
        if path.resolve() != here.resolve()
        for offence in offences(path)
    ]
    for offence in found:
        print(offence)
    if found:
        print(f"\n{len(found)} found. Rewrite them, or annotate the line with `slop-ok: <reason>`.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
