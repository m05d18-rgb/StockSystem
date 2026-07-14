import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MobileDefaultModuleTests(unittest.TestCase):
    def test_initial_markup_shows_holdings_and_hides_radar(self):
        html = (ROOT / "mobile-remote.html").read_text(encoding="utf-8")
        radar_tab = re.search(r'<button id="radarTab"[^>]*>', html).group(0)
        holdings_tab = re.search(r'<button id="holdingsTab"[^>]*>', html).group(0)
        radar_panel = re.search(r'<section id="radarPanel"[^>]*>', html).group(0)
        holdings_panel = re.search(r'<section id="holdingsPanel"[^>]*>', html).group(0)

        self.assertNotIn("module-tab active", radar_tab)
        self.assertIn('aria-selected="false"', radar_tab)
        self.assertIn("module-tab active", holdings_tab)
        self.assertIn('aria-selected="true"', holdings_tab)
        self.assertIn(" hidden", radar_panel)
        self.assertNotIn(" hidden", holdings_panel)

    def test_missing_or_unknown_hash_defaults_to_holdings(self):
        script = (ROOT / "mobile-remote.js").read_text(encoding="utf-8")
        self.assertIn('return location.hash === "#radar" ? "radar" : "holdings";', script)
        self.assertIn('activateModule("holdings");', script)
        self.assertNotIn("activateModule(moduleFromHash(), false);", script)

    def test_home_screen_start_url_opens_holdings(self):
        manifest = json.loads((ROOT / "site.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["start_url"], "/mobile-remote.html#holdings")


if __name__ == "__main__":
    unittest.main()
