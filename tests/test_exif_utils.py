"""EXIF の値の変換（シャッタースピード・露出モード・カメラ名）とメタデータの一括取得。"""

from unittest import mock

import pytest

from conftest import requires_exiftool, write_jpeg
from modules import constants
from modules.exif_utils import ExifConverter, ExifReader
from modules.exiftool_client import path_key


@pytest.mark.parametrize("value, expected", [
    (0.004, "1/250"),
    (1 / 125, "1/125"),
    (0.4, "1/2.5"),
    (0.5, "1/2"),
    ("0.01", "1/100"),
    (1, '1"'),
    (2.5, '2.5"'),
    (30, '30"'),
    (0, "-"),
    (-1, "-"),
])
def test_to_shutter_speed(value, expected):
    assert ExifConverter.to_shutter_speed(value) == expected


def test_to_shutter_speed_invalid_returns_text():
    assert ExifConverter.to_shutter_speed("abc") == "abc"


def test_to_shutter_speed_missing_is_dash():
    """露出時間が記録されていない写真は、他の項目と同じく "-"（以前は "None" と表示されていた）。"""
    assert ExifConverter.to_shutter_speed(None) == "-"
    assert ExifConverter.to_shutter_speed("") == "-"


@pytest.mark.parametrize("code, expected", [
    (1, "M"), (2, "P"), (3, "A"), (4, "S"), (0, "AUTO"), (None, "AUTO"), (7, "(7)"), ("x", "(x)"),
])
def test_to_exposure_mode(code, expected):
    assert ExifConverter.to_exposure_mode(code) == expected


@pytest.mark.parametrize("name, expected", [
    ("NIKON Z 6_3", "Z 6III"),
    ("NIKON Z50_2", "Z50II"),
    ("NIKKOR Z 24-70mm f/4 S", "Z 24-70mm f/4 S"),
    ("SONY ILCE-7M4", "Sony ILCE-7M4"),
    ("", "Unknown"),
    (None, "Unknown"),
    ("Unknown", "Unknown"),
])
def test_format_camera_name_default_rules(name, expected):
    assert ExifConverter.format_camera_name(name) == expected


def test_format_camera_name_custom_rules():
    rules = {"replacements": {"X-T5": "X-T5 Body"}, "removes": [], "brand_replacements": {}}
    assert ExifConverter.format_camera_name("X-T5", rules) == "X-T5 Body"


def test_parse_metadata_result():
    result = {
        "SourceFile": "a.jpg",
        "EXIF:Model": "NIKON Z 6_3",
        "EXIF:LensModel": "NIKKOR Z 24-70mm f/4 S",
        "EXIF:FocalLength": "35.0 mm",
        "EXIF:ISO": 200,
        "EXIF:FNumber": 4.0,
        "EXIF:ExposureTime": 0.004,
        "EXIF:ExposureCompensation": 0.3,
        "EXIF:ExposureProgram": 3,
        "MakerNotes:WhiteBalance": " Auto ",
        "MakerNotes:ColorTemperature": 5500.0,
    }
    meta = ExifReader._parse_metadata_result(result)
    assert (meta.camera, meta.lens, meta.focal_length) == ("Z 6III", "Z 24-70mm f/4 S", "35.0")
    assert (meta.shutter, meta.exposure_bias, meta.exposure_mode) == ("1/250", "+0.3", "A")
    assert (meta.white_balance, meta.color_temperature) == ("Auto", "(5500K)")


def test_parse_metadata_result_defaults():
    meta = ExifReader._parse_metadata_result({"SourceFile": "a.jpg"})
    assert meta.exposure_bias == "0.0"
    assert meta.white_balance == "-"
    assert meta.color_temperature == ""


def test_path_key_ignores_separator_and_dots():
    assert path_key("C:/x/../y/a.jpg") == path_key("C:\\y\\a.jpg")


@requires_exiftool
def test_one_bad_file_does_not_break_batch(tmp_path, make_config):
    """1枚の値の変換に失敗しても、残りの写真のメタデータは取得できる。"""
    config = make_config()
    good = write_jpeg(tmp_path / "good.jpg", Model="Z")
    bad = write_jpeg(tmp_path / "bad.jpg", Model="Z")
    original = ExifReader._parse_metadata_result

    def flaky(result, rules=None):
        if str(result["SourceFile"]).endswith("bad.jpg"):
            raise ValueError("想定外の値")
        return original(result, rules)

    with mock.patch.object(ExifReader, "_parse_metadata_result", staticmethod(flaky)):
        metadata = ExifReader(config=config).get_photo_metadata_batch([good, bad])
    assert path_key(good) in metadata
    assert path_key(bad) not in metadata


@requires_exiftool
def test_get_exif_dates_batch(tmp_path, make_config):
    config = make_config()
    dated = write_jpeg(tmp_path / "dated.jpg", DateTimeOriginal="2026:11:01 10:30:00")
    undated = write_jpeg(tmp_path / "undated.jpg")
    dates = ExifReader(config=config).get_exif_dates_batch([dated, undated])
    assert dates == {dated: "20261101"}
    assert constants.EXIF_DATE_FORMAT == "%Y%m%d"
