import json

import pytest

from fastbrowse.adapters.bitwarden import BitwardenError, covers, login_values


def item(*uris: str, password: str | None = "hunter2") -> str:
    login = {"username": "me@example.com", "password": password, "uris": [{"match": None, "uri": u} for u in uris]}
    return json.dumps({"id": "1", "name": "Amazon", "type": 1, "login": login})


@pytest.mark.parametrize(
    ("uri", "origin", "expected"),
    [
        ("https://www.amazon.com", "https://www.amazon.com", True),
        ("amazon.com", "https://www.amazon.com", True),
        ("https://amazon.com/ap/signin", "https://smile.amazon.com", True),
        ("https://amazon.com", "https://amazon.com.evil.test", False),
        ("https://amazon.com", "https://notamazon.com", False),
        ("", "https://amazon.com", False),
    ],
)
def test_a_vault_uri_covers_its_domain_and_subdomains_only(uri: str, origin: str, expected: bool) -> None:
    assert covers(uri, origin) is expected


def test_login_values_are_named_and_scoped_to_the_items_uris() -> None:
    assert login_values(item("amazon.com"), "https://www.amazon.com") == {
        "username": "me@example.com",
        "password": "hunter2",
    }
    assert login_values(item("amazon.com", password=None), "https://www.amazon.com") == {"username": "me@example.com"}
    with pytest.raises(BitwardenError, match="not saved for"):
        login_values(item("amazon.com"), "https://evil.test")


def test_a_malformed_item_error_does_not_echo_the_password() -> None:
    malformed = json.dumps({"login": {"password": "hunter2"}})
    with pytest.raises(BitwardenError) as raised:
        login_values(malformed, "https://www.amazon.com")
    assert "hunter2" not in str(raised.value)
