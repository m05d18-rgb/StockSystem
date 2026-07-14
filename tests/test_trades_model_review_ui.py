from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_trade_review_only_shows_model_and_radar_accuracy():
    html = (ROOT / "trades.html").read_text(encoding="utf-8")

    assert 'id="modelAccuracySummary"' in html
    assert 'id="modelAccuracyReview"' in html
    assert "模型準確率" in html
    assert 'id="radarDiscoveryAccuracySummary"' in html
    assert 'id="radarDiscoveryAccuracyReview"' in html
    assert "妖股雷達驗證" in html
    assert 'id="realizedRadarReview"' not in html
    assert 'id="modelPaperSection"' not in html
    assert 'id="radarScoreRecordSection"' not in html
    assert 'id="reviewEvidenceSection"' not in html
    assert 'id="tcnExperimentSection"' not in html
    assert 'id="localLedgerSection"' not in html


def test_model_accuracy_uses_same_buy_target_for_all_models():
    script = (ROOT / "trades.js").read_text(encoding="utf-8")
    renderer = script.split("async function renderModelAccuracyReview", 1)[1]
    renderer = renderer.split("async function renderRadarDiscoveryAccuracy", 1)[0]

    assert "/api/model-experiments/tcn/status" in renderer
    assert "dailyTop5Precision" in renderer
    assert "dailyTop5Trades" in renderer
    assert '["模型", "已結算樣本", "準確率"]' in renderer
    assert "10 日內先漲 +10%" in renderer
    assert "先跌 -7% 算失敗" in renderer


def test_trade_review_only_loads_accuracy_on_startup():
    script = (ROOT / "trades.js").read_text(encoding="utf-8")
    startup = script.rsplit("applyTheme();", 1)[1]

    assert "renderModelAccuracyReview();" in startup
    assert "renderRadarDiscoveryAccuracy();" in startup
    assert "renderModelPaperReview" not in startup
    assert "renderRealizedReview" not in startup
    assert "bindReviewDisclosures" not in startup


def test_radar_accuracy_uses_real_close_settlement_endpoint():
    script = (ROOT / "trades.js").read_text(encoding="utf-8")
    renderer = script.split("async function renderRadarDiscoveryAccuracy", 1)[1]
    renderer = renderer.split("function renderModelTestOverview", 1)[0]

    assert "/api/radar/discovery-recall?days=30&refresh=1" in renderer
    assert "actualMovers" in renderer
    assert "detectedMovers" in renderer
    assert "missedMovers" in renderer
    assert "earlyRecall" in renderer
    assert "newIntradayCandidates" in renderer
    assert "targetHitRate" in renderer
    assert "avgMaxAdverse" in renderer
    assert "avgNetReturn" in renderer


def test_trade_review_asset_version_busts_old_browser_cache():
    html = (ROOT / "trades.html").read_text(encoding="utf-8")

    assert '<div class="brand-mark">9.9.316</div>' in html
    assert '<link rel="stylesheet" href="./styles.css?v=9.9.316" />' in html
    assert '<script src="./trades.js?v=9.9.316"></script>' in html
