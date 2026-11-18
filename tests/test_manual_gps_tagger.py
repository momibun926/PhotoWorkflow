"""手動GPS付与ツール: 起動時のフォルダと対象の拡張子。

ツールは読み込んだ時点で config.json を読むため、別のプロセスで起動して確かめる。
PyQt6（と PyQt6-WebEngine）が入っていない環境ではスキップする。
"""

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT

pytest.importorskip("PyQt6.QtWebEngineWidgets")

SCRIPT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import PyQt6.QtWebEngineWidgets  # QApplication より先に読み込む必要がある
from PyQt6.QtWidgets import QApplication
app = QApplication(["manual_gps_tagger"])
from modules import manual_gps_tagger as m
calls = []
m.NefGpsTool.start_loading = lambda self, path: calls.append(path)
w = m.NefGpsTool()
files = []
if m.INIT_DIR:
    worker = m.LoadWorker(m.INIT_DIR, 64)
    worker.progress.connect(lambda cur, total, name, img, gps: files.append(name))
    worker.run()
print("RESULT" + json.dumps({
    "init_dir": m.INIT_DIR, "jpeg": sorted(m.JPEG_EXTS), "raw": sorted(m.RAW_EXTS),
    "autoload": calls, "files": files, "title": w.windowTitle(),
}))
'''


def _run(cwd):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox")
    result = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], cwd=cwd, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT")]
    assert lines, result.stdout + result.stderr
    return json.loads(lines[-1][len("RESULT"):])


@pytest.mark.skipif((ROOT / "config.json").exists(), reason="アプリのフォルダの config.json が優先されるため")
def test_uses_config_folder_and_extensions(tmp_path):
    folder = tmp_path / "from_camera"
    folder.mkdir()
    for name in ["a.jpg", "b.JPG", "c.arw", "d.nef", "e.png", "f.txt"]:
        (folder / name).write_bytes(b"x")
    (tmp_path / "config.json").write_text(json.dumps({
        "directories": {"base": "b", "from_camera": str(folder), "to_note": "n"},
        "external_tools": {"exiftool": "exiftool"},
        "file_types": {"jpeg_extensions": [".jpg"], "raw_extensions": [".nef", ".arw"]},
    }), encoding="utf-8")

    result = _run(tmp_path)
    assert result["init_dir"] == str(folder)
    assert result["autoload"] == [str(folder)]
    assert (result["jpeg"], result["raw"]) == ([".jpg"], [".arw", ".nef"])
    assert sorted(result["files"]) == ["a.jpg", "b.JPG", "c.arw", "d.nef"]
    assert "RAW" in result["title"]


@pytest.mark.skipif((ROOT / "config.json").exists(), reason="アプリのフォルダの config.json が優先されるため")
def test_without_config_opens_no_folder(tmp_path):
    result = _run(tmp_path)
    assert result["init_dir"] is None
    assert result["autoload"] == []
    assert result["raw"] == [".arw", ".dng", ".nef"]


def test_no_personal_paths_in_source():
    """個人の環境のパスをソースに直接書かない（config.json で指定する）。"""
    for path in [ROOT / "main.py", *(ROOT / "modules").glob("*.py"), *(ROOT / "tools").glob("*.py")]:
        assert "SHARE\\Photo" not in path.read_text(encoding="utf-8"), path
