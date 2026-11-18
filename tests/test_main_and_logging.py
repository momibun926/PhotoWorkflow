"""main.py のステップ実行、EXIF抽出・書き出し、ログの切り替え。"""

import logging
import os
import subprocess
import sys
from unittest import mock

import pytest

from conftest import ROOT, requires_exiftool, write_jpeg
from modules import exif_exporter, logging_config
from modules.frame_processor import ImageProcessor

import main


@pytest.fixture
def ctx(tmp_path, make_config):
    (tmp_path / "from_camera").mkdir()
    config = make_config()
    return main.WorkflowContext(
        config=config, from_camera_dir=tmp_path / "from_camera", to_note_dir=tmp_path / "to_note",
        copy_target_dirs=[], base_dir=tmp_path / "base", flame_yaml=str(ROOT / "standard.yaml"),
    )


def _steps(first_action, abort_on_failure=False):
    calls = []

    def second(_ctx):
        calls.append("second")
        return True

    steps = [
        main.WorkflowStep("copy", "S1", first_action, requires_confirmation=False, abort_on_failure=abort_on_failure),
        main.WorkflowStep("frame", "S2", second, requires_confirmation=False),
    ]
    return steps, calls


def _boom(_ctx):
    raise RuntimeError("boom")


def test_workflow_continues_after_exception(ctx, capsys):
    steps, calls = _steps(_boom)
    with mock.patch.object(main, "build_steps", return_value=steps):
        main.run_workflow(ctx, main.parse_args(["--yes"]))
    out = capsys.readouterr().out
    assert calls == ["second"]
    assert "失敗したステップがあります" in out and "- S1" in out


def test_workflow_stops_when_abort_on_failure(ctx):
    steps, calls = _steps(_boom, abort_on_failure=True)
    with mock.patch.object(main, "build_steps", return_value=steps):
        main.run_workflow(ctx, main.parse_args(["--yes"]))
    assert calls == []


def test_single_step_returns_false_on_exception(ctx):
    step = main.WorkflowStep("frame", "S", _boom, requires_source=False)
    assert main.run_single_step(ctx, step, main.parse_args([])) is False


def test_dry_run_does_not_execute(ctx):
    steps, calls = _steps(_boom)
    with mock.patch.object(main, "build_steps", return_value=steps):
        main.run_workflow(ctx, main.parse_args(["--yes", "--dry-run"]))
    assert calls == []


def test_run_exif_exporter_reports_result(tmp_path):
    assert main.run_exif_exporter(tmp_path / "missing") is False
    with mock.patch.object(main, "export_exif", return_value=exif_exporter.ExportResult(ok=True)):
        assert main.run_exif_exporter(tmp_path) is True
    with mock.patch.object(main, "export_exif", side_effect=RuntimeError("boom")):
        assert main.run_exif_exporter(tmp_path) is False


def test_exporter_module_exit_code(tmp_path):
    result = subprocess.run([sys.executable, "-m", "modules.exif_exporter", str(tmp_path / "missing")],
                            cwd=ROOT, capture_output=True)
    assert result.returncode == 1


def test_running_module_file_directly_shows_how_to_run(tmp_path):
    """python modules/xxx.py と直接実行したら、python -m で実行するよう案内して終わる。"""
    result = subprocess.run([sys.executable, str(ROOT / "modules" / "exif_exporter.py")],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 1
    assert "python -m modules.exif_exporter" in result.stderr


def test_manual_gps_tagger_is_started_as_module(tmp_path, monkeypatch):
    """手動GPS付与ツールは -m で起動し、カレントディレクトリは変えない。"""
    calls = []
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or mock.Mock(returncode=0))
    monkeypatch.chdir(tmp_path)
    main.run_manual_gps_tagger()
    (cmd, kw), = calls
    assert cmd == [sys.executable, "-m", "modules.manual_gps_tagger"]
    assert "cwd" not in kw
    assert kw["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT)


@requires_exiftool
def test_exporter_extensions_and_exclusions(tmp_path, make_config):
    config = make_config(file_types={"jpeg_extensions": [".jpg"], "raw_extensions": [".nef", ".arw"]},
                         exif_export={"tag_labels": {"IFD0:Model": "機種"}})
    note = tmp_path / "to_note"
    note.mkdir()
    write_jpeg(note / "a.jpg", Model="Z")
    write_jpeg(note / "b.jpg", Model="Z").rename(note / "b.arw")
    (note / "day1").mkdir()
    write_jpeg(note / "day1" / "c.jpg", Model="Z")
    frame_dir = note / ImageProcessor(ROOT / "standard.yaml").config.output.dir_name
    frame_dir.mkdir()
    write_jpeg(frame_dir / "framed.jpg", Model="Z")

    for _ in range(2):  # 2回目も、前回の出力（exif_data）やフレーム付き画像を読まない
        assert exif_exporter.extract_all_exif_recursive(note, tsv_filename="t.tsv", config=config)
        rows = (note / "exif_data" / "t.tsv").read_text(encoding="utf-8").splitlines()[1:]
        assert sorted(r.split("\t")[0].replace("\\", "/").split("/")[-1] for r in rows) == ["a.jpg", "b.arw", "c.jpg"]

    exif_text = (note / "exif_data" / "a.jpg.exif").read_text(encoding="utf-8")
    assert exif_text.splitlines()[0] == "機種: Z"


def test_clean_value():
    assert exif_exporter._clean_value("a\nb\rc\td") == "a b c\td"
    assert exif_exporter._clean_value("a\tb", "\t") == "a b"


def _logger(handler, name):
    lg = logging.getLogger(name)
    lg.propagate = False
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    return lg


def test_log_rotation(tmp_path):
    handler = logging_config._SafeRotatingFileHandler(tmp_path / "x.log", maxBytes=200, backupCount=2, encoding="utf-8")
    lg = _logger(handler, "test_rotation")
    for i in range(50):
        lg.info("line %03d xxxxxxxxxxxxxxxxxxxxxxxx", i)
    handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.log", "x.log.1", "x.log.2"]


def test_log_rotation_failure_keeps_writing(tmp_path):
    """別のプロセスがファイルを開いていて切り替えられない（Windows）場合も、ログは書き続ける。"""
    log = tmp_path / "y.log"
    handler = logging_config._SafeRotatingFileHandler(log, maxBytes=100, backupCount=2, encoding="utf-8")
    lg = _logger(handler, "test_rotation_fail")
    with mock.patch.object(handler, "rotate", side_effect=PermissionError("使用中")), \
            mock.patch.object(handler, "handleError") as handle_error:
        for i in range(20):
            lg.info("line %03d xxxxxxxxxxxxxxxxxxxxxxx", i)
    handler.close()
    handle_error.assert_not_called()
    assert "line 019" in log.read_text(encoding="utf-8")
