import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launch_stock_system.ps1"
VBS_LAUNCHER = ROOT / "open_stock_system.vbs"
TUNNEL_SETUP = ROOT / "setup_cloudflare_mobile.ps1"
TUNNEL_RUNNER = ROOT / "cloudflare" / "run_mobile_tunnel_hidden.vbs"


class DesktopLauncherTests(unittest.TestCase):
    def test_windows_powershell_can_parse_utf8_launcher(self):
        self.assertTrue(
            LAUNCHER.read_bytes().startswith(b"\xef\xbb\xbf"),
            "Windows PowerShell 5.1 needs a BOM to parse this UTF-8 script reliably",
        )
        escaped_path = str(LAUNCHER).replace("'", "''")
        command = (
            "$tokens=$null; $errors=$null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped_path}',"
            "[ref]$tokens,[ref]$errors); "
            "if($errors.Count -gt 0){"
            "$errors | ForEach-Object { Write-Error $_.Message }; exit 1}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_launcher_opens_http_url_instead_of_local_html(self):
        script = LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertIn('$Url = "http://127.0.0.1:$Port/"', script)
        self.assertIn("Start-Process -FilePath $chrome", script)
        self.assertNotIn("Start-Process $LoaderPath", script)

    def test_hidden_vbs_wrapper_invokes_powershell_launcher(self):
        script = VBS_LAUNCHER.read_text(encoding="ascii")
        self.assertIn("powershell.exe -NoProfile -ExecutionPolicy Bypass -File", script)
        self.assertIn('root & "\\launch_stock_system.ps1"', script)
        self.assertIn("shell.Run command, 0, False", script)

    def test_cloudflare_task_uses_hidden_waiting_vbs_runner(self):
        setup = TUNNEL_SETUP.read_text(encoding="utf-8")
        runner = TUNNEL_RUNNER.read_text(encoding="ascii")

        self.assertIn('"System32\\wscript.exe"', setup)
        self.assertIn('//B //NoLogo', setup)
        self.assertIn('run_mobile_tunnel_hidden.vbs', setup)
        self.assertNotIn('-Execute $Cloudflared', setup)
        self.assertIn('shell.Run(command, 0, True)', runner)
        self.assertIn('--logfile', runner)
        self.assertIn('50f56c4a-1e9c-4c94-81f7-df7faef48508', runner)


if __name__ == "__main__":
    unittest.main()
