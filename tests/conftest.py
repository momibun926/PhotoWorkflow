"""テスト共通の準備。

exiftool を使うテストには @requires_exiftool を付ける。exiftool が見つからない環境では自動でスキップする。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXIFTOOL = shutil.which("exiftool") or shutil.which("exiftool.exe")
requires_exiftool = pytest.mark.skipif(EXIFTOOL is None, reason="exiftool が見つからない")


@pytest.fixture
def make_config(tmp_path):
    """tmp_path に config.json を作って ConfigManager を返す。extra でキーを追加・上書きできる。"""
    from modules.config_manager import ConfigManager

    def _make(**extra):
        data = {
            "directories": {
                "base": str(tmp_path / "base"),
                "from_camera": str(tmp_path / "from_camera"),
                "to_note": str(tmp_path / "to_note"),
            },
            "external_tools": {"exiftool": EXIFTOOL or "exiftool", "flame_config": str(ROOT / "standard.yaml")},
        }
        data.update(extra)
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return ConfigManager(path)

    return _make


def write_jpeg(path: Path, size=(600, 400), color=(30, 90, 160), mode="RGB", icc_profile=None, **exif):
    """JPEG を作る。exif にタグを渡すと exiftool で書き込む（例: Model="NIKON Z 6_3"）。"""
    from PIL import Image

    img = Image.new(mode, size, color if mode == "RGB" else 128)
    options = {"icc_profile": icc_profile} if icc_profile else {}
    img.save(path, "JPEG", **options)
    if exif:
        args = [EXIFTOOL, "-q", "-overwrite_original"] + [f"-{k}={v}" for k, v in exif.items()] + [str(path)]
        subprocess.run(args, check=True)
    return path


@pytest.fixture
def sample_meta():
    from modules.exif_utils import PhotoMetadata

    return PhotoMetadata(
        camera="Z 6III", lens="Z 24-70mm f/4 S", focal_length="35", iso="200", f_number="4.0",
        shutter="1/250", exposure_bias="+0.3", exposure_mode="A", picture_control="Standard",
        white_balance="Auto", color_temperature="(5500K)",
    )
