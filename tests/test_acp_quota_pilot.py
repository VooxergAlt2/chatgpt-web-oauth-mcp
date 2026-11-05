"""Independent contract tests for the offline ACP quota pilot.

Keep this suite self-contained: it imports only the public function and pytest,
so it can be copied beside private candidate implementations.
"""
import math
from copy import deepcopy

import pytest

from scripts.acp_quota_pilot import quota_delta


def observation(**overrides):
    data = {
        "account": "acct-1",
        "group": "premium",
        "window": "weekly",
        "resets_at": "2030-01-02T00:00:00Z",
        "sampled_at": 1_893_456_000.0,
        "remaining_percent": 80.0,
    }
    data.update(overrides)
    return data


def pair(before=None, after=None, *, concurrent=False):
    return quota_delta(
        observation(**(before or {})),
        observation(**({"sampled_at": 1_893_456_001.0,
                        "remaining_percent": 70.0} | (after or {}))),
        concurrent=concurrent,
    )


@pytest.mark.parametrize(
    ("before_pct", "after_pct", "expected"),
    [
        (100, 99, (1.0, "observed")),
        (80, 70, (10.0, "observed")),
        (0.5, 0.1, (0.4, "observed")),
        (55.5556, 55.5, (0.056, "observed")),
        (99.999, 0, (99.999, "observed")),
        (80, 80, (None, "below_resolution")),
        (0, 0, (None, "below_resolution")),
        (12.3456, 12.3456, (None, "below_resolution")),
        (60, 60.001, (None, "increased")),
        (10, 11, (None, "increased")),
        (0, 100, (None, "increased")),
    ],
)
def test_percentage_delta_and_direction(before_pct, after_pct, expected):
    assert pair({"remaining_percent": before_pct},
                {"remaining_percent": after_pct}) == expected


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("account", None), ("account", ""), ("account", "  "),
        ("account", 7), ("account", []),
        ("group", None), ("group", ""), ("group", "\t"),
        ("group", False), ("group", {}),
        ("window", None), ("window", ""), ("window", "\n"),
        ("window", 1), ("window", ()),
    ],
)
def test_invalid_identity_fields_return_identity(key, bad_value):
    assert pair({key: bad_value}) == (None, "identity")
    assert pair({}, {key: bad_value}) == (None, "identity")


@pytest.mark.parametrize("key", ["account", "group", "window"])
def test_different_identity_returns_identity(key):
    assert pair({}, {key: "other"}) == (None, "identity")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"account": ""}, {"resets_at": "not-a-date", "sampled_at": 1}),
        ({}, {"group": "other", "sampled_at": 1}),
        ({"window": None}, {"remaining_percent": -1}),
        ({}, {"account": "other", "resets_at": ""}),
    ],
)
def test_identity_has_priority_over_stale_reset_and_availability(before, after):
    assert pair(before, after) == (None, "identity")


def test_concurrent_has_highest_priority_even_for_malformed_inputs():
    assert quota_delta(None, object(), concurrent=True) == (None, "concurrent")


@pytest.mark.parametrize(
    ("before_time", "after_time"),
    [
        (100, 100), (101, 100), (100, 99),
        (True, 102), (100, False), (None, 102),
        ("100", 102), (100, "102"),
        (math.nan, 102), (100, math.inf),
        (-math.inf, 102), (100, -math.inf),
    ],
)
def test_invalid_or_nonincreasing_sample_times_are_stale(before_time, after_time):
    assert pair({"sampled_at": before_time}, {"sampled_at": after_time}) == (
        None, "stale"
    )


@pytest.mark.parametrize(
    ("before_time", "after_time"),
    [
        (1_893_456_000, 1_893_456_001),
        (1_893_455_998.25, 1_893_455_999.5),
        (1_893_455_998, 1_893_455_999),
    ],
)
def test_finite_numeric_unix_seconds_are_accepted(before_time, after_time):
    assert quota_delta(
        observation(sampled_at=before_time),
        observation(sampled_at=after_time, remaining_percent=79),
    ) == (1.0, "observed")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("2030-01-01T02:00:00+02:00", "2030-01-01T00:00:00Z"),
        ("2029-12-31T19:00:00-05:00", "2030-01-01T00:00:00Z"),
    ],
)
def test_reset_must_match_as_exact_string_across_snapshots(left, right):
    assert quota_delta(
        observation(resets_at=left, sampled_at=1_893_455_998),
        observation(resets_at=right, sampled_at=1_893_455_999,
                    remaining_percent=75),
    ) == (None, "reset")


def test_equivalent_iso_spellings_are_valid_within_each_snapshot():
    for reset in ("2030-01-01T00:00:00Z", "2030-01-01T02:00:00+02:00"):
        assert quota_delta(
            observation(resets_at=reset, sampled_at=1_893_455_998),
            observation(resets_at=reset, sampled_at=1_893_455_999,
                        remaining_percent=75),
        ) == (5.0, "observed")


@pytest.mark.parametrize(
    ("before_reset", "after_reset"),
    [
        ("2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z"),
        ("2030-01-01T00:00:00Z", "2030-01-01T01:00:00Z"),
        ("", "2030-01-01T00:00:00Z"),
        (None, "2030-01-01T00:00:00Z"),
        ("2030-01-01T00:00:00Z", None),
        ("2030-01-01T00:00:00Z", ""),
        ("bad", "bad"),
        ("2030-01-01", "2030-01-01"),
        (123, 123),
    ],
)
def test_missing_invalid_or_different_reset_returns_reset(before_reset, after_reset):
    assert pair({"resets_at": before_reset}, {"resets_at": after_reset}) == (
        None, "reset"
    )


@pytest.mark.parametrize(
    ("before_time", "after_time"),
    [
        (1_893_455_999, 1_893_456_000),
        (1_893_455_999, 1_893_456_001),
    ],
)
def test_reset_at_or_between_observations_returns_reset(before_time, after_time):
    reset = "2030-01-01T00:00:00Z"
    assert quota_delta(
        observation(resets_at=reset, sampled_at=before_time),
        observation(resets_at=reset, sampled_at=after_time,
                    remaining_percent=70),
    ) == (None, "reset")


@pytest.mark.parametrize(
    ("before_value", "after_value"),
    [
        (None, 10), (10, None),
        (True, 10), (10, False),
        ("10", 9), (10, "9"),
        (math.nan, 10), (10, math.nan),
        (math.inf, 10), (10, -math.inf),
        (-math.inf, 10), (10, math.inf),
        (-0.001, 10), (10, -0.001),
        (100.001, 10), (10, 100.001),
        (10**1000, 10), (10, 10**1000),
    ],
)
def test_missing_boolean_nonfinite_or_out_of_range_percent_is_unavailable(
    before_value, after_value
):
    assert pair({"remaining_percent": before_value},
                {"remaining_percent": after_value}) == (None, "unavailable")


@pytest.mark.parametrize("missing_key", ["remaining_percent"])
def test_absent_percentage_is_unavailable(missing_key):
    before = observation()
    after = observation(sampled_at=1_893_456_001, remaining_percent=70)
    del before[missing_key]
    assert quota_delta(before, after) == (None, "unavailable")


def test_identity_and_stale_precede_reset_and_percentage_validation():
    assert pair(
        {"account": "acct-1", "sampled_at": 1},
        {"account": "different", "sampled_at": 1, "resets_at": "bad",
         "remaining_percent": True},
    ) == (None, "identity")
    assert pair(
        {"sampled_at": 10, "resets_at": "bad", "remaining_percent": True},
        {"sampled_at": 10, "resets_at": "bad", "remaining_percent": True},
    ) == (None, "stale")
    assert pair(
        {"resets_at": "bad", "remaining_percent": True},
        {"resets_at": "bad", "remaining_percent": True},
    ) == (None, "reset")


def test_inputs_are_not_mutated():
    before = observation(remaining_percent=90)
    after = observation(sampled_at=1_893_456_001, remaining_percent=83)
    before_copy, after_copy = deepcopy(before), deepcopy(after)
    assert quota_delta(before, after) == (7.0, "observed")
    assert before == before_copy
    assert after == after_copy

@pytest.mark.parametrize(("first", "last", "expected"), [
    (-(10**400), 0, (10.0, "observed")),
    (0, 10**400, (None, "reset")),
])
def test_arbitrary_precision_integer_times(first, last, expected):
    assert pair({"sampled_at": first}, {"sampled_at": last}) == expected
