"""config.json / YAML の読み込みと検証。"""

import json

import pytest

from conftest import ROOT
from modules import constants
from modules.config_manager import APP_DIR, ConfigManager, YamlConfigManager


def test_example_config_is_valid(tmp_path, monkeypatch):
    """config.example.json はそのまま読み込めて、必須キーがそろっている。"""
    monkeypatch.chdir(tmp_path)
    config = ConfigManager(ROOT / "config.example.json")
    assert config.get_directory("to_note")
    assert config.get("external_tools.flame_config") == "standard.yaml"
    assert config.get_enabled_steps() == {k: True for k in ("gps_tag", "manual_gps", "copy", "exif_export", "frame")}


def test_explicit_missing_path_is_error(tmp_path, monkeypatch, make_config):
    make_config()
    monkeypatch.chdir(tmp_path)  # カレントディレクトリに正しい config.json があっても使わない
    with pytest.raises(FileNotFoundError):
        ConfigManager(tmp_path / "typo.json")


def test_missing_required_key(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"directories": {"base": "x"}, "external_tools": {"exiftool": "e"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="from_camera"):
        ConfigManager(path)


def test_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        ConfigManager(path)


def test_top_level_must_be_object(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        ConfigManager(path)


def test_get_and_attribute_access(make_config):
    config = make_config(gps={"skip_existing_gps": False})
    assert config.get("gps.skip_existing_gps") is False
    assert config.get("gps.nothing", "default") == "default"
    assert config.directories.to_note.endswith("to_note")


def test_resolve_path(tmp_path, make_config, monkeypatch):
    config = make_config()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    (tmp_path / "mine.yaml").write_text("x", encoding="utf-8")
    assert config.resolve_path("mine.yaml") == tmp_path / "mine.yaml"
    assert config.resolve_path("standard.yaml") == APP_DIR / "standard.yaml"


@pytest.mark.parametrize("value, expected", [
    ("+00:01:30", "+00:01:30"),
    ("-0:00:10", "-0:00:10"),
    ("", None),
    ("+09:00", None),
    ("abc", None),
])
def test_camera_clock_offset(make_config, value, expected):
    assert make_config(gps={"camera_clock_offset": value}).get_camera_clock_offset() == expected


def test_gps_options(make_config):
    options = make_config(gps={"max_interpolation_seconds": "600", "max_extrapolation_seconds": -1}).get_gps_options()
    assert options == {"skip_existing": True, "max_interpolation_secs": 600, "max_extrapolation_secs": None}


def test_file_extensions(make_config):
    ext = make_config(file_types={"raw_extensions": [".CR3", ".nef"]}).get_file_extensions()
    assert ext["raw"] == {".cr3", ".nef"}
    assert ext["jpeg"] == constants.JPEG_EXTENSIONS


def test_exif_export_tags_default(make_config):
    assert make_config().get_exif_export_tags() == constants.EXIF_EXPORT_TAGS


def test_exif_export_tags_override(make_config):
    tags = make_config(exif_export={
        "target_tags": ["ExifIFD:ISO", "XMP:Rating"],
        "tag_labels": {"ExifIFD:ISO": "ISO感度"},
    }).get_exif_export_tags()
    assert tags == {"ExifIFD:ISO": "ISO感度", "XMP:Rating": "XMP:Rating"}


def test_exif_export_labels_only(make_config):
    tags = make_config(exif_export={"tag_labels": {"ExifIFD:ISO": "ISO感度"}}).get_exif_export_tags()
    assert list(tags) == list(constants.EXIF_EXPORT_TAGS)
    assert tags["ExifIFD:ISO"] == "ISO感度"


def test_enabled_steps_ignores_comment_keys(make_config):
    steps = make_config(steps={"_comment": "x", "copy": False}).get_enabled_steps()
    assert "_comment" not in steps
    assert steps["copy"] is False and steps["frame"] is True


def test_copy_targets_legacy(make_config, tmp_path):
    config = make_config(directories={
        "base": "b", "from_camera": "f", "to_note": "n", "to_amazon_jpeg": str(tmp_path / "amz"),
    })
    assert config.get_copy_targets() == [{"name": "to_amazon_jpeg", "path": str(tmp_path / "amz")}]


@pytest.mark.parametrize("name", ["standard.yaml", "black.yaml", "custom_example.yaml"])
def test_bundled_yaml_files_are_valid(name):
    assert YamlConfigManager(ROOT / name).get("output.dir_name")


@pytest.mark.parametrize("patch, message", [
    ({"layout": {"align": "middle"}}, "layout.align"),
    ({"canvas": {"margins": {"top": 0}, "aspect_ratio": "4:x"}}, "aspect_ratio"),
    ({"output": {"prefix": "F_"}}, "dir_name"),
])
def test_yaml_validation_errors(tmp_path, patch, message):
    import yaml

    data = yaml.safe_load((ROOT / "standard.yaml").read_text(encoding="utf-8"))
    for key, value in patch.items():
        if key == "output":
            data[key] = value
        else:
            data.setdefault(key, {}).update(value)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        YamlConfigManager(path)


@pytest.mark.parametrize("value", ["auto", "always", "never", "Never"])
def test_display_color_valid(make_config, value):
    make_config(display={"color": value})


@pytest.mark.parametrize("value", ["yes", True, 1])
def test_display_color_invalid(make_config, value):
    with pytest.raises(ValueError, match="display.color"):
        make_config(display={"color": value})
