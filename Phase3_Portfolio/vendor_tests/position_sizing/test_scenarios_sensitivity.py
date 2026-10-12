from decimal import Decimal

from position_sizing.config import DEFAULT_CONFIG
from position_sizing.engine import calculate_position_size
from position_sizing.enums import ScenarioType
from position_sizing.report import generate_sizing_report
from position_sizing.scenarios import run_all_scenarios
from position_sizing.sensitivity import position_size_frontier, stop_loss_sensitivity

from .conftest import make_request


def test_scenario_ordering_base_ge_conservative_ge_stress():
    req = make_request()
    scenarios = run_all_scenarios(req, DEFAULT_CONFIG)
    base_q = scenarios["BASE"]["quantity_raw"]
    cons_q = scenarios["CONSERVATIVE"]["quantity_raw"]
    stress_q = scenarios["STRESS"]["quantity_raw"]
    assert base_q >= cons_q >= stress_q


def test_stop_sensitivity_wider_stop_smaller_size():
    req = make_request()
    results = stop_loss_sensitivity(req, [Decimal("1"), Decimal("3"), Decimal("5")], DEFAULT_CONFIG)
    sizes = [r["base_size_raw"] for r in results if "base_size_raw" in r]
    assert sizes == sorted(sizes, reverse=True)


def test_frontier_monotonic_position_value():
    req = make_request()
    frontier = position_size_frontier(req, 5, DEFAULT_CONFIG)
    values = [row["position_value"] for row in frontier]
    assert values == sorted(values)


def test_report_generation_is_readable_string():
    req = make_request()
    result = calculate_position_size(req)
    report = generate_sizing_report(req, result)
    assert "TRADEX POSITION SIZING REPORT" in report
    assert req.symbol in report
