"""
Shared pytest fixtures. Plain factory helpers (make_portfolio_state,
make_sizing_request, etc.) live in tests/factories.py instead of here,
specifically so other test modules can `from factories import ...`
without going through a `tests.*` package path — this repo's `tests/`
directory has no `__init__.py`, and both position_sizing_pkg and this
package happen to name their test directory `tests`, which collide as
import targets once both are on PYTHONPATH for integration testing.
"""
from cost_model import models as cost_models
from cost_model.engine import CostEngine

import pytest

from capital_feasibility.config import CapitalFeasibilityConfig


@pytest.fixture
def cost_engine():
    return CostEngine()


@pytest.fixture
def cost_models_module():
    return cost_models


@pytest.fixture
def config():
    return CapitalFeasibilityConfig()
