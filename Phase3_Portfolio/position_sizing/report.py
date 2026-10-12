from .models import PositionSizeResult, PositionSizingRequest


def generate_sizing_report(request: PositionSizingRequest, result: PositionSizeResult) -> str:
    def fmt(v, dp=2):
        if v is None:
            return "—"
        try:
            return f"{v:.{dp}f}"
        except Exception:
            return str(v)

    lines = [
        "TRADEX POSITION SIZING REPORT",
        "=" * 40,
        f"Symbol:                     {request.symbol}",
        f"Trade:                      {request.trade_type.value}",
        f"Entry:                      ₹{fmt(request.entry_price)}",
        f"Stop:                       ₹{fmt(request.stop_loss)}",
        f"Expected Exit:              ₹{fmt(request.expected_exit_price)}",
        f"Expected Net Return:        {fmt(request.expected_net_return, 4)}",
        f"Probability of Profit:      {fmt(request.probability_of_profit, 2)}",
        f"Confidence:                 {fmt(request.model_confidence, 2)}",
        f"Volatility (ATR%):          {fmt(request.atr_pct, 4)}",
        "-" * 40,
        f"Risk / Share:               ₹{fmt(result.risk_per_share)}",
        f"Confidence Adjustment:      {fmt(result.confidence_adjustment, 3)}x",
        f"Volatility Adjustment:      {fmt(result.volatility_adjustment, 3)}x",
        f"Liquidity Adjustment:       {fmt(result.liquidity_adjustment, 3)}",
        f"Regime Adjustment:          {fmt(result.regime_adjustment, 3)}x",
        f"Portfolio Adjustment:       {fmt(result.portfolio_adjustment, 3)}",
        "-" * 40,
        f"Recommended Quantity:       {result.recommended_quantity}",
        f"Minimum Sensible Quantity:  {result.minimum_quantity}",
        f"Maximum Sensible Quantity:  {result.maximum_quantity}",
        f"Position Value:             ₹{fmt(result.recommended_position_value)}",
        f"Expected Net Profit:        ₹{fmt(result.expected_net_profit)}",
        f"Estimated Risk:             ₹{fmt(result.estimated_trade_risk)}",
        "-" * 40,
        f"Sizing Method:              {result.sizing_method.value}",
        f"Sizing Level:               {result.sizing_level.value}",
        f"Binding Constraint(s):      {', '.join(bc.value for bc in result.binding_constraints) or '—'}",
        f"Sizing Decision:            {result.sizing_status.value}",
    ]
    if result.rejection_reason:
        lines.append(f"Reason:                      {result.rejection_reason}")
    return "\n".join(lines)
