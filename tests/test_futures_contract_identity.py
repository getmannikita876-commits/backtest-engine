"""Adversarial tests for canonical listed-futures-contract identity (ADR-009).

These are written to *break* the model, not to confirm it. The properties under
attack are the ones a research platform cannot recover from if they fail:

* two different listed contracts must never collide;
* one listed contract must never acquire two identities;
* no identity may depend on the wall clock, the timezone, the locale, or the
  hash seed;
* a product root must never pass as an executable contract;
* a vendor alias must never become canonical identity by accident.
"""

from __future__ import annotations

import itertools
import os
from typing import Any

import pytest

from quant_research_terminal.domain.contract_month import MONTHS_IN_YEAR, ContractMonth
from quant_research_terminal.domain.futures_contract import (
    CANONICAL_SEPARATOR,
    MAX_CONTRACT_YEAR,
    MIN_CONTRACT_YEAR,
    FuturesContractId,
    FuturesProduct,
    Venue,
    require_listed_contract,
    resolve_abbreviated_contract_year,
)

CME = Venue(code="CME")


def contract(
    root: str = "ES",
    month: ContractMonth = ContractMonth.JUNE,
    year: int = 2026,
    venue: Venue = CME,
) -> FuturesContractId:
    """Build a contract identity, varying one axis at a time in the tests."""
    return FuturesContractId(
        product=FuturesProduct(venue=venue, root=root),
        contract_month=month,
        contract_year=year,
    )


# --------------------------------------------------------------------------
# A. Equality
# --------------------------------------------------------------------------


def test_the_same_contract_is_equal_to_itself() -> None:
    assert contract() == contract()


def test_a_different_delivery_month_is_a_different_contract() -> None:
    # ESM6 and ESU6 are the canonical example: same product, different expiry,
    # different open interest, different price.
    assert contract(month=ContractMonth.JUNE) != contract(month=ContractMonth.SEPTEMBER)


def test_a_different_full_year_is_a_different_contract() -> None:
    assert contract(year=2016) != contract(year=2026)
    assert contract(year=2026) != contract(year=2036)


def test_a_different_product_root_is_a_different_contract() -> None:
    # MES is the micro contract: a different product with a different
    # multiplier, not a spelling of ES.
    assert contract(root="ES") != contract(root="MES")
    assert contract(root="ES") != contract(root="NQ")


def test_a_different_venue_is_a_different_contract() -> None:
    assert contract(venue=Venue(code="CME")) != contract(venue=Venue(code="ICE"))


def test_every_pair_of_the_twelve_months_is_distinct() -> None:
    contracts = [contract(month=month) for month in ContractMonth]

    assert len({identity.canonical() for identity in contracts}) == MONTHS_IN_YEAR
    for left, right in itertools.combinations(contracts, 2):
        assert left != right


def test_no_two_contracts_over_a_generated_grid_collide() -> None:
    """A brute-force sweep for identity collisions across all four axes."""
    venues = ["CME", "ICE", "CBOT", "X1A"]
    roots = ["ES", "MES", "NQ", "6E", "M2K", "ZN", "ES1"]
    years = [MIN_CONTRACT_YEAR, 1999, 2016, 2026, 2036, MAX_CONTRACT_YEAR]

    identities = [
        contract(root=root, month=month, year=year, venue=Venue(code=venue))
        for venue in venues
        for root in roots
        for month in ContractMonth
        for year in years
    ]
    expected = len(venues) * len(roots) * MONTHS_IN_YEAR * len(years)

    assert len(identities) == expected
    assert len(set(identities)) == expected
    assert len({identity.canonical() for identity in identities}) == expected


def _differs(left: object, right: object) -> bool:
    """Compare as ``object`` so the check is a runtime one.

    Comparing the concrete types directly is rejected by mypy as a
    non-overlapping equality check — which is itself the strongest possible
    result, and is asserted separately in
    :func:`test_distinct_identity_types_are_statically_non_comparable`. This
    helper defeats the narrowing so the *runtime* behaviour is also pinned: a
    type checker is not present when a research script runs.
    """
    return left != right and right != left


def test_a_product_never_equals_a_contract_or_a_venue() -> None:
    product = FuturesProduct(venue=CME, root="ES")

    assert _differs(product, contract())
    assert _differs(product, CME)
    assert _differs(CME, contract())


def test_distinct_identity_types_are_statically_non_comparable() -> None:
    """The type checker rejects the comparison outright, before runtime.

    ``mypy --strict`` reports ``comparison-overlap`` for
    ``FuturesProduct() == FuturesContractId()``, so code that confuses a
    product with a contract fails CI rather than silently evaluating to
    ``False``. This test records that the guarantee is intentional; the
    ``type: ignore`` codes below are what assert it, because an unused ignore
    is itself a mypy error under ``--strict``.
    """
    product = FuturesProduct(venue=CME, root="ES")
    identity = contract()

    assert not (product == identity)  # type: ignore[comparison-overlap]
    assert not (identity == product)  # type: ignore[comparison-overlap]


def test_a_contract_never_equals_its_own_canonical_string() -> None:
    # Guards against a StrEnum-style model where the identity would compare
    # equal to text and let a raw vendor string slip into an identity set.
    identity = contract()

    assert _differs(identity, identity.canonical())
    assert _differs(identity, "CME:ES:M2026")


# --------------------------------------------------------------------------
# B. Hashing and immutability
# --------------------------------------------------------------------------


def test_equal_identities_hash_equally_and_deduplicate() -> None:
    assert hash(contract()) == hash(contract())
    assert len({contract(), contract()}) == 1


def test_identities_work_as_dictionary_keys() -> None:
    positions = {contract(month=ContractMonth.JUNE): 3, contract(month=ContractMonth.SEPTEMBER): -1}

    assert positions[contract(month=ContractMonth.JUNE)] == 3
    assert positions[contract(month=ContractMonth.SEPTEMBER)] == -1
    assert len(positions) == 2


def test_a_product_and_a_contract_do_not_collide_in_one_set() -> None:
    product = FuturesProduct(venue=CME, root="ES")

    assert len({product, contract(), CME}) == 3


def test_identity_components_cannot_be_reassigned() -> None:
    identity = contract()

    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        identity.contract_year = 2027
    with pytest.raises(Exception):  # noqa: B017
        identity.product.root = "NQ"
    with pytest.raises(Exception):  # noqa: B017
        identity.product.venue.code = "ICE"

    assert identity.canonical() == "CME:ES:M2026"


def test_identity_holds_no_mutable_container() -> None:
    """Every component must itself be hashable, or the hash is a lie."""
    identity = contract()

    for value in (identity.product, identity.product.venue, identity.contract_month):
        hash(value)
    assert hash(identity) == hash(contract())


# --------------------------------------------------------------------------
# C. Canonical serialization and round-tripping
# --------------------------------------------------------------------------


def test_canonical_form_is_exactly_as_specified() -> None:
    assert CME.canonical() == "CME"
    assert FuturesProduct(venue=CME, root="ES").canonical() == "CME:ES"
    assert contract().canonical() == "CME:ES:M2026"
    assert contract(root="MES", month=ContractMonth.MARCH, year=2016).canonical() == "CME:MES:H2016"


def test_canonical_form_is_stable_across_repeated_calls() -> None:
    identity = contract()

    assert len({identity.canonical() for _ in range(100)}) == 1


def test_canonical_year_is_always_four_digits() -> None:
    for year in (MIN_CONTRACT_YEAR, 1999, 2006, 2026, MAX_CONTRACT_YEAR):
        delivery = contract(year=year).canonical().split(CANONICAL_SEPARATOR)[2]

        assert len(delivery) == 5
        assert delivery[1:] == str(year)


@pytest.mark.parametrize(
    "text",
    [
        "CME:ES:M2026",
        "CME:MES:H2016",
        "ICE:6E:Z9999",
        "CBOT:ZN:F1000",
        "X1A:M2K:U2030",
    ],
)
def test_parse_round_trips_canonical_form(text: str) -> None:
    assert FuturesContractId.parse(text).canonical() == text


def test_every_constructible_identity_round_trips() -> None:
    for month in ContractMonth:
        for year in (MIN_CONTRACT_YEAR, 2016, 2026, MAX_CONTRACT_YEAR):
            identity = contract(month=month, year=year)

            assert FuturesContractId.parse(identity.canonical()) == identity


def test_product_parse_round_trips() -> None:
    product = FuturesProduct(venue=CME, root="ES")

    assert FuturesProduct.parse(product.canonical()) == product


def test_parse_is_no_wider_than_canonical_output() -> None:
    """The parser must accept nothing the serializer cannot emit.

    A parser wider than the serializer is a second, looser definition of
    identity — the route by which a vendor spelling quietly becomes canonical.
    """
    rejected = [
        "cme:es:m2026",  # lower case
        "CME:ES:m2026",  # lower-case month code
        "CME:es:M2026",
        " CME:ES:M2026",  # leading whitespace
        "CME:ES:M2026 ",  # trailing whitespace
        "CME:ES:M26",  # abbreviated year
        "CME:ES:M6",
        "CME:ES:M02026",  # five-digit year
        "CME:ES",  # too few fields
        "CME:ES:M2026:X",  # too many fields
        "CME::M2026",  # empty root
        ":ES:M2026",  # empty venue
        "CME:ES:2026M",  # transposed
        "CME:ES:A2026",  # not a month code
        "CME:ES:M+026",
        "CME:ES:M-026",
        "CME:ES:M2O26",  # letter O for zero
        "ESM6",  # a vendor alias
        "ES1!",  # a continuous alias
        "CME:ES1!:M2026",
        "",
        ":",
        "::",
        "CME:ES:M٢026",  # Arabic-Indic digit
        "CME:ES:M２０２６",  # full-width digits
        "ＣＭＥ:ES:M2026",  # full-width letters
        "CME:ES:M2026\n",
        "\nCME:ES:M2026",
    ]

    for text in rejected:
        with pytest.raises(ValueError):
            FuturesContractId.parse(text)


def test_parse_rejects_non_string_input() -> None:
    for value in (None, 2026, b"CME:ES:M2026", ["CME", "ES", "M2026"]):
        with pytest.raises(ValueError):
            FuturesContractId.parse(value)  # type: ignore[arg-type]


def test_canonical_form_is_independent_of_locale() -> None:
    """Formatting must not consult locale — no separators, no digit shaping."""
    import locale

    original = locale.setlocale(locale.LC_ALL)
    expected = contract().canonical()
    try:
        for candidate in ("de_DE.UTF-8", "German_Germany.1252", "de_DE", "C"):
            try:
                locale.setlocale(locale.LC_ALL, candidate)
            except locale.Error:
                continue
            assert contract().canonical() == expected
    finally:
        locale.setlocale(locale.LC_ALL, original)


# --------------------------------------------------------------------------
# D. Root versus contract
# --------------------------------------------------------------------------


def test_a_root_only_value_cannot_be_constructed_as_a_contract() -> None:
    """There is no way to make a contract without stating month and year."""
    with pytest.raises(Exception):  # noqa: B017
        FuturesContractId(product=FuturesProduct(venue=CME, root="ES"))  # type: ignore[call-arg]


def test_a_product_is_rejected_where_a_listed_contract_is_required() -> None:
    with pytest.raises(TypeError):
        require_listed_contract(FuturesProduct(venue=CME, root="ES"))


def test_a_listed_contract_passes_the_executable_guard_unchanged() -> None:
    identity = contract()

    assert require_listed_contract(identity) is identity


# --------------------------------------------------------------------------
# E. Year ambiguity — the ESM6 trap
# --------------------------------------------------------------------------


def test_esm6_is_not_resolvable_without_an_explicit_decade() -> None:
    """The single most important property in this file.

    ``ESM6`` must not become "June 2026" because the machine's clock says 2026.
    There is no API that turns the digit into a year without the caller
    supplying the decade.
    """
    with pytest.raises(TypeError):
        resolve_abbreviated_contract_year("6")  # type: ignore[call-arg]


def test_the_same_abbreviated_code_yields_different_years_per_decade() -> None:
    assert resolve_abbreviated_contract_year("6", cycle_start=2020) == 2026
    assert resolve_abbreviated_contract_year("6", cycle_start=2010) == 2016
    assert resolve_abbreviated_contract_year("6", cycle_start=2000) == 2006
    assert resolve_abbreviated_contract_year("6", cycle_start=2030) == 2036


def test_two_digit_codes_resolve_against_a_century() -> None:
    assert resolve_abbreviated_contract_year("26", cycle_start=2000) == 2026
    assert resolve_abbreviated_contract_year("26", cycle_start=1900) == 1926
    assert resolve_abbreviated_contract_year("06", cycle_start=2000) == 2006


def test_every_single_digit_resolves_within_its_decade() -> None:
    for digit in range(10):
        assert resolve_abbreviated_contract_year(str(digit), cycle_start=2020) == 2020 + digit


def test_a_misaligned_cycle_start_is_rejected() -> None:
    # A window starting at 2015 would make "6" mean 2016 or 2021 depending on
    # which reading you take; refusing is the only unambiguous answer.
    for cycle_start in (2015, 2021, 1, 2026):
        with pytest.raises(ValueError):
            resolve_abbreviated_contract_year("6", cycle_start=cycle_start)
    for cycle_start in (2020, 2010, 1950):
        with pytest.raises(ValueError):
            resolve_abbreviated_contract_year("26", cycle_start=cycle_start)


def test_abbreviated_year_rejects_malformed_codes() -> None:
    for code in ("", "123", "1234", "a", "M6", "6a", " 6", "6 ", "+6", "-6", "٦", "６", "6\n"):
        with pytest.raises(ValueError):
            resolve_abbreviated_contract_year(code, cycle_start=2020)


def test_abbreviated_year_rejects_non_string_codes() -> None:
    for code in (6, None, True, 6.0, ["6"]):
        with pytest.raises(ValueError):
            resolve_abbreviated_contract_year(code, cycle_start=2020)  # type: ignore[arg-type]


def test_abbreviated_year_rejects_a_bool_cycle_start() -> None:
    # bool is a subclass of int; without an explicit guard True would be 1.
    with pytest.raises(ValueError):
        resolve_abbreviated_contract_year("6", cycle_start=True)


def test_abbreviated_year_rejects_a_non_int_cycle_start() -> None:
    for cycle_start in ("2020", 2020.0, None, [2020]):
        with pytest.raises(ValueError):
            resolve_abbreviated_contract_year("6", cycle_start=cycle_start)  # type: ignore[arg-type]


def test_abbreviated_year_windows_stay_inside_the_representable_range() -> None:
    for cycle_start in (0, 10, 990, 10000, -2020):
        with pytest.raises(ValueError):
            resolve_abbreviated_contract_year("6", cycle_start=cycle_start)

    assert resolve_abbreviated_contract_year("9", cycle_start=MIN_CONTRACT_YEAR) == 1009
    assert resolve_abbreviated_contract_year("9", cycle_start=9990) == MAX_CONTRACT_YEAR


def test_abbreviated_year_resolution_is_pure() -> None:
    assert len({resolve_abbreviated_contract_year("6", cycle_start=2020) for _ in range(100)}) == 1


# --------------------------------------------------------------------------
# F. Contract-year validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("year", [MIN_CONTRACT_YEAR, 1999, 2016, 2026, 2036, MAX_CONTRACT_YEAR])
def test_valid_full_years_are_accepted(year: int) -> None:
    assert contract(year=year).contract_year == year


@pytest.mark.parametrize(
    "year",
    [
        0,
        6,
        26,
        999,
        MIN_CONTRACT_YEAR - 1,
        MAX_CONTRACT_YEAR + 1,
        10000,
        -2026,
        10**30,
    ],
)
def test_out_of_range_years_are_rejected(year: int) -> None:
    with pytest.raises(Exception):  # noqa: B017
        contract(year=year)


@pytest.mark.parametrize("year", [True, False, 2026.0, "2026", None, [2026], b"2026"])
def test_non_int_years_are_rejected(year: Any) -> None:
    # bool first: it is an int subclass, so True would otherwise mean year 1.
    with pytest.raises(Exception):  # noqa: B017
        contract(year=year)


def test_a_decimal_looking_year_is_not_coerced() -> None:
    from decimal import Decimal

    with pytest.raises(Exception):  # noqa: B017
        contract(year=Decimal(2026))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# G. Product and venue validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("root", ["ES", "MES", "NQ", "6E", "M2K", "ZN", "ES1", "A", "ABCDEFGHIJKL"])
def test_well_formed_roots_are_accepted(root: str) -> None:
    assert FuturesProduct(venue=CME, root=root).root == root


@pytest.mark.parametrize(
    "root",
    [
        "",
        " ",
        "   ",
        "\t",
        "\n",
        " ES",
        "ES ",
        " ES ",
        "es",
        "Es",
        "eS",
        "ES1!",
        "ES-M6",
        "ES.M6",
        "ES_M6",
        "ES:M6",
        "ES/M6",
        "ES M6",
        "EЅ",  # Cyrillic capital Dze, visually 'S'
        "ＥＳ",  # full-width
        "Ε",  # Greek capital epsilon
        "123",  # purely numeric: a vendor instrument_id must never be a root
        "0",
        "999999",
        "ABCDEFGHIJKLM",  # one over the length limit
        "E" * 100,
        "ES\n",
        "ES\x00",
    ],
)
def test_malformed_roots_are_rejected(root: str) -> None:
    with pytest.raises(Exception):  # noqa: B017
        FuturesProduct(venue=CME, root=root)


@pytest.mark.parametrize("code", ["CME", "ICE", "CBOT", "XCME", "GLBX", "A", "X1A"])
def test_well_formed_venue_codes_are_accepted(code: str) -> None:
    assert Venue(code=code).code == code


@pytest.mark.parametrize(
    "code",
    ["", " ", "cme", "Cme", " CME", "CME ", "CME:", "C ME", "C-ME", "123", "ＣＭＥ", "C" * 17],
)
def test_malformed_venue_codes_are_rejected(code: str) -> None:
    with pytest.raises(Exception):  # noqa: B017
        Venue(code=code)


@pytest.mark.parametrize("value", [None, 123, True, b"ES", ["ES"], 4.5])
def test_non_string_roots_and_venues_are_rejected(value: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017
        FuturesProduct(venue=CME, root=value)
    with pytest.raises(Exception):  # noqa: B017
        Venue(code=value)


def test_nothing_is_silently_normalized() -> None:
    """No trimming, no case folding — the value in is the value out.

    Silent normalization is how ``" ES"`` and ``"ES"`` become one instrument
    without anyone deciding that they should.
    """
    for spelling in ("es", " ES", "ES ", "E S"):
        with pytest.raises(Exception):  # noqa: B017
            FuturesProduct(venue=CME, root=spelling)

    assert FuturesProduct(venue=CME, root="ES").root == "ES"


def test_extra_fields_are_rejected_rather_than_ignored() -> None:
    with pytest.raises(Exception):  # noqa: B017
        FuturesProduct(venue=CME, root="ES", multiplier=50)  # type: ignore[call-arg]
    with pytest.raises(Exception):  # noqa: B017
        Venue(code="CME", mic="XCME")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# Regression: model_copy must not forge an unvalidated identity (ADR-009, D2)
#
# Before the override, ``model_copy`` skipped validation entirely:
#
#     identity.model_copy(update={"contract_year": 6}).canonical()
#         -> 'CME:ES:M0006'          which FuturesContractId.parse rejects
#
# A serializer emitting a string its own parser refuses means a catalogue key
# written today cannot be read back tomorrow.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "update",
    [
        {"contract_year": 6},
        {"contract_year": 26},
        {"contract_year": 0},
        {"contract_year": True},  # bool leaked through as year 1
        {"contract_year": "2026"},
        {"contract_year": None},
        {"contract_month": "M"},  # a string, not the enum
        {"contract_month": "m"},
        {"product": "ES"},  # a bare root string
        {"product": None},
        {"tick_size": "0.25"},  # a specification field
        {"venue": "ICE"},  # a field this model does not have
    ],
)
def test_model_copy_cannot_forge_an_invalid_identity(update: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017
        contract().model_copy(update=update)


def test_model_copy_still_works_for_a_legitimate_change() -> None:
    rolled = contract(month=ContractMonth.JUNE).model_copy(
        update={"contract_month": ContractMonth.SEPTEMBER}
    )

    assert rolled.canonical() == "CME:ES:U2026"
    assert FuturesContractId.parse(rolled.canonical()) == rolled


def test_model_copy_without_an_update_is_an_equal_identity() -> None:
    identity = contract()

    assert identity.model_copy() == identity
    assert identity.model_copy(deep=True) == identity


@pytest.mark.parametrize("update", [{"root": "es"}, {"root": ""}, {"venue": "CME"}, {"root": 123}])
def test_model_copy_cannot_forge_an_invalid_product(update: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017
        FuturesProduct(venue=CME, root="ES").model_copy(update=update)


@pytest.mark.parametrize("update", [{"code": "cme"}, {"code": ""}, {"code": None}, {"mic": "XCME"}])
def test_model_copy_cannot_forge_an_invalid_venue(update: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017
        CME.model_copy(update=update)


def test_every_identity_reachable_through_the_public_api_round_trips() -> None:
    """The serializer must never emit a string the parser rejects.

    Stated as the property rather than as a list of cases: whatever a caller
    can build with the public API, ``parse(canonical(x)) == x``.
    """
    for update in ({}, {"contract_month": ContractMonth.MARCH}, {"contract_year": 2030}):
        identity = contract().model_copy(update=dict(update))

        assert FuturesContractId.parse(identity.canonical()) == identity


def test_json_round_trip_preserves_identity() -> None:
    identity = contract()

    assert FuturesContractId.model_validate_json(identity.model_dump_json()) == identity


def test_model_validate_applies_the_same_rules_as_construction() -> None:
    for payload in (
        {"product": {"venue": {"code": "cme"}, "root": "ES"}, "contract_month": "M"},
        {"product": {"venue": {"code": "CME"}, "root": "es"}, "contract_month": "M"},
    ):
        with pytest.raises(Exception):  # noqa: B017
            FuturesContractId.model_validate({**payload, "contract_year": 2026})


def test_a_str_subclass_is_stored_as_an_exact_str() -> None:
    """A subclass may override ``__eq__``; identity must not inherit that.

    The import layer had exactly this defect (ADR-009 D3), where a subclass with
    broken equality split one instrument in two. Here the guarantee comes from
    strict field validation normalizing the value, and this test pins it so a
    future config change cannot remove it silently.
    """

    class Unequal(str):
        def __eq__(self, other: object) -> bool:
            return False

        def __hash__(self) -> int:
            return id(self)

    product = FuturesProduct(venue=Venue(code=Unequal("CME")), root=Unequal("ES"))

    assert type(product.root) is str
    assert type(product.venue.code) is str
    assert product == FuturesProduct(venue=CME, root="ES")
    assert hash(product) == hash(FuturesProduct(venue=CME, root="ES"))


def test_no_identity_field_has_a_default() -> None:
    """No hidden default venue, product, month, or year.

    A defaulted component would let an identity be constructed that names less
    than a specific contract while looking complete.
    """
    for model in (Venue, FuturesProduct, FuturesContractId):
        for name, field in model.model_fields.items():
            assert field.is_required(), f"{model.__name__}.{name} has a default"


def test_specification_is_not_part_of_identity() -> None:
    """Identity has exactly three components and admits no fourth."""
    assert set(FuturesContractId.model_fields) == {"product", "contract_month", "contract_year"}
    assert set(FuturesProduct.model_fields) == {"venue", "root"}
    assert set(Venue.model_fields) == {"code"}


# --------------------------------------------------------------------------
# H. Continuous versus executable
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "ESM6",
        "ES1!",
        "ES",
        "CME:ES:M2026",  # even the canonical *string* is not the identity
        None,
        2026,
        ("CME", "ES", "M", 2026),
        {"product": "ES"},
    ],
)
def test_the_executable_guard_rejects_everything_that_is_not_a_contract(value: Any) -> None:
    with pytest.raises(TypeError):
        require_listed_contract(value)


def test_a_runtime_subclass_cannot_pass_the_executable_guard() -> None:
    """Regression: ADR-009 D4.

    ``@final`` is enforced by the type checker only. With ``isinstance``, a
    subclass declared at runtime passed the guard — and a continuous series
    bolted on as a subclass of the contract type is exactly the shape that
    mistake takes. The guard is now an exact type check.
    """

    class BackAdjustedContinuousSeries(FuturesContractId):  # type: ignore[misc]
        pass

    synthetic = BackAdjustedContinuousSeries(
        product=FuturesProduct(venue=CME, root="ES"),
        contract_month=ContractMonth.JUNE,
        contract_year=2026,
    )

    with pytest.raises(TypeError):
        require_listed_contract(synthetic)


def test_a_look_alike_object_cannot_pass_the_executable_guard() -> None:
    """Duck typing must not be enough — the guard is a type check on purpose."""

    class FakeContinuousSeries:
        product = FuturesProduct(venue=CME, root="ES")
        contract_month = ContractMonth.JUNE
        contract_year = 2026

        def canonical(self) -> str:
            return "CME:ES:M2026"

    with pytest.raises(TypeError):
        require_listed_contract(FakeContinuousSeries())


def test_a_continuous_alias_cannot_become_a_listed_contract_by_parsing() -> None:
    for alias in ("ES1!", "ES1", "ES!", "ESc1", "ES_CONT", "CME:ES1!:M2026"):
        with pytest.raises(ValueError):
            FuturesContractId.parse(alias)


def test_a_continuous_style_root_is_a_different_product_not_the_same_one() -> None:
    """``ES1`` is syntactically a valid root, and that is fine — it is *not* ES.

    The guarantee is not that a continuous-looking spelling is unwritable, but
    that it can never be *equal* to the product it is derived from.
    """
    assert FuturesProduct(venue=CME, root="ES1") != FuturesProduct(venue=CME, root="ES")
    assert contract(root="ES1") != contract(root="ES")


# --------------------------------------------------------------------------
# I. Determinism independent of ambient state
# --------------------------------------------------------------------------


def test_no_identity_module_reads_the_wall_clock_at_runtime() -> None:
    """Behavioural counterpart to the static architecture check.

    Freezing is not possible without a dependency, so this asserts the
    observable consequence instead: repeated construction and serialization of
    the same inputs produces one value, and no attribute of the result varies.
    """
    canonical_forms = {contract().canonical() for _ in range(1000)}
    years = {contract().contract_year for _ in range(1000)}

    assert canonical_forms == {"CME:ES:M2026"}
    assert years == {2026}


#: Emits the whole canonical universe plus a validator's error text. Run in a
#: child process under several ``PYTHONHASHSEED`` values; the output must be
#: byte-identical every time.
_DETERMINISM_SCRIPT = """
from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId, FuturesProduct, Venue,
)

ids = [
    FuturesContractId(
        product=FuturesProduct(venue=Venue(code=v), root=r),
        contract_month=m,
        contract_year=y,
    )
    for v in ("CME", "ICE")
    for r in ("ES", "MES", "NQ")
    for m in ContractMonth
    for y in (2016, 2026)
]
print("|".join(i.canonical() for i in ids))
print("|".join(sorted(i.canonical() for i in ids)))
try:
    ContractMonth.from_code("A")
except ValueError as exc:
    print(str(exc))
"""


def test_canonical_serialization_is_identical_across_hash_seeds() -> None:
    """Canonical form must not depend on ``PYTHONHASHSEED``.

    A manifest key written by one process has to be readable by the next, so
    the *serialized* form is the thing that must be stable. This asserts
    semantic and serialization determinism across processes — deliberately not
    that CPython's ``hash()`` integers match, which no contract promises and
    which hash randomisation makes false by design.

    Error messages are included: one built by iterating a set would vary
    between runs and make a diagnostic irreproducible.
    """
    import subprocess
    import sys

    outputs = set()
    for seed in ("0", "1", "42", "12345", "random"):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _DETERMINISM_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.add(completed.stdout)

    assert len(outputs) == 1
    assert "CME:ES:M2026" in outputs.pop()


def test_identity_is_not_influenced_by_the_process_timezone() -> None:
    import os
    import time

    original = os.environ.get("TZ")
    expected = contract().canonical()
    try:
        for zone in ("UTC", "America/Chicago", "Asia/Tokyo", "Pacific/Auckland"):
            os.environ["TZ"] = zone
            if hasattr(time, "tzset"):
                time.tzset()
            assert contract().canonical() == expected
            assert FuturesContractId.parse(expected) == contract()
            assert resolve_abbreviated_contract_year("6", cycle_start=2020) == 2026
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        if hasattr(time, "tzset"):
            time.tzset()
