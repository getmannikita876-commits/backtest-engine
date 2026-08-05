"""Regression tests for provider resource ownership and iterator safety.

A streaming provider holds an operating-system resource for as long as the
caller holds its iterator. These tests pin down when that handle is opened and
when it is closed, so a future change cannot silently start leaking handles or
start reading eagerly.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

import pytest

from quant_research_terminal.data_import import (
    CsvMarketDataProvider,
    ImportRecordType,
    MarketDataProvider,
    ProviderDecodeError,
    ProviderNotConfiguredError,
    ProviderRequest,
    RecordStream,
    ThetaDataMarketDataProvider,
)

TRADE_HEADER = "timestamp,instrument_symbol,price,size,side"


class _HandleSpy:
    """Records read handles opened through :meth:`Path.open`.

    Only read handles are tracked. The tests write their fixture files through
    the same call, and those writes are not what is under test here.
    """

    def __init__(self) -> None:
        self.handles: list[IO[Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open = Path.open
        spy = self

        def tracked_open(self: Path, *args: Any, **kwargs: Any) -> IO[Any]:
            handle = cast(IO[Any], real_open(self, *args, **kwargs))
            if getattr(handle, "mode", "").startswith("r"):
                spy.handles.append(handle)
            return handle

        monkeypatch.setattr(Path, "open", tracked_open)

    @property
    def all_closed(self) -> bool:
        return all(handle.closed for handle in self.handles)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _HandleSpy:
    tracker = _HandleSpy()
    tracker.install(monkeypatch)
    return tracker


def _csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "trades.csv"
    path.write_text("\n".join((TRADE_HEADER, *rows)) + "\n", encoding="utf-8")
    return path


def _provider(path: Path) -> CsvMarketDataProvider:
    return CsvMarketDataProvider(path=path, record_type=ImportRecordType.TRADE)


def _request() -> ProviderRequest:
    return ProviderRequest(record_type=ImportRecordType.TRADE, instrument_symbol="ES")


def _rows(count: int) -> list[str]:
    return [f"2024-01-02T12:00:{index:02d}+00:00,ES,5000.25,2,buy" for index in range(count)]


# --------------------------------------------------------------------------
# Laziness
# --------------------------------------------------------------------------


def test_fetch_does_not_open_the_file(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(3)))

    iterator = provider.fetch(_request())

    # Nothing is read until the caller asks for the first record.
    assert spy.handles == []
    iterator.close()


def test_rejected_request_never_opens_a_handle(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(1)))
    request = ProviderRequest(record_type=ImportRecordType.QUOTE, instrument_symbol="ES")

    with pytest.raises(Exception, match="does not serve"):
        provider.fetch(request)

    assert spy.handles == []


# --------------------------------------------------------------------------
# Deterministic cleanup
# --------------------------------------------------------------------------


def test_full_iteration_closes_the_handle(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(3)))

    records = list(provider.fetch(_request()))

    assert len(records) == 3
    assert len(spy.handles) == 1
    assert spy.all_closed


def test_closing_early_closes_the_handle(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(10)))
    iterator = provider.fetch(_request())

    next(iterator)
    assert not spy.all_closed

    iterator.close()

    assert spy.all_closed


def test_contextlib_closing_releases_the_handle(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(10)))

    with closing(provider.fetch(_request())) as records:
        next(records)

    assert spy.all_closed


def test_breaking_out_of_a_loop_then_closing_releases_the_handle(
    tmp_path: Path, spy: _HandleSpy
) -> None:
    provider = _provider(_csv(tmp_path, *_rows(10)))
    iterator = provider.fetch(_request())

    for _record in iterator:
        break

    iterator.close()

    assert spy.all_closed


def test_decode_error_closes_the_handle(tmp_path: Path, spy: _HandleSpy) -> None:
    # A structural defect propagates out of the generator; the handle must not
    # survive the exception.
    path = _csv(tmp_path, "2024-01-02T12:00:00+00:00,ES,5000.25")
    provider = _provider(path)

    with pytest.raises(ProviderDecodeError):
        list(provider.fetch(_request()))

    assert spy.all_closed


def test_exception_raised_by_the_consumer_closes_the_handle(
    tmp_path: Path, spy: _HandleSpy
) -> None:
    provider = _provider(_csv(tmp_path, *_rows(10)))

    class _ConsumerError(RuntimeError):
        pass

    with pytest.raises(_ConsumerError):
        with closing(provider.fetch(_request())) as records:
            for _record in records:
                raise _ConsumerError

    assert spy.all_closed


# --------------------------------------------------------------------------
# Iterator safety
# --------------------------------------------------------------------------


def test_each_fetch_returns_an_independent_iterator(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(3)))

    first = provider.fetch(_request())
    second = provider.fetch(_request())

    # Advancing one must not move the other's position in the file.
    assert next(first).source_index == 0
    assert next(second).source_index == 0
    assert next(first).source_index == 1

    first.close()
    second.close()

    assert len(spy.handles) == 2
    assert spy.all_closed


def test_a_closed_iterator_yields_nothing_further(tmp_path: Path) -> None:
    provider = _provider(_csv(tmp_path, *_rows(5)))
    iterator = provider.fetch(_request())

    next(iterator)
    iterator.close()

    with pytest.raises(StopIteration):
        next(iterator)


def test_closing_twice_is_harmless(tmp_path: Path, spy: _HandleSpy) -> None:
    provider = _provider(_csv(tmp_path, *_rows(5)))
    iterator = provider.fetch(_request())

    next(iterator)
    iterator.close()
    iterator.close()

    assert spy.all_closed


# --------------------------------------------------------------------------
# Buffering / newline handling
# --------------------------------------------------------------------------


def test_quoted_field_containing_a_newline_is_read_as_one_row(tmp_path: Path) -> None:
    # The classic newline surprise: the file must be opened with newline=""
    # so the csv module, not the text layer, decides where a row ends.
    path = tmp_path / "trades.csv"
    # newline="" on the write too, so the embedded newline reaches the file as
    # a bare \n rather than being translated to \r\n by the text layer.
    path.write_text(
        f'{TRADE_HEADER}\n2024-01-02T12:00:00+00:00,ES,5000.25,2,"buy\nfilled"\n',
        encoding="utf-8",
        newline="",
    )

    records = list(_provider(path).fetch(_request()))

    assert len(records) == 1
    assert records[0].value("side") == "buy\nfilled"


# --------------------------------------------------------------------------
# The closeable stream contract, exercised through the provider interface
# --------------------------------------------------------------------------


def test_fetch_returns_a_record_stream_through_the_interface(tmp_path: Path) -> None:
    # Typed as the interface, not the concrete class: the guarantee must come
    # from MarketDataProvider itself.
    provider: MarketDataProvider = _provider(_csv(tmp_path, *_rows(3)))

    stream = provider.fetch(_request())

    assert isinstance(stream, RecordStream)
    stream.close()


def test_stream_is_usable_as_a_context_manager(tmp_path: Path, spy: _HandleSpy) -> None:
    provider: MarketDataProvider = _provider(_csv(tmp_path, *_rows(10)))

    with provider.fetch(_request()) as stream:
        next(iter(stream))
        assert not spy.all_closed

    assert spy.all_closed


def test_context_manager_closes_even_when_the_body_raises(tmp_path: Path, spy: _HandleSpy) -> None:
    provider: MarketDataProvider = _provider(_csv(tmp_path, *_rows(10)))

    class _ConsumerError(RuntimeError):
        pass

    with pytest.raises(_ConsumerError):
        with provider.fetch(_request()) as stream:
            for _record in stream:
                raise _ConsumerError

    assert spy.all_closed


def test_stream_reports_its_closed_state(tmp_path: Path) -> None:
    provider: MarketDataProvider = _provider(_csv(tmp_path, *_rows(3)))
    stream = provider.fetch(_request())

    assert stream.closed is False
    stream.close()
    assert stream.closed is True


def test_exhausting_a_stream_marks_it_closed(tmp_path: Path, spy: _HandleSpy) -> None:
    provider: MarketDataProvider = _provider(_csv(tmp_path, *_rows(2)))
    stream = provider.fetch(_request())

    records = list(stream)

    assert len(records) == 2
    assert stream.closed is True
    assert spy.all_closed


def test_stream_closed_before_iteration_opens_no_handle(tmp_path: Path, spy: _HandleSpy) -> None:
    provider: MarketDataProvider = _provider(_csv(tmp_path, *_rows(3)))

    stream = provider.fetch(_request())
    stream.close()

    assert spy.handles == []
    assert list(stream) == []


def test_stub_providers_raise_before_returning_a_stream() -> None:
    # The stream contract must not paper over an unimplemented vendor by
    # handing back an empty, already-closed stream.
    provider: MarketDataProvider = ThetaDataMarketDataProvider()

    with pytest.raises(ProviderNotConfiguredError):
        provider.fetch(_request())


def test_crlf_line_endings_do_not_leak_into_values(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_bytes(f"{TRADE_HEADER}\r\n2024-01-02T12:00:00+00:00,ES,5000.25,2,buy\r\n".encode())

    records = list(_provider(path).fetch(_request()))

    assert len(records) == 1
    assert records[0].value("side") == "buy"
    assert records[0].value("timestamp") == datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
