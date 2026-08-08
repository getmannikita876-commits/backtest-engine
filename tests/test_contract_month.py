"""Tests for the delivery-month vocabulary (ADR-009).

All twelve codes are exercised, both directions of the code/calendar mapping
are checked against each other, and every plausible malformed spelling is
probed. A month-code table that is wrong by one shifts every contract in the
research universe by a month, silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from quant_research_terminal.domain.contract_month import MONTHS_IN_YEAR, ContractMonth

#: The mapping restated independently of the implementation. If the two ever
#: disagree, one of them is wrong and the test says so — a test that derived
#: this from the enum would agree with any table, including a broken one.
EXPECTED: dict[str, int] = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}


def test_there_are_exactly_twelve_months() -> None:
    assert len(ContractMonth) == MONTHS_IN_YEAR == 12


def test_every_code_maps_to_the_expected_calendar_month() -> None:
    assert {month.code: month.month_number for month in ContractMonth} == EXPECTED


def test_codes_are_unique() -> None:
    codes = [month.code for month in ContractMonth]

    assert len(set(codes)) == len(codes)


def test_month_numbers_cover_one_to_twelve_exactly_once() -> None:
    assert sorted(month.month_number for month in ContractMonth) == list(range(1, 13))


@pytest.mark.parametrize("code", sorted(EXPECTED))
def test_from_code_accepts_every_published_code(code: str) -> None:
    month = ContractMonth.from_code(code)

    assert month.code == code
    assert month.month_number == EXPECTED[code]


def test_from_code_and_month_number_are_mutual_inverses() -> None:
    for month in ContractMonth:
        assert ContractMonth.from_code(month.code) is month
        assert ContractMonth.from_month_number(month.month_number) is month


@pytest.mark.parametrize(
    "code",
    [
        "",
        " ",
        "  ",
        "\t",
        "\n",
        "m",
        "f",
        "z",  # lower case is not folded
        " M",
        "M ",
        " M ",
        "MM",
        "M6",
        "6",
        "A",
        "B",  # not a delivery-month code
        "C",
        "D",
        "E",
        "I",  # deliberately absent from the vocabulary
        "L",
        "O",
        "P",
        "R",
        "S",
        "T",
        "W",
        "Y",
        "JUNE",
        "June",
        "Ｍ",  # full-width M
        "М",  # Cyrillic capital Em, visually 'M'
        "M\n",
    ],
)
def test_from_code_rejects_anything_but_the_twelve_codes(code: str) -> None:
    with pytest.raises(ValueError):
        ContractMonth.from_code(code)


def test_from_code_covers_the_whole_ascii_uppercase_alphabet_correctly() -> None:
    """Exactly twelve of the twenty-six letters are codes; the rest must fail."""
    accepted = set()
    for letter in map(chr, range(ord("A"), ord("Z") + 1)):
        try:
            accepted.add(ContractMonth.from_code(letter).code)
        except ValueError:
            continue

    assert accepted == set(EXPECTED)


@pytest.mark.parametrize("code", [None, 6, True, b"M", ["M"], 6.0])
def test_from_code_rejects_non_string_input(code: Any) -> None:
    with pytest.raises(ValueError):
        ContractMonth.from_code(code)


@pytest.mark.parametrize("month_number", [0, 13, -1, 100, 2026])
def test_from_month_number_rejects_out_of_range_values(month_number: int) -> None:
    with pytest.raises(ValueError):
        ContractMonth.from_month_number(month_number)


@pytest.mark.parametrize("month_number", [True, False, 6.0, "6", None, [6]])
def test_from_month_number_rejects_non_int_values(month_number: Any) -> None:
    # bool is checked first: True would otherwise silently mean January.
    with pytest.raises(ValueError):
        ContractMonth.from_month_number(month_number)


def test_the_enum_value_is_the_code_so_it_serializes_canonically() -> None:
    assert ContractMonth.JUNE.value == "M"
    assert ContractMonth.JUNE.code == "M"
    assert f"{ContractMonth.JUNE}" == "M"


def test_member_names_are_the_calendar_months() -> None:
    assert ContractMonth.JANUARY.month_number == 1
    assert ContractMonth.DECEMBER.month_number == 12
    assert ContractMonth.JUNE.name == "JUNE"


def test_delivery_ordering_is_available_without_ordering_identity() -> None:
    """Rollover needs chronological order; identity deliberately has none.

    The key is built by the caller from public components, so there is one
    place the code/calendar mapping lives and no comparison operator on an
    identity type that could be reached for by mistake.
    """
    months = sorted(ContractMonth, key=lambda month: month.month_number)

    assert [month.code for month in months] == list("FGHJKMNQUVXZ")
