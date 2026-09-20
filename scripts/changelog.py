"""Read one version's section out of CHANGELOG.md.

The release workflow publishes what this prints, so the GitHub release, the changelog in the repository and
the site's changelog page are the same words rather than three drifting accounts of one release. It fails
loudly on a missing section: a release whose notes would silently be empty is a release nobody can read.

    uv run python scripts/changelog.py 0.3.3        # that version's entry
    uv run python scripts/changelog.py --latest     # the topmost entry, and its version
    uv run python scripts/changelog.py --check 0.3.3
"""

import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
# Keep a Changelog writes a version as a link reference (`## [0.3.3] - 2026-09-20`); the brackets are
# optional here so an entry written either way is still found.
_HEADING = re.compile(r"^## \[?(?P<version>\d+\.\d+\.\d+)\]? - (?P<date>\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


class ChangelogError(RuntimeError):
    """The changelog does not hold what a release needs from it."""


def entries(text: str) -> dict[str, str]:
    """Every version's section, in the order they appear."""
    found: dict[str, str] = {}
    matches = list(_HEADING.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found[match.group("version")] = text[match.end() : end].strip()
    return found


def section(text: str, version: str) -> str:
    sections = entries(text)
    if version not in sections:
        known = ", ".join(sections) or "none"
        raise ChangelogError(f"CHANGELOG.md has no section for {version} (has: {known})")
    body = sections[version]
    if not body:
        raise ChangelogError(f"CHANGELOG.md's section for {version} is empty")
    return body


def latest(text: str) -> tuple[str, str]:
    for version, body in entries(text).items():
        return version, body
    raise ChangelogError("CHANGELOG.md has no version sections")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print one version's changelog entry.")
    parser.add_argument("version", nargs="?", help="the version to print, e.g. 0.3.3")
    parser.add_argument("--latest", action="store_true", help="print the topmost entry instead")
    parser.add_argument("--check", metavar="VERSION", help="exit non-zero unless that version has an entry")
    args = parser.parse_args()
    text = CHANGELOG.read_text(encoding="utf-8")
    try:
        if args.check:
            section(text, args.check)
            print(f"CHANGELOG.md documents {args.check}")
            return 0
        if args.latest:
            version, body = latest(text)
            print(f"{version}\n{body}")
            return 0
        if not args.version:
            parser.error("give a version, --latest or --check VERSION")
        print(section(text, args.version))
    except ChangelogError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
