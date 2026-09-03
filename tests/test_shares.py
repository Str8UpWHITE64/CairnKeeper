"""Share-code parsing and decoding.

Codes arrive pasted from Discord with names and punctuation around them, and a
shared route is base64 JSON plus a binary signature.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.shares import decode_share, parse_codes  # noqa: E402

# A real shared route captured from /GetFriends.
REAL_SHARE = (
    "eyJzaGFyZVB1YmxpY1ZlcnNpb24iOjIsInJvdXRlU3RhZ2UiOjIsInJlbGljSURzIjpbIiJd"
    "LCJkdW5nZW9uS2V5cyI6bnVsbH0=,eEYU/T6cE6rN4SpX2fS3eMWaANEz2DHepJzSo1l56b2e"
)


def test_extracts_codes_from_pasted_text() -> None:
    text = "check these out: endohifg, afldbifg\nand @someone said abcd1234!"
    assert parse_codes(text) == ["endohifg", "afldbifg", "abcd1234"]


def test_codes_are_deduplicated_in_order() -> None:
    assert parse_codes("aaaaaaaa bbbbbbbb aaaaaaaa") == ["aaaaaaaa", "bbbbbbbb"]


def test_ignores_tokens_that_are_not_code_shaped() -> None:
    assert parse_codes("hi a ab https://exam.com/x") == []


def test_case_is_normalised() -> None:
    assert parse_codes("ENDOHIFG") == ["endohifg"]


def test_decodes_a_real_shared_route() -> None:
    decoded = decode_share(REAL_SHARE)
    assert decoded is not None
    assert decoded["sharePublicVersion"] == 2
    assert decoded["routeStage"] == 2


def test_undecodable_share_is_not_fatal() -> None:
    assert decode_share("not-base64,xxx") is None
    assert decode_share("") is None
    assert decode_share(None) is None


def test_code_dash_name_format() -> None:
    """Community lists are '<code>-<player name>' per line."""
    text = "afldbifg-leon2365\nagdkbifg-LyCH_OS\nalcpbifg-Ayr (KayrBayr)"
    assert parse_codes(text) == ["afldbifg", "agdkbifg", "alcpbifg"]


def test_eight_character_player_names_are_not_taken_as_codes() -> None:
    """'leon2365' is 8 chars but sits after the dash, so it is a name."""
    assert parse_codes("afldbifg-leon2365") == ["afldbifg"]


def test_comments_are_ignored() -> None:
    text = "# these are excludes, whatever\nafldbifg-leon2365\n# oefcifg-malformed"
    assert parse_codes(text) == ["afldbifg"]


def test_names_containing_dashes_still_work() -> None:
    assert parse_codes("mhijfifg--Ventiii-VIPs- DEV") == ["mhijfifg"]


def test_malformed_code_is_skipped() -> None:
    """'oefcifg' is 7 characters."""
    assert parse_codes("oefcifg-Othdurn") == []


def test_unicode_username_does_not_crash_printing() -> None:
    """A name Windows' cp1252 cannot encode killed a 114-code run at 101."""
    from phantom_offline.shares import safe

    for name in ("是丁丁呀", "Ω≈ç√", "plain", None, 42):
        assert isinstance(safe(name), str)
    assert safe(None) == ""
