import subprocess
from pathlib import Path


def test_account_page_javascript():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/profile_page_js.cjs'), str(root / 'static/account.html')],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
