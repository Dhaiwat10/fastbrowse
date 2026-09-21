from urllib.parse import unquote

import pytest

from fastbrowse.citations import text_fragment


@pytest.mark.parametrize(
    ("text", "encoded"),
    [
        ("a-b & c,d", "a%2Db%20%26%20c%2Cd"),
        ("caf\u00e9 \u6771\u4eac", "caf%C3%A9%20%E6%9D%B1%E4%BA%AC"),
        ("100% #1 / (yes?)", "100%25%20%231%20%2F%20%28yes%3F%29"),
        ("  a\n\tb\u00a0c  ", "a%20b%20c"),
    ],
)
def test_text_fragment_encodes_terms(text: str, encoded: str) -> None:
    assert text_fragment("https://example.test/a?x=1&y=2", text) == f"https://example.test/a?x=1&y=2#:~:text={encoded}"


@pytest.mark.parametrize("fragment", ["#section", "#section:~:text=old", "#section:~:text=old&text=other"])
def test_text_fragment_preserves_anchor_and_replaces_old_directive(fragment: str) -> None:
    assert text_fragment(f"https://example.test/{fragment}", "new") == "https://example.test/#section:~:text=new"


def test_long_quotes_use_nonoverlapping_whole_words_at_both_ends() -> None:
    words = [f"word-{n}," for n in range(30)]
    start, end = text_fragment("https://example.test", " ".join(words)).split("text=")[1].split(",")
    assert unquote(start) == " ".join(words[:5])
    assert unquote(end) == " ".join(words[-5:])
    assert "%2D" in start and "%2C" in end
    long_word = "a" * 121
    assert text_fragment("https://example.test#", long_word).endswith(f"text={long_word}")


def test_empty_quotes_cannot_make_a_text_fragment() -> None:
    with pytest.raises(ValueError, match="nonempty quote"):
        text_fragment("https://example.test", " \n")
