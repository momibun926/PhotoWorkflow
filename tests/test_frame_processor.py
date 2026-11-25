"""フレーム付与: フレームのレイアウト計算と、フレーム付き画像の出力。"""

from pathlib import Path
from unittest import mock

import pytest
from PIL import Image, ImageCms, ImageDraw

from conftest import ROOT, requires_exiftool, write_jpeg
from modules.exiftool_client import path_key
from modules.frame_processor import ImageProcessor

STANDARD = ROOT / "standard.yaml"


@pytest.fixture
def processor():
    return ImageProcessor(STANDARD)


def _with_metadata(processor, files, meta):
    """exiftool を使わずに、全ファイルに meta を返すようにする。"""
    fake = mock.Mock(get_photo_metadata_batch=lambda paths: {path_key(p): meta for p in files})
    return mock.patch.object(type(processor), "exif_reader", mock.PropertyMock(return_value=fake))


@pytest.mark.parametrize("value, expected", [
    (None, 0), (0.02, 20), ("0.02", 20), ("120px", 120), (" 80PX ", 80), (0, 0),
])
def test_to_px(value, expected):
    assert ImageProcessor._to_px(value, 1000) == expected


@pytest.mark.parametrize("value, expected", [
    ("4:5", 0.8), ("1:1", 1.0), (1.5, 1.5), ("1.25", 1.25), (None, None), ("none", None), ("", None),
])
def test_parse_aspect(value, expected):
    assert ImageProcessor._parse_aspect(value) == expected


def _yaml_processor(tmp_path, canvas):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["canvas"] = canvas
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return ImageProcessor(path)


def test_compute_margins_explicit(tmp_path):
    p = _yaml_processor(tmp_path, {"margins": {"top": "10px", "bottom": 0.1, "left": 0, "right": "5px"}})
    assert p._compute_margins(1000, 500, 1000) == {"top": 10, "bottom": 100, "left": 0, "right": 5}


@pytest.mark.parametrize("fill, expected_top, expected_bottom", [
    ("center", 250, 250), ("bottom", 0, 500), ("top", 500, 0),
])
def test_compute_margins_aspect_adds_height(tmp_path, fill, expected_top, expected_bottom):
    """横長 1000x500 を 1:1 にするには高さが 500 足りない。aspect_fill に従って上下に配る。"""
    p = _yaml_processor(tmp_path, {"margins": {"top": 0, "bottom": 0, "left": 0, "right": 0},
                                   "aspect_ratio": "1:1", "aspect_fill": fill})
    m = p._compute_margins(1000, 500, 1000)
    assert (m["top"], m["bottom"], m["left"], m["right"]) == (expected_top, expected_bottom, 0, 0)


def test_compute_margins_portrait_ratio(tmp_path):
    """縦長の写真には aspect_ratio_portrait を使い、横幅が足りなければ左右に足す。"""
    p = _yaml_processor(tmp_path, {"margins": {"top": 0, "bottom": 0, "left": 0, "right": 0},
                                   "aspect_ratio": "1:1", "aspect_ratio_portrait": "4:5"})
    m = p._compute_margins(600, 1000, 1000)
    assert m["left"] + m["right"] == 200 and m["top"] == m["bottom"] == 0


def test_fonts_and_logo_resolved_from_other_folder(tmp_path, monkeypatch, processor):
    """起動したフォルダに関係なく、同梱のフォントとロゴを使う。"""
    monkeypatch.chdir(tmp_path)
    fonts = processor._resolve_font_list("bold")
    assert fonts[0] == str(ROOT / "fonts" / "Rubik-Regular.ttf")
    assert fonts[1] == "arialbd.ttf"  # 見つからないものは OS のフォント名としてそのまま渡す
    assert processor._resolve_path("nikon_logo.png") == ROOT / "nikon_logo.png"


def test_font_fallback_warns_once(processor, caplog):
    fonts = ["no-such-font.ttf", str(ROOT / "fonts" / "Rubik-Regular.ttf")]
    for _ in range(3):
        processor._get_font(fonts, 20)
    warnings = [r for r in caplog.records if "no-such-font.ttf" in r.getMessage()]
    assert len(warnings) == 1


def test_font_not_found(processor):
    with pytest.raises(FileNotFoundError):
        processor._get_font(["no-such-font-1.ttf", "no-such-font-2.ttf"], 20)


@pytest.mark.parametrize("yaml_name", ["standard.yaml", "black.yaml", "custom_example.yaml"])
def test_render_each_bundled_layout(tmp_path, sample_meta, yaml_name):
    p = ImageProcessor(ROOT / yaml_name)
    src = write_jpeg(tmp_path / "p.jpg", size=(1500, 1000))
    out = p.render(src, sample_meta)
    assert out.parent == tmp_path / p.config.output.dir_name
    assert out.name == f"{p.config.output.prefix}p.jpg"
    w, h = Image.open(out).size
    assert w >= 1500 and h > 1000


def test_render_keeps_icc_profile_for_rgb(tmp_path, processor, sample_meta):
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    out = processor.render(write_jpeg(tmp_path / "p.jpg", icc_profile=icc), sample_meta)
    assert Image.open(out).info.get("icc_profile") == icc


def test_render_grayscale_has_no_profile(tmp_path, processor, sample_meta):
    out = processor.render(write_jpeg(tmp_path / "g.jpg", mode="L"), sample_meta)
    assert Image.open(out).mode == "RGB"
    assert not Image.open(out).info.get("icc_profile")


def test_unknown_template_key_becomes_empty(tmp_path, sample_meta):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["layout"]["lines"] = [{"text": "{camera} {no_such_key}"}]
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert ImageProcessor(path).render(write_jpeg(tmp_path / "p.jpg"), sample_meta).exists()


def test_run_skips_raw_and_counts(tmp_path, processor, sample_meta):
    jpg = write_jpeg(tmp_path / "a.jpg")
    png = tmp_path / "b.png"
    Image.new("RGB", (300, 200)).save(png)
    (tmp_path / "c.nef").write_bytes(b"raw")
    (tmp_path / "d.txt").write_text("x")
    with _with_metadata(processor, [jpg, png], sample_meta):
        assert processor.run(tmp_path, workers=1) == 2
    outputs = sorted(f.name for f in (tmp_path / processor.config.output.dir_name).iterdir())
    assert outputs == sorted([f"{processor.config.output.prefix}a.jpg", f"{processor.config.output.prefix}b.jpg"])


def test_run_parallel_matches_sequential(tmp_path, sample_meta):
    folders = {}
    for workers in (1, 3):
        folder = tmp_path / f"w{workers}"
        folder.mkdir()
        files = [write_jpeg(folder / f"p{i}.jpg", size=(800, 600), color=(i * 40, 80, 120)) for i in range(4)]
        p = ImageProcessor(STANDARD)
        with _with_metadata(p, files, sample_meta):
            assert p.run(folder, workers=workers) == 4
        out = folder / p.config.output.dir_name
        folders[workers] = {f.name: Image.open(f).tobytes() for f in out.iterdir()}
    assert folders[1] == folders[3]


def test_run_parallel_falls_back_when_pool_fails(tmp_path, processor, sample_meta):
    files = [write_jpeg(tmp_path / f"p{i}.jpg") for i in range(3)]
    with _with_metadata(processor, files, sample_meta), \
            mock.patch("modules.frame_processor.ProcessPoolExecutor", side_effect=OSError("no processes")):
        assert processor.run(tmp_path, workers=2) == 3


def test_workers_setting(make_config):
    assert ImageProcessor(STANDARD, app_config=make_config(frame={"workers": 1}))._workers() == 1
    assert ImageProcessor(STANDARD, app_config=make_config(frame={"workers": "x"}))._workers() >= 1


@requires_exiftool
def test_run_frame_processing_end_to_end(tmp_path, make_config):
    from modules.frame_processor import run_frame_processing

    config = make_config(frame={"workers": 1})
    note = tmp_path / "to_note"
    note.mkdir()
    write_jpeg(note / "DSC_0001.jpg", size=(1200, 800), Model="NIKON Z 6_3", FNumber=4, ExposureTime=0.004, ISO=200)
    assert run_frame_processing(note, str(STANDARD), app_config=config)
    assert any((note / ImageProcessor(STANDARD).config.output.dir_name).iterdir())


def test_run_notifies_events_without_printing(tmp_path, processor, sample_meta, capsys):
    """一括処理は画面に表示せず、FrameEvents に進み具合を知らせる。"""
    from modules.frame_processor import FrameEvents

    ok = write_jpeg(tmp_path / "ok.jpg")
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    no_meta = write_jpeg(tmp_path / "no_meta.jpg")

    class Recorder(FrameEvents):
        def __init__(self):
            self.log = []

        def reading_metadata(self, files):
            self.log.append(("reading", len(files)))

        def no_metadata(self, path):
            self.log.append(("no_metadata", path.name))

        def done(self, path, out_path, current, total):
            self.log.append(("done", path.name, current, total))

        def failed(self, path, error):
            self.log.append(("failed", path.name))

    events = Recorder()
    fake = mock.Mock(get_photo_metadata_batch=lambda paths: {path_key(ok): sample_meta, path_key(broken): sample_meta})
    with mock.patch.object(type(processor), "exif_reader", mock.PropertyMock(return_value=fake)):
        assert processor.run(tmp_path, workers=1, events=events) == 1
    assert capsys.readouterr().out == ""
    assert events.log[0] == ("reading", 3)
    assert ("no_metadata", "no_meta.jpg") in events.log
    assert ("failed", "broken.jpg") in events.log
    assert any(e[0] == "done" and e[1] == "ok.jpg" and e[3] == 2 for e in events.log)


def _skip_setup(tmp_path, sample_meta, make_config=None, **frame):
    files = [write_jpeg(tmp_path / f"p{i}.jpg") for i in range(3)]
    config = make_config(frame={"workers": 1, **frame}) if make_config else None
    return files, ImageProcessor(STANDARD, app_config=config)


def test_run_skips_photos_already_framed(tmp_path, sample_meta):
    """フレーム付き画像が作成済みの写真は、作り直さずに飛ばす。"""
    files, p = _skip_setup(tmp_path, sample_meta)
    with _with_metadata(p, files, sample_meta):
        assert p.run(tmp_path, workers=1) == 3 and p.last_skipped == 0
    new = write_jpeg(tmp_path / "new.jpg")
    with _with_metadata(p, files + [new], sample_meta):
        assert p.run(tmp_path, workers=1) == 1
    assert p.last_skipped == 3


def test_run_redoes_when_source_or_yaml_is_newer(tmp_path, sample_meta):
    import os
    import shutil

    yaml_copy = tmp_path / "layout.yaml"
    shutil.copy(STANDARD, yaml_copy)
    files = [write_jpeg(tmp_path / f"p{i}.jpg") for i in range(2)]
    p = ImageProcessor(yaml_copy)
    with _with_metadata(p, files, sample_meta):
        p.run(tmp_path, workers=1)
        out_time = p.output_path(files[0]).stat().st_mtime
        os.utime(files[0], (out_time + 10, out_time + 10))      # 元の写真の方が新しい → 作り直す
        assert p.run(tmp_path, workers=1) == 1 and p.last_skipped == 1
        later = max(f.stat().st_mtime for f in tmp_path.rglob("*.jpg")) + 10
        os.utime(yaml_copy, (later, later))                    # レイアウト設定の方が新しい → すべて作り直す
        assert p.run(tmp_path, workers=1) == 2 and p.last_skipped == 0


def test_overwrite_existing_setting(tmp_path, sample_meta, make_config):
    files, p = _skip_setup(tmp_path, sample_meta, make_config, overwrite_existing=True)
    with _with_metadata(p, files, sample_meta):
        p.run(tmp_path, workers=1)
        assert p.run(tmp_path, workers=1) == 3 and p.last_skipped == 0


def test_step_succeeds_when_everything_is_already_framed(tmp_path, sample_meta, capsys):
    from modules.frame_processor import run_frame_processing

    files, p = _skip_setup(tmp_path, sample_meta)
    with _with_metadata(p, files, sample_meta):
        p.run(tmp_path, workers=1)
    with mock.patch.object(ImageProcessor, "exif_reader", mock.PropertyMock(side_effect=AssertionError("読まない"))):
        assert run_frame_processing(tmp_path, str(STANDARD)) is True
    out = capsys.readouterr().out
    assert "フレーム付き画像が作成済みの 3 枚は飛ばします。" in out
    assert "作成済みのため飛ばした写真: 3 枚" in out


# ----------------------------------------------------------------------
# scaling.basis: display（note などで縦・横の文字の見た目をそろえる）
# ----------------------------------------------------------------------
def _scaling_processor(tmp_path, scaling, canvas=None):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["canvas"] = canvas or {"margins": {"top": 0.01, "bottom": 0.14, "left": 0.01, "right": 0.01}}
    data["scaling"] = scaling
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return ImageProcessor(path)


def _shown_scale(p, w, h, box=0.8):
    """note の表示: キャンバスを幅 1 に縮小し、縦長は 幅:高さ = box の枠に収める。そのときの倍率。"""
    ref = p._scale_reference(w, h)
    m = p._compute_margins(w, h, ref)
    cw, ch = w + m["left"] + m["right"], h + m["top"] + m["bottom"]
    return ref, min(1 / cw, 1 / (box * ch)) if box else 1 / cw


def test_scale_reference_default_is_long_side():
    """scaling を書いていない YAML（black.yaml）は従来どおり長辺が基準。"""
    p = ImageProcessor(ROOT / "black.yaml")
    assert p._scale_reference(4000, 6000) == 6000
    assert p._scale_reference(6000, 4000) == 6000


def test_scale_reference_display_keeps_landscape(tmp_path):
    p = _scaling_processor(tmp_path, {"basis": "display"})
    assert p._scale_reference(6000, 4000) == 6000


@pytest.mark.parametrize("box, canvas", [
    ("4:5", None),
    (None, None),
    ("4:5", {"margins": {"top": 0.01, "bottom": 0.14, "left": 0.01, "right": 0.01}, "aspect_ratio_portrait": "4:5"}),
])
def test_scale_reference_display_matches_landscape_on_screen(tmp_path, box, canvas):
    """表示したときの文字の大きさ（基準 × 表示倍率）が、縦写真でも横写真と同じになる。"""
    p = _scaling_processor(tmp_path, {"basis": "display", "display_box": box}, canvas)
    ref_l, s_l = _shown_scale(p, 6000, 4000, p._parse_aspect(box))
    ref_p, s_p = _shown_scale(p, 4000, 6000, p._parse_aspect(box))
    assert ref_p < 6000
    assert ref_p * s_p == pytest.approx(ref_l * s_l, rel=0.01)


def test_render_display_portrait_text_smaller(tmp_path):
    """縦写真の下余白（＝文字エリア）が長辺基準より小さくなる。"""
    from modules.exif_utils import PhotoMetadata

    meta = PhotoMetadata("Z50II", "LENS", "35", "100", "1.7", "1/500", "0.0", "A", "NEUTRAL", "AUTO", "")
    src = tmp_path / "port.jpg"
    Image.new("RGB", (400, 600), "gray").save(src)
    heights = {}
    for basis in ("long_side", "display"):
        p = _scaling_processor(tmp_path, {"basis": basis})
        with Image.open(p.render(src, meta)) as out:
            heights[basis] = out.height
    assert heights["display"] < heights["long_side"]


@pytest.mark.parametrize("scaling, message", [
    ({"basis": "width"}, "scaling.basis"),
    ({"basis": "display", "display_box": "abc"}, "scaling.display_box"),
])
def test_scaling_validation(tmp_path, scaling, message):
    with pytest.raises(ValueError, match=message):
        _scaling_processor(tmp_path, scaling)


# ----------------------------------------------------------------------
# メーカーごとのロゴの切り替え（logo.dir / logo.makers / logo.fallback）
# ----------------------------------------------------------------------
def _logo_processor(tmp_path, logo_cfg, files=("nikon.png", "google.png")):
    import yaml

    logos = tmp_path / "logos"
    logos.mkdir(exist_ok=True)
    for f in files:
        Image.new("RGBA", (100, 50), "red").save(logos / f)
    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["logo"] = {"enabled": True, **logo_cfg}
    path = tmp_path / "logo.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return ImageProcessor(path)


@pytest.mark.parametrize("make, slug", [
    ("NIKON CORPORATION", "nikon"), ("Google", "google"), ("FUJIFILM", "fujifilm"),
    ("OM Digital Solutions", "om"), ("RICOH IMAGING COMPANY, LTD.", "ricoh"), ("", ""),
])
def test_maker_slug(make, slug):
    assert ImageProcessor._maker_slug(make) == slug


def test_select_logo_by_makers(tmp_path):
    p = _logo_processor(tmp_path, {"dir": "logos", "makers": {"nikon": "nikon.png", "google": "google.png"}})
    cfg = p._section("logo")
    assert p._select_logo(cfg, "NIKON CORPORATION")[0].name == "nikon.png"
    assert p._select_logo(cfg, "Google")[0].name == "google.png"


def test_select_logo_auto_filename(tmp_path):
    """makers に無いメーカーは「メーカー名.png」を自動で探す。"""
    p = _logo_processor(tmp_path, {"dir": "logos"}, files=("fujifilm.png",))
    assert p._select_logo(p._section("logo"), "FUJIFILM")[0].name == "fujifilm.png"


def test_select_logo_missing_and_fallback(tmp_path):
    p = _logo_processor(tmp_path, {"dir": "logos"})
    assert p._select_logo(p._section("logo"), "Canon")[0] is None
    assert p._select_logo(p._section("logo"), "")[0] is None
    p = _logo_processor(tmp_path, {"dir": "logos", "fallback": "nikon.png"})
    assert p._select_logo(p._section("logo"), "Canon")[0].name == "nikon.png"


def test_select_logo_per_maker_override(tmp_path):
    p = _logo_processor(tmp_path, {
        "dir": "logos", "height_font_ratio": 2.3,
        "makers": {"google": {"file": "google.png", "height_font_ratio": 1.5}},
    })
    path, cfg = p._select_logo(p._section("logo"), "Google")
    assert path.name == "google.png" and cfg.height_font_ratio == 1.5
    _, cfg = p._select_logo(p._section("logo"), "NIKON CORPORATION")
    assert cfg.height_font_ratio == 2.3


def test_select_logo_legacy_path(tmp_path):
    """dir が無ければ、従来どおり path の1枚をメーカーに関係なく使う。"""
    p = _logo_processor(tmp_path, {"path": "logos/nikon.png"})
    assert p._select_logo(p._section("logo"), "Google")[0].name == "nikon.png"


def test_render_switches_logo_by_make(tmp_path):
    """メーカーによってロゴが入る・入らないが変わる（ロゴの赤い画素で判定）。"""
    from modules.exif_utils import PhotoMetadata

    p = _logo_processor(tmp_path, {"dir": "logos", "makers": {"nikon": "nikon.png"}}, files=("nikon.png",))
    src = tmp_path / "land.jpg"
    Image.new("RGB", (600, 400), "gray").save(src)

    def red_pixels(make):
        meta = PhotoMetadata("Z50II", "LENS", "35", "100", "1.7", "1/500", "0.0", "A", "NEUTRAL", "AUTO", "", make=make)
        with Image.open(p.render(src, meta)) as out:
            rgb = out.convert("RGB")
            return sum(1 for x in range(rgb.width) for y in range(rgb.height)
                       if (lambda c: c[0] > 200 and c[1] < 60 and c[2] < 60)(rgb.getpixel((x, y))))

    assert red_pixels("NIKON CORPORATION") > 0
    assert red_pixels("Canon") == 0


@pytest.mark.parametrize("logo_cfg, message", [
    ({"dir": "logos", "makers": ["nikon"]}, "logo.makers"),
    ({"dir": "logos", "makers": {"nikon": {"file": "n.png", "size": 3}}}, "logo.makers.nikon"),
    ({"makers": {"nikon": "nikon.png"}}, "logo.dir"),
])
def test_logo_makers_validation(tmp_path, logo_cfg, message):
    with pytest.raises(ValueError, match=message):
        _logo_processor(tmp_path, logo_cfg)


# ----------------------------------------------------------------------
# focal_length.equivalent_35mm（スマホは35mm換算で表示）
# ----------------------------------------------------------------------
def _equiv_processor(tmp_path, makers):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["focal_length"] = {"equivalent_35mm": makers}
    path = tmp_path / "eq.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return ImageProcessor(path)


def test_use_35mm_focal_length(tmp_path):
    p = _equiv_processor(tmp_path, ["google"])
    assert p._use_35mm_focal_length("Google")
    assert not p._use_35mm_focal_length("NIKON CORPORATION")
    assert not p._use_35mm_focal_length("")
    assert not _equiv_processor(tmp_path, [])._use_35mm_focal_length("Google")


def test_render_focal_length_35mm_text(tmp_path, monkeypatch):
    """equivalent_35mm のメーカーは {focal_length} が換算値になり、それ以外は実焦点距離のまま。"""
    from modules.exif_utils import PhotoMetadata

    processor = _equiv_processor(tmp_path, ["google"])

    src = tmp_path / "a.jpg"
    Image.new("RGB", (600, 400), "gray").save(src)
    texts = []
    real_text = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        texts.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    base = dict(camera="C", lens="L", focal_length="4.53", iso="28", f_number="1.7", shutter="1/280",
                exposure_bias="0.0", exposure_mode="P", picture_control="-", white_balance="Auto",
                color_temperature="", focal_length_35mm="24")
    processor.render(src, PhotoMetadata(**base, make="Google"))
    assert any(t.startswith("24mm") for t in texts)
    texts.clear()
    processor.render(src, PhotoMetadata(**base, make="NIKON CORPORATION"))
    assert any(t.startswith("4.53mm") for t in texts)


def test_focal_length_validation(tmp_path):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["focal_length"] = {"equivalent_35mm": "google"}
    path = tmp_path / "f.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="focal_length.equivalent_35mm"):
        ImageProcessor(path)


# ----------------------------------------------------------------------
# {focal_length_text}（DX は換算を付ける、FX は付けない）
# ----------------------------------------------------------------------
def _focal_meta(focal, equiv, make):
    from modules.exif_utils import PhotoMetadata

    return PhotoMetadata(camera="C", lens="L", focal_length=focal, iso="100", f_number="1.7", shutter="1/500",
                         exposure_bias="0.0", exposure_mode="A", picture_control="-", white_balance="Auto",
                         color_temperature="", focal_length_35mm=equiv, make=make)


@pytest.mark.parametrize("focal, equiv, make, text", [
    ("35", "52.5", "NIKON CORPORATION", "35mm (52.5mm)"),   # DX
    ("35", "35", "NIKON CORPORATION", "35mm"),              # FX
    ("50", "", "NIKON CORPORATION", "50mm"),                # 換算値なし
    ("4.53", "24", "Google", "4.53mm (24mm)"),               # Pixel も DX と同じく換算を付ける
])
def test_focal_length_text_crop(processor, focal, equiv, make, text):
    assert processor._focal_length_text(_focal_meta(focal, equiv, make)) == text


def test_focal_length_text_equivalent_only(tmp_path):
    """equivalent_35mm に書いたメーカーは換算値だけ。"""
    p = _equiv_processor(tmp_path, ["google"])
    assert p._focal_length_text(_focal_meta("4.53", "24", "Google")) == "24mm"


@pytest.mark.parametrize("mode, focal, equiv, text", [
    ("always", "35", "35", "35mm (35mm)"),
    ("never", "35", "52.5", "35mm"),
])
def test_focal_length_text_modes(tmp_path, mode, focal, equiv, text):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["focal_length"] = {"show_equivalent": mode}
    path = tmp_path / "f.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    p = ImageProcessor(path)
    assert p._focal_length_text(_focal_meta(focal, equiv, "NIKON CORPORATION")) == text


def test_focal_length_text_format(tmp_path):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["focal_length"] = {"equivalent_format": "{focal_length}mm ( {focal_length_35mm}mm )"}
    path = tmp_path / "f.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert ImageProcessor(path)._focal_length_text(_focal_meta("35", "52.2", "NIKON")) == "35mm ( 52.2mm )"


def test_focal_length_show_equivalent_validation(tmp_path):
    import yaml

    data = yaml.safe_load(STANDARD.read_text(encoding="utf-8"))
    data["focal_length"] = {"show_equivalent": "dx"}
    path = tmp_path / "f.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="focal_length.show_equivalent"):
        ImageProcessor(path)
