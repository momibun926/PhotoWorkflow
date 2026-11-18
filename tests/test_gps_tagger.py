"""exiftool -geotag の出力の解析（GPS位置情報の書き込みのサマリー表示の元になる）。"""

from conftest import requires_exiftool, write_jpeg
from modules import gps_tagger
from modules.gps_tagger import (
    REASON_NO_DATE, REASON_OTHER, REASON_OUT_OF_RANGE, _classify_reason, _split_message, parse_geotag_output,
)

# exiftool 12.76 の実際の出力（-progress -geotag ... -if "not $GPSLatitude"）
REAL_OUTPUT = """\
======== ./b.jpg [1/4]
Warning: Time is too far beyond track in File:Geotime (ValueConvInv) - ./b.jpg
Warning: No writable tags set from ./b.jpg
Nothing changed in ./b.jpg
======== ./a.jpg [3/4]
======== ./c.jpg [4/4]
Warning: No writable tags set from ./c.jpg
Nothing changed in ./c.jpg
    1 directories scanned
    1 files failed condition
    1 image files updated
    2 image files unchanged
"""


def test_parse_real_output():
    result = parse_geotag_output(REAL_OUTPUT, "")
    assert (result.updated, result.unchanged, result.skipped_existing, result.error_files) == (1, 2, 1, 0)
    reasons = {path.replace("\\", "/").split("/")[-1]: reason for path, reason in result.not_tagged.items()}
    assert reasons == {"b.jpg": REASON_OUT_OF_RANGE, "c.jpg": REASON_OTHER}
    assert not result.has_error_line


def test_parse_errors():
    output = "Error: File not found - x.jpg\n    1 files weren't updated due to errors\n"
    result = parse_geotag_output(output, "")
    assert result.error_files == 1
    assert result.has_error_line
    assert result.messages["Error: File not found"] == 1


def test_split_message_uses_last_separator():
    """本文中の " - " ではなく、最後の " - " でファイルパスを切り出す。"""
    assert _split_message("Warning: [minor] A - B - C:/x/a.jpg") == ("Warning", "A - B", "C:/x/a.jpg")
    assert _split_message("Nothing changed in a.jpg") is None


def test_classify_reason():
    assert _classify_reason("Time is too far before track") == REASON_OUT_OF_RANGE
    assert _classify_reason("Tag 'DateTimeOriginal' not defined") == REASON_NO_DATE
    assert _classify_reason("") == REASON_OTHER


@requires_exiftool
def test_apply_gps_tags_end_to_end(tmp_path, make_config):
    """GPX の範囲内の写真には位置が入り、既に位置がある写真は書き換えない。

    撮影時刻は 10:30（+09:00）= 01:30Z で、記録点 01:20Z と 01:40Z のちょうど中間になる。
    """
    from modules.exiftool_client import ExifToolClient

    config = make_config()
    folder = tmp_path / "from_camera"
    folder.mkdir()
    gpx = folder / "track.gpx"
    gpx.write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
        "<trk><trkseg>"
        '<trkpt lat="35.0" lon="139.0"><time>2026-11-01T01:20:00Z</time></trkpt>'
        '<trkpt lat="35.1" lon="139.1"><time>2026-11-01T01:40:00Z</time></trkpt>'
        "</trkseg></trk></gpx>",
        encoding="utf-8",
    )
    inside = write_jpeg(folder / "inside.jpg", DateTimeOriginal="2026:11:01 10:30:00", OffsetTimeOriginal="+09:00")
    has_gps = write_jpeg(
        folder / "has_gps.jpg", DateTimeOriginal="2026:11:01 10:30:00", OffsetTimeOriginal="+09:00",
        GPSLatitude=1, GPSLatitudeRef="N", GPSLongitude=1, GPSLongitudeRef="E",
    )

    assert gps_tagger.apply_gps_tags(folder, [gpx], config=config)

    client = ExifToolClient(config=config)
    lat, lng = client.get_gps(inside)
    assert abs(lat - 35.05) < 0.01 and abs(lng - 139.05) < 0.01
    assert client.get_gps(has_gps) == (1.0, 1.0)


@requires_exiftool
def test_geotag_photos_returns_result_without_printing(tmp_path, make_config, capsys):
    config = make_config()
    folder = tmp_path / "from_camera"
    folder.mkdir()
    gpx = folder / "t.gpx"
    gpx.write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
        '<trk><trkseg><trkpt lat="35.0" lon="139.0"><time>2026-11-01T01:20:00Z</time></trkpt>'
        '<trkpt lat="35.1" lon="139.1"><time>2026-11-01T01:40:00Z</time></trkpt></trkseg></trk></gpx>',
        encoding="utf-8",
    )
    write_jpeg(folder / "a.jpg", DateTimeOriginal="2026:11:01 10:30:00", OffsetTimeOriginal="+09:00")
    started, files = [], []
    result, geosync = gps_tagger.geotag_photos(folder, [gpx], config, on_start=started.extend,
                                               on_file=lambda path, status, problem: files.append((path, status)))
    assert capsys.readouterr().out == ""
    assert result.updated == 1 and geosync is None
    assert [p.name for p in started] == ["a.jpg"]
    assert [(p.split("/")[-1], s) for p, s in files] == [("a.jpg", gps_tagger.STATUS_WRITTEN)]


@requires_exiftool
def test_each_photo_gets_a_status(tmp_path, make_config):
    """スキップ・書き込み・GPXの範囲外・撮影日時なし・エラーを、写真ごとに知らせる。"""
    config = make_config()
    folder = tmp_path / "from_camera"
    folder.mkdir()
    gpx = folder / "t.gpx"
    gpx.write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
        '<trk><trkseg><trkpt lat="35.0" lon="139.0"><time>2026-11-01T01:20:00Z</time></trkpt>'
        '<trkpt lat="35.1" lon="139.1"><time>2026-11-01T01:40:00Z</time></trkpt></trkseg></trk></gpx>',
        encoding="utf-8",
    )
    write_jpeg(folder / "a_in.jpg", DateTimeOriginal="2026:11:01 10:30:00", OffsetTimeOriginal="+09:00")
    write_jpeg(folder / "b_out.jpg", DateTimeOriginal="2026:12:01 10:30:00", OffsetTimeOriginal="+09:00")
    write_jpeg(folder / "c_nodate.jpg")
    write_jpeg(folder / "d_hasgps.jpg", DateTimeOriginal="2026:11:01 10:30:00",
               GPSLatitude=1, GPSLatitudeRef="N", GPSLongitude=1, GPSLongitudeRef="E")
    write_jpeg(folder / "e_fake.jpg", DateTimeOriginal="2026:11:01 10:30:00").rename(folder / "e_fake.dng")

    statuses = {}
    result, _ = gps_tagger.geotag_photos(
        folder, [gpx], config,
        on_file=lambda path, status, problem: statuses.__setitem__(path.replace("\\", "/").split("/")[-1],
                                                                   (status, problem)))
    assert statuses == {
        "a_in.jpg": (gps_tagger.STATUS_WRITTEN, False),
        "b_out.jpg": ("付与できず（GPXの記録範囲外）", True),
        "c_nodate.jpg": ("付与できず（撮影日時なし）", True),
        "d_hasgps.jpg": (gps_tagger.STATUS_SKIPPED, False),
        "e_fake.dng": (statuses["e_fake.dng"][0], True),
    }
    assert statuses["e_fake.dng"][0].startswith("エラー（")
    assert (result.updated, result.skipped_existing, result.error_files) == (1, 1, 1)
    reasons = {p.replace("\\", "/").split("/")[-1]: r for p, r in result.not_tagged.items()}
    assert reasons == {"b_out.jpg": gps_tagger.REASON_OUT_OF_RANGE, "c_nodate.jpg": gps_tagger.REASON_NO_DATE}


@requires_exiftool
def test_many_photos_are_passed_in_a_file(tmp_path, make_config):
    """写真の一覧はファイル（-@）で渡すため、ファイル名に空白や日本語があっても書き込める。"""
    config = make_config()
    folder = tmp_path / "from camera 写真"
    folder.mkdir()
    gpx = folder / "t.gpx"
    gpx.write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
        '<trk><trkseg><trkpt lat="35.0" lon="139.0"><time>2026-11-01T01:20:00Z</time></trkpt>'
        '<trkpt lat="35.1" lon="139.1"><time>2026-11-01T01:40:00Z</time></trkpt></trkseg></trk></gpx>',
        encoding="utf-8",
    )
    for name in ["写真 1.jpg", "photo 2.jpg"]:
        write_jpeg(folder / name, DateTimeOriginal="2026:11:01 10:30:00", OffsetTimeOriginal="+09:00")
    result, _ = gps_tagger.geotag_photos(folder, [gpx], config)
    assert result.updated == 2
