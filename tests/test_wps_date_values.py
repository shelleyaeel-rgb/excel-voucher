from decimal import Decimal, ROUND_DOWN
import math

import pytest

from xiaoyu.wps_service import target_value_matches


REQUESTED = 46332.576580243054
STORED = float(Decimal(str(REQUESTED)).quantize(Decimal('0.0000000001'), rounding=ROUND_DOWN))


def test_date_accepts_only_exact_wps_serial_truncation_with_same_millisecond():
    assert target_value_matches(STORED, REQUESTED, 'yyyy-mm-dd hh:mm:ss.000')
    assert target_value_matches(REQUESTED, REQUESTED, 'yyyy-mm-dd')
    assert not target_value_matches(math.nextafter(STORED, math.inf), REQUESTED, 'yyyy-mm-dd')
    assert not target_value_matches(STORED + 1 / 86400000, REQUESTED, 'yyyy-mm-dd')


@pytest.mark.parametrize('fmt', ['General', '0.00', '#,##0.00', '@', 'General"月"', '0"yyyy-mm-dd"'])
def test_storage_precision_rule_never_applies_to_money_or_plain_numbers(fmt):
    assert not target_value_matches(STORED, REQUESTED, fmt)


@pytest.mark.parametrize('value', [str(STORED), True, None, float('inf'), float('nan')])
def test_date_precision_rule_does_not_coerce_types(value):
    assert not target_value_matches(value, REQUESTED, 'yyyy-mm-dd')


def test_truncation_that_changes_millisecond_is_not_accepted():
    requested = 2958464.123456789
    stored = float(Decimal(str(requested)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN))
    assert not target_value_matches(stored, requested, 'yyyy-mm-dd hh:mm:ss.000')
