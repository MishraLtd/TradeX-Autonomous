from risk_model import features, inference


def test_predict_risk_end_to_end(ohlcv):
    feat = features.build_feature_frame(ohlcv)
    row = feat.dropna(subset=features.FEATURE_COLUMNS).iloc[-1].to_dict()

    pred = inference.predict_risk(
        symbol="RELIANCE",
        trade_type="DELIVERY",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
        horizon_bars=5,
        feature_row=row,
    )

    assert 0 <= pred.probability_of_loss <= 1
    assert 0 <= pred.probability_of_stop_hit <= 1
    assert pred.mae_p90_pct <= pred.mae_p50_pct  # worse tail more negative
    assert 0 <= pred.risk_score <= 100
    assert pred.model_version.startswith("risk_model_v")
