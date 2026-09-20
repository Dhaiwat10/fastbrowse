import pytest

from fastbrowse.models import CostBasis, CostComponent, CostLine, Limits
from fastbrowse.telemetry import BudgetExceeded, Ledger


@pytest.mark.parametrize(
    ("limit", "shown"), [(0.00001, "0.00001"), (0.25, "0.25"), (1.0, "1.0"), (0.5, "0.5"), (12.5, "12.5")]
)
def test_the_spend_limit_is_printed_as_a_currency_amount(limit: float, shown: str) -> None:
    ledger = Ledger(Limits(max_dollars=limit))
    with pytest.raises(BudgetExceeded) as error:
        ledger.record(CostLine(component=CostComponent.LLM, basis=CostBasis.METERED, dollars=limit + 1))
    assert str(error.value) == f"spend limit ${shown} reached"
