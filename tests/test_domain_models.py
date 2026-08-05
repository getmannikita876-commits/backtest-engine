from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import ValidationError

from quant_research_terminal.domain.models import Bar, Instrument, Quote, Trade, TradeSide

ONE_MINUTE = timedelta(minutes=1)


def test_instrument_is_immutable_and_uses_utc_datetimes() -> None:
    instrument = Instrument(
        symbol="ES",
        exchange="CME",
        asset_class="futures",
        currency="USD",
        created_at=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
    )

    assert instrument.symbol == "ES"
    with pytest.raises(ValidationError):
        instrument.symbol = "NQ"


def test_trade_validates_positive_price_and_quantity() -> None:
    trade = Trade(
        instrument_symbol="ES",
        timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        price=Decimal("5000.25"),
        size=Decimal("2"),
        side=TradeSide.BUY,
    )

    assert trade.price == Decimal("5000.25")
    assert trade.size == Decimal("2")

    with pytest.raises(ValidationError):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=Decimal("0"),
            size=Decimal("2"),
            side=TradeSide.BUY,
        )

    with pytest.raises(ValidationError):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=Decimal("5000.25"),
            size=Decimal("0"),
            side=TradeSide.BUY,
        )


def test_quote_requires_bid_to_be_less_than_or_equal_to_ask() -> None:
    quote = Quote(
        instrument_symbol="ES",
        timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        bid=Decimal("5000.00"),
        ask=Decimal("5000.25"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
    )

    assert quote.bid == Decimal("5000.00")
    assert quote.ask == Decimal("5000.25")

    with pytest.raises(ValidationError):
        Quote(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            bid=Decimal("5000.25"),
            ask=Decimal("5000.00"),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
        )


def test_rejects_float_inputs_for_decimal_fields() -> None:
    with pytest.raises(ValidationError):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=cast(Any, 5.25),
            size=Decimal("2"),
            side=TradeSide.BUY,
        )

    with pytest.raises(ValidationError):
        Quote(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            bid=cast(Any, 1.0),
            ask=cast(Any, 1.25),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
        )


def test_bar_validates_ohlc_and_volume() -> None:
    bar = Bar(
        instrument_symbol="ES",
        interval_start=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        interval=ONE_MINUTE,
        open=Decimal("4999.50"),
        high=Decimal("5002.00"),
        low=Decimal("4998.75"),
        close=Decimal("5001.25"),
        volume=Decimal("12345"),
    )

    assert bar.high >= bar.close

    with pytest.raises(ValidationError):
        Bar(
            instrument_symbol="ES",
            interval_start=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            interval=ONE_MINUTE,
            open=Decimal("5000.00"),
            high=Decimal("4999.00"),
            low=Decimal("4998.75"),
            close=Decimal("5001.25"),
            volume=Decimal("12345"),
        )

    with pytest.raises(ValidationError):
        Bar(
            instrument_symbol="ES",
            interval_start=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            interval=ONE_MINUTE,
            open=Decimal("5000.00"),
            high=Decimal("5002.00"),
            low=Decimal("4998.75"),
            close=Decimal("5001.25"),
            volume=Decimal("0"),
        )


def test_rejects_naive_datetime_values() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0),
            price=Decimal("5000.25"),
            size=Decimal("2"),
            side=TradeSide.BUY,
        )

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        Quote(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0),
            bid=Decimal("5000.00"),
            ask=Decimal("5000.25"),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
        )


def test_rejects_non_utc_timezones() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        Bar(
            instrument_symbol="ES",
            interval_start=datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=1))),
            interval=ONE_MINUTE,
            open=Decimal("4999.50"),
            high=Decimal("5002.00"),
            low=Decimal("4998.75"),
            close=Decimal("5001.25"),
            volume=Decimal("12345"),
        )
