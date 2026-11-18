"""バージョン番号が、更新する3か所（modules/__init__.py・pyproject.toml・CHANGELOG.md）でそろっているか。"""

import re
import subprocess
import sys

from conftest import ROOT
from modules import __version__


def test_version_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pyproject_version_matches():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == __version__


def test_changelog_has_released_section_for_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [未リリース]" in text
    assert re.search(rf"^## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M), \
        f"CHANGELOG.md に「## [{__version__}] - 日付」の見出しがありません"
    assert f"[{__version__}]: " in text, "CHANGELOG.md の末尾に、この版のリンクがありません"


def test_version_option():
    result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--version"],
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0
    assert result.stdout.strip().endswith(__version__)


def test_bundled_fonts_have_license():
    """同梱フォントは SIL Open Font License で、配布するときはライセンス文を同梱する必要がある。"""
    text = (ROOT / "fonts" / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in text
    for font in (ROOT / "fonts").glob("*.ttf"):
        family = font.stem.split("-")[0]
        assert font.name in text or family in text.replace(" ", ""), f"{font.name} の著作権表示が OFL.txt にありません"


def test_license_file_is_plain_mit():
    """GitHub が MIT と自動判定できるよう、LICENSE は MIT の原文だけにする。"""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n")
    assert text.rstrip().endswith("SOFTWARE.")
