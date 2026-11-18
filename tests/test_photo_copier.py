"""写真整理・コピー: コピーに成功した写真だけをゴミ箱へ移動する。"""

from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest

from conftest import requires_exiftool, write_jpeg
from modules import photo_copier


def _run(tmp_path, jpegs, copy=None, trashed=None):
    """JPEG を to_note と amazon にコピーする。ゴミ箱へ送ったファイル名は trashed に入る。"""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            photo_copier, "send2trash", mock.Mock(send2trash=lambda p: trashed.append(Path(p).name))))
        if copy is not None:
            stack.enter_context(mock.patch.object(photo_copier.shutil, "copy2", copy))
        return photo_copier.copy_and_organize_photos(
            list(jpegs), [], tmp_path / "to_note", [tmp_path / "amazon"], tmp_path / "base",
        )


def test_categorize_files(tmp_path):
    for name in ["a.JPG", "b.jpeg", "c.NEF", "d.gpx", "e.mov"]:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    jpeg, raw, gpx = photo_copier.categorize_files(tmp_path)
    assert sorted(p.name for p in jpeg) == ["a.JPG", "b.jpeg"]
    assert [p.name for p in raw] == ["c.NEF"]
    assert [p.name for p in gpx] == ["d.gpx"]


def test_all_copied_are_trashed(tmp_path):
    src = tmp_path / "from_camera"
    src.mkdir()
    a = src / "a.jpg"
    a.write_bytes(b"x" * 100)
    trashed = []
    assert _run(tmp_path, [a], trashed=trashed) is True
    assert trashed == ["a.jpg"]
    assert (tmp_path / "amazon" / "a.jpg").read_bytes() == b"x" * 100


def test_failed_copy_is_not_trashed(tmp_path):
    src = tmp_path / "from_camera"
    src.mkdir()
    ok, bad = src / "ok.jpg", src / "bad.jpg"
    ok.write_bytes(b"x" * 100)
    bad.write_bytes(b"y" * 100)
    real_copy = photo_copier.shutil.copy2

    def copy(s, d):
        if Path(s).name == "bad.jpg":
            raise OSError("ディスクがいっぱい")
        return real_copy(s, d)

    trashed = []
    assert _run(tmp_path, [ok, bad], copy=copy, trashed=trashed) is False
    assert trashed == ["ok.jpg"]


def test_size_mismatch_is_not_trashed(tmp_path):
    src = tmp_path / "from_camera"
    src.mkdir()
    a = src / "a.jpg"
    a.write_bytes(b"x" * 100)

    def truncated_copy(s, d):
        Path(d).write_bytes(b"x" * 10)

    trashed = []
    assert _run(tmp_path, [a], copy=truncated_copy, trashed=trashed) is False
    assert trashed == []


@requires_exiftool
def test_raw_goes_to_date_folder_or_unknown(tmp_path, make_config):
    config = make_config()
    src = tmp_path / "from_camera"
    src.mkdir()
    # 中身は JPEG だが、拡張子で RAW として扱われる。撮影日は exiftool で読める
    dated = write_jpeg(src / "dated.jpg", DateTimeOriginal="2026:11:01 10:30:00").rename(src / "dated.dng")
    undated = write_jpeg(src / "undated.jpg").rename(src / "undated.dng")
    trashed = []
    with mock.patch.object(photo_copier, "send2trash", mock.Mock(send2trash=lambda p: trashed.append(p))):
        ok = photo_copier.copy_and_organize_photos(
            [], [dated, undated], tmp_path / "to_note", [], tmp_path / "base", config=config,
        )
    assert ok
    assert (tmp_path / "base" / "2026" / "11" / "20261101" / "dated.dng").exists()
    assert (tmp_path / "base" / "unknown" / "undated.dng").exists()
    assert len(trashed) == 2


def test_raw_destination(tmp_path):
    assert photo_copier.raw_destination(tmp_path, "20261101") == tmp_path / "2026" / "11" / "20261101"
    assert photo_copier.raw_destination(tmp_path, None) is None
    assert photo_copier.raw_destination(tmp_path, "2026-11") is None


def test_copy_jpegs_reports_progress_without_printing(tmp_path, capsys):
    """処理の関数は画面に表示せず、進み具合と失敗を通知先に知らせる。"""
    src = tmp_path / "src"
    src.mkdir()
    files = []
    for name in ["a.jpg", "b.jpg"]:
        (src / name).write_bytes(b"x" * 10)
        files.append(src / name)
    dest1, dest2 = tmp_path / "d1", tmp_path / "d2"
    dest1.mkdir()  # d2 は作らない → コピーに失敗する
    copied, failed = [], []
    result = photo_copier.copy_jpegs(files, [dest1, dest2],
                                     on_copied=lambda i, n, p, dest: copied.append((i, n, p.name)),
                                     on_failed=lambda p, e: failed.append(p.name))
    assert capsys.readouterr().out == ""
    assert copied == [] and failed == ["a.jpg", "b.jpg"]
    assert result.failed == files and result.copied == []

    dest2.mkdir()
    result = photo_copier.copy_jpegs(files, [dest1, dest2],
                                     on_copied=lambda i, n, p, dest: copied.append((i, n, p.name, dest)))
    assert copied == [(1, 2, "a.jpg", dest1), (2, 2, "b.jpg", dest1)]
    assert result.copied == files


def test_trash_files_counts_and_reports(tmp_path, no_wait):
    files = [tmp_path / "a", tmp_path / "b"]

    def trash(path):
        if path.endswith("b"):
            raise OSError("使用中")

    failed = []
    with mock.patch.object(photo_copier, "send2trash", mock.Mock(send2trash=trash)):
        assert photo_copier.trash_files(files, on_failed=lambda p, e: failed.append(p.name)) == 1
    assert failed == ["b"]
    with mock.patch.object(photo_copier, "send2trash", None):
        assert photo_copier.trash_files(files) == 0


@pytest.fixture
def no_wait(monkeypatch):
    """ゴミ箱への移動のやり直しで、実際には待たない。"""
    waits = []
    monkeypatch.setattr(photo_copier.time, "sleep", waits.append)
    return waits


def test_trash_retries_temporary_failure(tmp_path, no_wait):
    """一時的な失敗（ウイルス対策ソフトがファイルを開いているなど）は、待ってやり直せば移せる。"""
    calls = []

    def trash(path):
        calls.append(path)
        if len(calls) < 3:
            raise OSError("[WinError 161] 指定されたパスは無効です。")

    failed = []
    with mock.patch.object(photo_copier, "send2trash", mock.Mock(send2trash=trash)):
        assert photo_copier.trash_files([tmp_path / "a.NEF"], on_failed=lambda p, e: failed.append(p)) == 1
    assert len(calls) == 3 and failed == []
    assert no_wait == [0.5, 1.0]


def test_trash_gives_up_after_retries(tmp_path, no_wait):
    def trash(path):
        raise OSError("使用中")

    failed = []
    with mock.patch.object(photo_copier, "send2trash", mock.Mock(send2trash=trash)):
        assert photo_copier.trash_files([tmp_path / "a.NEF"], on_failed=lambda p, e: failed.append(p.name)) == 0
    assert failed == ["a.NEF"]
    assert no_wait == list(photo_copier.TRASH_RETRY_DELAYS)


def test_summary_lists_files_left_in_source(tmp_path, no_wait, capsys):
    src = tmp_path / "from_camera"
    src.mkdir()
    a, b = src / "a.jpg", src / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"y")

    def trash(path):
        if path.endswith("b.jpg"):
            raise OSError("使用中")

    with mock.patch.object(photo_copier, "send2trash", mock.Mock(send2trash=trash)):
        assert photo_copier.copy_and_organize_photos([a, b], [], tmp_path / "note", [], tmp_path / "base") is True
    out = capsys.readouterr().out
    assert "ゴミ箱へ移せず  : 1 件" in out
    assert "         - b.jpg" in out
