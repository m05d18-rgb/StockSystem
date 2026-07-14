from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_radar_defaults_to_short_opportunities_with_surge_filter():
    source = (ROOT / "app.js").read_text(encoding="utf-8")

    assert 'safeGetItem(RADAR_VIEW_MODE_STORAGE_KEY, "opportunity")' in source
    assert 'data-radar-view-mode="opportunity"' in source
    assert 'data-radar-view-mode="surge"' in source
    assert 'viewMode === "surge" ? isShortMonsterCandidate : isShortOpportunityCandidate' in source
    assert 'const radarViewMode = isMobileRadarView ? "surge" : readRadarViewMode();' in source


def test_short_opportunities_exclude_overheated_and_risk_vetoed_candidates():
    source = (ROOT / "app.js").read_text(encoding="utf-8")
    helper = source.split("function isShortOpportunityCandidate", 1)[1].split(
        "function scanSummaryCards", 1
    )[0]

    assert "isStrengtheningCandidate(item)" in helper
    assert "!Boolean(item.overheated)" in helper
    assert "!Boolean(item.risk_vetoed ?? item.riskVetoed)" in helper


def test_visible_app_copy_uses_short_opportunity_direction():
    html = (ROOT / "index.html").read_text(encoding="utf-8")

    assert "短線機會雷達" in html
    assert "手動掃描短線機會" in html
    assert '<div class="brand-mark">9.9.319</div>' in html
    assert '<script src="./app.js?v=9.9.319"></script>' in html
