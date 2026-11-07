from decimal import Decimal

import pytest

from factory.costs import estimate_cost


def priced(tariff, usage, *, operation="script"):
    return estimate_cost("example", "example-model", operation, usage,
                         {"example": {"example-model": {"script": tariff}}})


def test_token_cost_with_input_output_and_request_charge():
    result = priced({"input_per_million": "2", "output_per_million": "8", "base_per_request": ".01"},
                    {"input_tokens": 1500, "output_tokens": 500, "requests": 1})
    assert result == pytest.approx(0.017)


def test_media_cost_components_are_additive_and_accept_decimal_units():
    result = priced({"per_second": ".02", "per_image": ".1", "per_character": ".0001"},
                    {"seconds": 12.5, "images": 2, "characters": 100})
    assert result == pytest.approx(0.46)


@pytest.mark.parametrize("usage", [
    {"input_tokens": 100}, {"input_tokens": 100, "output_tokens": None}, {},
])
def test_missing_required_usage_remains_unknown(usage):
    assert priced({"input_per_million": 1, "output_per_million": 2}, usage) is None


def test_base_price_requires_known_number_of_requests():
    assert priced({"base_per_request": .5}, {}) is None
    assert priced({"base_per_request": .5}, {"requests": 3}) == 1.5


def test_no_tariff_no_operation_no_model_and_empty_tariff_are_unknown():
    assert estimate_cost("example", "model", "script", {"input_tokens": 10}, {}) is None
    assert priced({"per_second": 1}, {"seconds": 3}, operation="voice") is None
    assert estimate_cost("example", "other", "script", {}, {"example": {"model": {"script": {"per_second": 1}}}}) is None
    assert priced({}, {}) is None
    assert priced({"currency": "USD"}, {}) is None


def test_explicit_wildcard_operation_fallback_and_specific_override():
    rates = {"example": {"model": {"*": {"base_per_request": 1}, "voice": {"base_per_request": 2}}}}
    assert estimate_cost("example", "model", "research", {"requests": 1}, rates) == 1
    assert estimate_cost("example", "model", "voice", {"requests": 1}, rates) == 2
    rates["example"]["model"]["voice"] = {}
    assert estimate_cost("example", "model", "voice", {"requests": 1}, rates) is None


def test_explicit_zero_is_distinct_from_unknown():
    assert priced({"per_image": 0}, {"images": 2}) == 0
    assert priced({"per_image": 1}, {"images": 0}) == 0
    assert priced({"per_image": 0}, {}) is None


@pytest.mark.parametrize("invalid", [None, True, False, -1, "bad", float("nan"), float("inf"), "-Infinity", [], {}])
def test_invalid_rates_or_units_never_produce_false_cost(invalid):
    assert priced({"per_second": invalid}, {"seconds": 3}) is None
    assert priced({"per_second": 1}, {"seconds": invalid}) is None


def test_count_units_must_be_whole_numbers_but_seconds_may_be_fractional():
    assert priced({"per_image": 1}, {"images": 1.5}) is None
    assert priced({"input_per_million": 1}, {"input_tokens": 2.5}) is None
    assert priced({"per_second": 1}, {"seconds": .5}) == .5


def test_misspelled_tariff_field_and_non_usd_currency_are_unknown():
    assert priced({"input_per_milion": 1}, {"input_tokens": 100}) is None
    assert priced({"currency": "EUR", "per_image": 1}, {"images": 1}) is None


def test_extra_provider_usage_fields_do_not_change_price():
    assert priced({"per_image": Decimal(".2")}, {"images": 2, "raw": {"trace_id": "private"}, "provider": "example"}) == .4


def test_cost_overflow_and_underflow_do_not_become_free_or_infinite():
    assert priced({"per_second": "1e1000"}, {"seconds": 1}) is None
    assert priced({"per_second": "1e-1000"}, {"seconds": 1}) is None
