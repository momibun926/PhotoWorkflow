"""フレーム付与: 写真にフレーム（余白・撮影データの文字・ロゴ）を付ける。

見た目は YAML（standard.yaml など）で決める。出力は元の写真と同じフォルダの下の
output.dir_name フォルダに、output.prefix + 元のファイル名 で保存する。

単独でも実行できる:
    python -m modules.frame_processor <画像ファイルまたはフォルダ> [YAMLのパス]
"""

import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

if __package__ in (None, ""):
    # python modules/frame_processor.py のようにファイルを直接実行すると、下の相対 import が失敗するため
    sys.exit("このファイルは、main.py のあるフォルダで python -m modules.frame_processor として実行してください")

from . import console, constants
from .config_manager import APP_DIR, YamlConfigManager, resolve_relative_path
from .exif_utils import ExifReader, PhotoMetadata
from .exiftool_client import path_key
from .logging_config import ON_SCREEN, setup_logging

logger = logging.getLogger(__name__)

#: 並列処理の既定のプロセス数の上限。1枚あたり数百MBのメモリを使うため、CPU が多くてもこれ以上は増やさない
DEFAULT_MAX_WORKERS = 4


class _SafeDict(dict):
    """テンプレートに未知の項目（例: {lens_serial}）があっても例外にせず空文字にする辞書。"""

    def __missing__(self, key):
        logger.warning("テンプレートに未知の項目があります: {%s}（空文字として扱います）", key)
        return ""


@lru_cache(maxsize=64)
def _load_font(font_list: Tuple[str, ...], size: int) -> Tuple[ImageFont.FreeTypeFont, int]:
    """候補を順に試して最初に読み込めたフォントと、その候補の番号を返す。

    同じフォント・サイズを写真ごと・行ごとに読み直さないよう、結果をキャッシュする。

    Raises:
        FileNotFoundError: どの候補も読み込めない場合
    """
    for i, font_name in enumerate(font_list):
        try:
            return ImageFont.truetype(font_name, size), i
        except OSError:
            logger.debug("フォント読み込み失敗: %s", font_name)
    raise FileNotFoundError(f"指定されたフォントが見つかりません: {list(font_list)}")


def _default_workers() -> int:
    return max(1, min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1))


class ImageProcessor:
    """写真にフレームと撮影データを付けるクラス。"""

    def __init__(self, config_path: Path, app_config=None) -> None:
        """
        Args:
            config_path: フレームのレイアウト設定（YAML）のパス
            app_config: ConfigManager（config.json）。exiftool のパス、対象拡張子、
                並列数（frame.workers）に使う。None なら既定値
        """
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            raise FileNotFoundError(constants.ERROR_MESSAGES["config_not_found"].format(config_path))
        self.config = YamlConfigManager(self.config_path)
        self.app_config = app_config
        # exiftool はメタデータを読むとき（run）だけ必要。並列処理用のプロセスでは作らない
        self._exif_reader: Optional[ExifReader] = None
        # 代わりのフォントを使った旨の警告を出したフォント（写真ごとに同じ警告を出さないため）
        self._warned_fonts: set = set()
        # 直前の run で、フレーム付き画像が作成済みのため飛ばした写真の数
        self.last_skipped = 0
        logger.info("ImageProcessor 初期化完了: config=%s", config_path)

    @property
    def exif_reader(self) -> ExifReader:
        if self._exif_reader is None:
            self._exif_reader = ExifReader(config=self.app_config)
        return self._exif_reader

    # ------------------------------------------------------------------
    # 設定値の読み取り
    # ------------------------------------------------------------------
    def _section(self, name: str):
        """YAML のトップレベルのセクション。無ければ None。"""
        return getattr(self.config, name, None)

    @staticmethod
    def _opt(section, key: str, default=None):
        """セクション内の任意の項目。セクション自体が None でも default を返す。"""
        if section is None:
            return default
        return getattr(section, key, default)

    @staticmethod
    def _to_px(value, long_side: int, default: int = 0) -> int:
        """長さの設定値をピクセル数にする。

        - 数値（例: 0.02）/ 文字列 "0.02": 写真の長辺に対する比率
        - 文字列 "120px": ピクセル数
        """
        if value is None:
            return default
        if isinstance(value, str):
            v = value.strip().lower()
            if v.endswith("px"):
                return int(float(v[:-2]))
            return int(long_side * float(v))
        return int(long_side * float(value))

    def _calculate_font_size(self, long_side: int, config_val) -> int:
        """文字サイズ（px）。"40px" なら px、数値なら fonts.size_type に従う（"px" 以外は長辺に対する比率）。"""
        if isinstance(config_val, str) and config_val.strip().lower().endswith("px"):
            return int(float(config_val.strip()[:-2]))
        size_type = self._opt(self._section("fonts"), "size_type", "ratio")
        if size_type == "px":
            return int(config_val)
        return int(long_side * float(config_val))

    def _get_font(self, font_list: List[str], size: int) -> ImageFont.FreeTypeFont:
        """候補の中で最初に読み込めたフォント。1番目の候補が使えなかった場合は1回だけ警告する。"""
        try:
            font, used = _load_font(tuple(font_list), size)
        except FileNotFoundError as e:
            logger.error("%s", e)
            raise
        if used > 0 and font_list[0] not in self._warned_fonts:
            # 代わりのフォントでは見た目が変わるため知らせる
            self._warned_fonts.add(font_list[0])
            logger.warning("フォント %s が見つからないため %s を使用します", font_list[0], font_list[used])
        return font

    def _resolve_font_list(self, font_spec) -> List[str]:
        """行ごとの font 指定をフォント候補のリストにする。

        - "bold" / "regular" など fonts セクションのキー名 → そのリスト
        - "fonts/xxx.ttf" のようなパス → [そのパス]
        - リスト → そのまま
        """
        if isinstance(font_spec, list):
            candidates = font_spec
        else:
            named = self._opt(self._section("fonts"), str(font_spec))
            if isinstance(named, list):
                candidates = named
            elif isinstance(named, str):
                candidates = [named]
            else:
                candidates = [font_spec]
        return [self._resolve_font_entry(str(f)) for f in candidates]

    def _resolve_color(self, color_spec, default: str) -> str:
        """色指定。"main" / "sub" など colors のキー名、または "#RRGGBB" などの色。"""
        if color_spec is None:
            return default
        named = self._opt(self._section("colors"), str(color_spec))
        return named if isinstance(named, str) else str(color_spec)

    def _resolve_path(self, path_str: str) -> Path:
        """相対パスを、YAML のあるフォルダ → アプリのフォルダ（main.py のある場所）の順に探す。

        カレントディレクトリは基準にしない（タスクスケジューラ等で別のフォルダから
        起動したときに、フォントやロゴが見つからなくなるのを防ぐため）。
        """
        return resolve_relative_path(path_str, [self.config_path.parent, APP_DIR])

    def _resolve_font_entry(self, font: str) -> str:
        """フォント候補1件。ファイルとして見つかればフルパスにする。

        見つからないもの（"arialbd.ttf" のような OS にインストール済みのフォント名）は
        そのまま返し、Pillow に OS のフォントフォルダから探させる。
        """
        resolved = self._resolve_path(font)
        return str(resolved) if resolved.is_file() else str(font)

    @staticmethod
    def _parse_aspect(value) -> Optional[float]:
        """"4:5" や 1.5 のような指定を 幅/高さ の比にする。未指定は None。"""
        if value in (None, "", "none", "None", False):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if ":" in text:
            w_str, h_str = text.split(":", 1)
            return float(w_str) / float(h_str)
        return float(text)

    # ------------------------------------------------------------------
    # レイアウト計算
    # ------------------------------------------------------------------
    #: scaling.basis: display で、収束しないときに打ち切る繰り返し回数
    _SCALE_MAX_ITER = 30

    def _scale_reference(self, w: int, h: int) -> int:
        """比率で書いた長さ（文字サイズ・余白・行間・ロゴなど）の基準にする長さ（px）。

        - scaling.basis: long_side（既定）… 写真の長辺。従来どおり
        - scaling.basis: display … 表示先（note など）で横写真と同じ大きさに見えるように決める

        display の考え方:
            note などは画像を「記事の幅」に合わせて縮小し、縦長の画像は さらに
            scaling.display_box（note は 幅:高さ = 4:5）の枠に収まるよう小さく表示する。
            そこで、出来上がるキャンバスの「表示上の大きさ」
                D = max(キャンバス幅, キャンバス高さ × display_box)
            が、「写真の幅が基準の長さと同じ横写真」のキャンバス幅と同じになるよう基準を決める。
            横写真ではこれは写真の幅（= 長辺）になるので、横写真の見た目は従来と変わらない。
            余白も基準に比例して変わるため、少しずつ近づけて求める。
        """
        long_side = max(w, h)
        scaling = self._section("scaling")
        if self._opt(scaling, "basis", "long_side") != "display":
            return long_side

        box = self._parse_aspect(self._opt(scaling, "display_box", constants.DEFAULT_DISPLAY_BOX))
        ref = float(w)
        for _ in range(self._SCALE_MAX_ITER):
            m = self._compute_margins(w, h, ref)
            canvas_w = w + m["left"] + m["right"]
            canvas_h = h + m["top"] + m["bottom"]
            shown = max(canvas_w, canvas_h * box) if box else canvas_w
            # aspect_ratio で足す分を除いた、左右の余白だけのキャンバス幅
            base = self._compute_margins(w, h, ref, apply_aspect=False)
            plain_w = ref + base["left"] + base["right"]
            new_ref = shown * ref / plain_w
            if abs(new_ref - ref) < 0.5:
                ref = new_ref
                break
            ref = new_ref
        return max(1, int(round(ref)))

    def _compute_margins(self, w: int, h: int, long_side, apply_aspect: bool = True) -> Dict[str, int]:
        """上下左右の余白（px）。

        1. 旧形式の ratios.side_margin（上・左・右）/ ratios.bottom_margin（下）を基準にする
        2. canvas.margins.top/bottom/left/right があれば上書きする
        3. canvas.aspect_ratio があれば、その比率になるよう余白を足す（apply_aspect=False なら足さない）

        Args:
            long_side: 比率で書いた余白の基準の長さ（_scale_reference の値）
        """
        ratios = self._section("ratios")
        side = self._to_px(self._opt(ratios, "side_margin", 0), long_side)
        bottom = self._to_px(self._opt(ratios, "bottom_margin", 0), long_side)
        m = {"top": side, "bottom": bottom, "left": side, "right": side}

        canvas = self._section("canvas")
        margins = self._opt(canvas, "margins")
        for key in m:
            val = self._opt(margins, key)
            if val is not None:
                m[key] = self._to_px(val, long_side)

        aspect = self._opt(canvas, "aspect_ratio")
        if h > w and self._opt(canvas, "aspect_ratio_portrait") is not None:
            aspect = self._opt(canvas, "aspect_ratio_portrait")
        target = self._parse_aspect(aspect) if apply_aspect else None
        if target:
            cw = w + m["left"] + m["right"]
            ch = h + m["top"] + m["bottom"]
            if cw / ch < target:
                # 横幅が足りない → 左右に均等に足す
                extra = round(ch * target) - cw
                m["left"] += extra // 2
                m["right"] += extra - extra // 2
            elif cw / ch > target:
                # 高さが足りない → aspect_fill に従って上下に足す
                extra = round(cw / target) - ch
                fill = self._opt(canvas, "aspect_fill", "center")
                if fill == "bottom":
                    m["bottom"] += extra
                elif fill == "top":
                    m["top"] += extra
                else:
                    m["top"] += extra // 2
                    m["bottom"] += extra - extra // 2
        return m

    def _line_specs(self) -> List[Dict[str, Any]]:
        """描画する行の設定。layout.lines があればそれを、なければ旧形式の top/bottom の2行を使う。"""
        layout = self._section("layout")
        fonts = self._section("fonts")
        colors = self._section("colors")
        lines = self._opt(layout, "lines")
        if lines:
            specs = []
            for i, ln in enumerate(lines):
                # 1行目は bold・main、2行目以降は regular・sub を既定にする（旧形式と同じ見た目）
                default_color = self._opt(colors, "main" if i == 0 else "sub", "#000000")
                specs.append({
                    "text": str(self._opt(ln, "text", "")),
                    "fonts": self._resolve_font_list(self._opt(ln, "font", "bold" if i == 0 else "regular")),
                    "size": self._opt(ln, "size", self._opt(fonts, "main_size" if i == 0 else "sub_size", 0.015)),
                    "color": self._resolve_color(self._opt(ln, "color"), default_color),
                    "align": self._opt(ln, "align"),
                    "spacing": self._opt(ln, "spacing"),
                })
            return specs
        return [
            {"text": layout.top, "fonts": self._resolve_font_list(list(fonts.bold)), "size": fonts.main_size,
             "color": colors.main, "align": None, "spacing": None},
            {"text": layout.bottom, "fonts": self._resolve_font_list(list(fonts.regular)), "size": fonts.sub_size,
             "color": colors.sub, "align": None, "spacing": None},
        ]

    def _line_spacing_px(self, long_side: int) -> int:
        """行の上端から次の行の上端までの距離（px）。layout.line_spacing、なければ旧形式の line_spacing_px。"""
        layout = self._section("layout")
        val = self._opt(layout, "line_spacing")
        if val is not None:
            return self._to_px(val, long_side)
        return int(self._opt(layout, "line_spacing_px", constants.DEFAULT_LINE_SPACING_PX))

    def _draw_logo(self, canvas: Image.Image, main_fs: int, long_side: int,
                   img_box: tuple, area: tuple, text_box: Optional[tuple]) -> None:
        """ロゴを描く。ロゴが無い・描けない場合もフレーム処理は続ける。

        Args:
            main_fs: 1行目の文字サイズ（ロゴの高さの基準）
            img_box: 写真の (left, top, right, bottom)
            area: 文字エリアの (top, bottom)
            text_box: 文字全体の (left, top, right, bottom)。text_left / text_right 配置に使う
        """
        cfg = self._section("logo")
        if not self._opt(cfg, "enabled", True):
            return
        logo_path = self._resolve_path(self._opt(cfg, "path", constants.LOGO_FILENAME))
        if not logo_path.exists():
            logger.debug("ロゴファイルが見つかりません: %s", logo_path)
            return
        try:
            logo = Image.open(logo_path).convert("RGBA")
            if self._opt(cfg, "height") is not None:
                logo_h = self._to_px(self._opt(cfg, "height"), long_side)
            else:
                logo_h = int(main_fs * float(self._opt(cfg, "height_font_ratio", constants.LOGO_SIZE_RATIO)))
            if logo_h <= 0:
                return
            logo_w = int(logo.width * (logo_h / logo.height))
            logo = logo.resize((logo_w, logo_h), Image.Resampling.LANCZOS)

            opacity = float(self._opt(cfg, "opacity", 1.0))
            if opacity < 1.0:
                alpha = logo.getchannel("A").point(lambda a: int(a * max(opacity, 0.0)))
                logo.putalpha(alpha)

            position = self._opt(cfg, "position", "left")
            off_x = self._to_px(self._opt(cfg, "offset_x", f"{constants.DEFAULT_LOGO_LEFT_OFFSET}px"), long_side)
            off_y = self._to_px(self._opt(cfg, "offset_y", f"{constants.DEFAULT_LOGO_TOP_OFFSET}px"), long_side)
            gap = self._to_px(self._opt(cfg, "gap", 0.01), long_side)
            img_l, _, img_r, _ = img_box

            if position == "right":
                x = img_r - logo_w - off_x
            elif position == "center":
                x = (img_l + img_r - logo_w) // 2 + off_x
            elif position == "text_left" and text_box:
                x = text_box[0] - gap - logo_w + off_x
            elif position == "text_right" and text_box:
                x = text_box[2] + gap + off_x
            else:  # left（既定）
                x = img_l + off_x

            # 文字の隣に置く場合は、既定で文字ブロックの縦中央にそろえる
            default_v = "text" if position in ("text_left", "text_right") else "bottom"
            vertical = self._opt(cfg, "vertical", default_v)
            area_top, area_bottom = area
            if vertical == "top":
                y = area_top + off_y
            elif vertical == "center":
                y = (area_top + area_bottom - logo_h) // 2 + off_y
            elif vertical == "text" and text_box:
                y = (text_box[1] + text_box[3] - logo_h) // 2 + off_y
            else:  # bottom（既定）: 文字エリアの下端から off_y 上
                y = area_bottom - logo_h - off_y

            # logo.left / logo.top はキャンバス左上からの座標で、position / vertical より優先する。
            # 片方だけ書いた場合、もう片方は上で計算した位置を使う
            if self._opt(cfg, "left") is not None:
                x = self._to_px(self._opt(cfg, "left"), long_side)
            if self._opt(cfg, "top") is not None:
                y = self._to_px(self._opt(cfg, "top"), long_side)

            canvas.paste(logo, (x, y), logo)
            logger.debug("ロゴ描画完了: x=%d y=%d (position=%s vertical=%s)", x, y, position, vertical)
        except Exception as e:
            logger.warning("ロゴ描画失敗: %s", e)

    # ------------------------------------------------------------------
    # 描画・保存
    # ------------------------------------------------------------------
    def render(self, path: Path, meta: PhotoMetadata) -> Path:
        """1枚にフレームを付けて保存し、出力ファイルのパスを返す。失敗したら例外を送出する。"""
        with Image.open(path) as img:
            # 色空間（Adobe RGB / Display P3 など）を出力にも引き継ぐため、ICC プロファイルを控えておく。
            # RGB 以外（CMYK・グレースケール等）は RGB に変換するため、元のプロファイルは使えない
            icc_profile = img.info.get("icc_profile") if img.mode in ("RGB", "RGBA", "RGBX") else None
            img = ImageOps.exif_transpose(img).convert("RGB")

        meta_dict = _SafeDict(asdict(meta))
        w, h = img.size
        # 比率で書いた長さの基準。scaling.basis が既定（long_side）なら写真の長辺
        long_side = self._scale_reference(w, h)
        if long_side != max(w, h):
            logger.debug("基準の長さ: %d px（長辺 %d px、%s）", long_side, max(w, h), path.name)

        # ---- キャンバス（余白） ----
        m = self._compute_margins(w, h, long_side)
        canvas_w = w + m["left"] + m["right"]
        canvas_h = h + m["top"] + m["bottom"]
        canvas = Image.new("RGB", (canvas_w, canvas_h), self.config.colors.bg)
        canvas.paste(img, (m["left"], m["top"]))
        draw = ImageDraw.Draw(canvas)
        img_box = (m["left"], m["top"], m["left"] + w, m["top"] + h)

        layout = self._section("layout")
        if self._opt(layout, "text_area", "bottom") == "top":
            area = (0, m["top"])
        else:
            area = (m["top"] + h, canvas_h)
        area_h = area[1] - area[0]

        # ---- 各行の文字・フォント・1行目からの縦位置 ----
        specs = self._line_specs()
        default_spacing = self._line_spacing_px(long_side)
        rows = []
        y_rel = 0
        for i, sp in enumerate(specs):
            fs = self._calculate_font_size(long_side, sp["size"])
            font = self._get_font(sp["fonts"], fs)
            text = str(sp["text"]).format_map(meta_dict)
            if i > 0:
                y_rel += (self._to_px(sp["spacing"], long_side)
                          if sp["spacing"] is not None else default_spacing)
            rows.append({"font": font, "fs": fs, "text": text, "color": sp["color"],
                         "align": sp["align"], "y_rel": y_rel})

        # ---- 文字ブロックの縦位置 ----
        y_offset = int(self._opt(layout, "y_offset_px", 0))
        if self._opt(layout, "y_offset") is not None:
            y_offset = self._to_px(self._opt(layout, "y_offset"), long_side)
        if self._opt(layout, "vertical_align", "padding") == "center":
            # 実際の文字の上端〜下端を文字エリアの中央に置く（フォントごとの余白の差を吸収するため）
            tops = [draw.textbbox((0, r["y_rel"]), r["text"], font=r["font"])[1] for r in rows]
            bottoms = [draw.textbbox((0, r["y_rel"]), r["text"], font=r["font"])[3] for r in rows]
            block_top, block_bottom = min(tops), max(bottoms)
            base_y = area[0] + (area_h - (block_bottom - block_top)) // 2 - block_top + y_offset
        else:
            top_padding_ratio = float(self._opt(layout, "top_padding_ratio", constants.DEFAULT_TOP_PADDING_RATIO))
            base_y = area[0] + int(area_h * top_padding_ratio) + y_offset

        # ---- 横位置 ----
        default_align = self._opt(layout, "align", "center")
        pad_x = self._to_px(self._opt(layout, "padding_x", 0), long_side)
        if self._opt(layout, "align_to", "image") == "canvas":
            ref_l, ref_r = 0, canvas_w
        else:
            ref_l, ref_r = img_box[0], img_box[2]

        for r in rows:
            r["y"] = base_y + r["y_rel"]
            tw = draw.textbbox((0, 0), r["text"], font=r["font"])[2]
            align = r["align"] or default_align
            if align == "left":
                r["x"] = ref_l + pad_x
            elif align == "right":
                r["x"] = ref_r - pad_x - tw
            else:
                r["x"] = ref_l + (ref_r - ref_l - tw) // 2

        text_box = None
        boxes = [draw.textbbox((r["x"], r["y"]), r["text"], font=r["font"]) for r in rows if r["text"]]
        if boxes:
            text_box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                        max(b[2] for b in boxes), max(b[3] for b in boxes))

        # ロゴを先に描き、文字が前面に来るようにする
        main_fs = rows[0]["fs"] if rows else 0
        self._draw_logo(canvas, main_fs, long_side, img_box, area, text_box)
        for r in rows:
            draw.text((r["x"], r["y"]), r["text"], fill=r["color"], font=r["font"])

        # ---- 保存 ----
        out_path = self.output_path(path)
        out_path.parent.mkdir(exist_ok=True, parents=True)
        save_options = {
            "quality": getattr(self.config.output, "quality", constants.JPEG_QUALITY),
            "subsampling": getattr(self.config.output, "subsampling", constants.JPEG_SUBSAMPLING),
        }
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        canvas.save(out_path, "JPEG", **save_options)
        return out_path

    def _process_one(self, path: Path, meta: PhotoMetadata, current: int, total: int, events: "FrameEvents") -> bool:
        """1枚を処理して結果を通知する。失敗しても例外は出さず False を返す（残りの写真を続けるため）。"""
        try:
            out_path = self.render(path, meta)
        except Exception as e:
            _notify_failed(events, path, e)
            return False
        _notify_done(events, path, out_path, current, total)
        return True

    # ------------------------------------------------------------------
    # 一括処理
    # ------------------------------------------------------------------
    def _target_files(self, target: Path) -> List[Path]:
        """処理対象の写真（ファイル1つ、またはフォルダ直下の JPEG・PNG）。

        RAW（NEF など）は Pillow で開くと埋め込みの小さなプレビュー画像などになり、
        まともなフレーム画像を作れないため対象にしない。
        """
        if not target.exists():
            raise FileNotFoundError(constants.ERROR_MESSAGES["directory_not_found"].format(target))
        files = [target] if target.is_file() else [f for f in target.iterdir() if f.is_file()]

        extensions = set(constants.JPEG_EXTENSIONS)
        if self.app_config is not None:
            extensions = set(self.app_config.get_file_extensions()["jpeg"])
        extensions |= constants.FRAME_EXTRA_EXTENSIONS
        return [f for f in files if f.suffix.lower() in extensions]

    def output_path(self, path: Path) -> Path:
        """写真 path のフレーム付き画像の保存先（元の写真と同じフォルダの output.dir_name の中）。"""
        return path.parent / self.config.output.dir_name / f"{self.config.output.prefix}{path.stem}.jpg"

    def _overwrite_existing(self) -> bool:
        """作成済みのフレーム付き画像も作り直すか。config.json の frame.overwrite_existing（既定 false）。"""
        if self.app_config is None:
            return False
        return bool(self.app_config.get("frame.overwrite_existing", False))

    def is_up_to_date(self, path: Path) -> bool:
        """フレーム付き画像が既にあり、元の写真とレイアウト設定（YAML）のどちらよりも新しいか。

        to_note には前回までの写真も残っているため、作成済みのものを毎回作り直さないよう飛ばす。
        YAML を書き換えた後は、YAML の方が新しくなるので自動で作り直す。
        """
        out_path = self.output_path(path)
        try:
            made = out_path.stat().st_mtime
            return made >= path.stat().st_mtime and made >= self.config_path.stat().st_mtime
        except OSError:
            return False

    def _workers(self) -> int:
        """並列に処理するプロセス数。config.json の frame.workers（1 なら並列にしない）。"""
        value = self.app_config.get("frame.workers") if self.app_config is not None else None
        if value in (None, ""):
            return _default_workers()
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            logger.warning("frame.workers の値が不正なため既定値を使います: %r", value)
            return _default_workers()

    def run(self, target: Path, workers: Optional[int] = None, events: Optional["FrameEvents"] = None) -> int:
        """target の写真すべてにフレームを付け、成功した枚数を返す。

        フレーム付き画像が作成済みの写真は飛ばす（is_up_to_date を参照）。飛ばした枚数は last_skipped に入る。

        Args:
            workers: 並列に処理するプロセス数。None なら _workers() の値
            events: 進み具合などの通知先。None なら通知しない
        """
        events = events or FrameEvents()
        valid_files = self._target_files(target)
        self.last_skipped = 0
        if valid_files and not self._overwrite_existing():
            skipped = [f for f in valid_files if self.is_up_to_date(f)]
            if skipped:
                self.last_skipped = len(skipped)
                logger.info("フレーム付き画像が作成済みのため飛ばす写真: %d 枚", len(skipped))
                events.skipped_existing(skipped)
                skipped_set = set(skipped)
                valid_files = [f for f in valid_files if f not in skipped_set]
                if not valid_files:
                    return 0
        if not valid_files:
            logger.warning("有効な画像ファイルが見つかりません: %s", target, extra=ON_SCREEN)
            events.no_files(target)
            return 0

        # exiftool の起動回数を減らすため、メタデータは1回でまとめて取得する
        logger.info("メタデータ一括取得開始: %d ファイル", len(valid_files))
        events.reading_metadata(valid_files)
        metadata_map = self.exif_reader.get_photo_metadata_batch(valid_files)

        jobs: List[Tuple[Path, PhotoMetadata]] = []
        for file_path in valid_files:
            meta = metadata_map.get(path_key(file_path))
            if not meta:
                logger.warning("メタデータが取得できません: %s", file_path.name, extra=ON_SCREEN)
                events.no_metadata(file_path)
                continue
            jobs.append((file_path, meta))

        total = len(jobs)
        workers = workers or self._workers()
        if workers > 1 and len(jobs) > 1:
            success_count = self._run_parallel(jobs, total, min(workers, len(jobs)), events)
        else:
            success_count = sum(
                self._process_one(p, meta, i, total, events) for i, (p, meta) in enumerate(jobs, 1)
            )

        logger.info("画像処理完了: %d/%d ファイル", success_count, len(valid_files))
        return success_count

    def _run_parallel(self, jobs: List[Tuple[Path, PhotoMetadata]], total: int, workers: int,
                      events: "FrameEvents") -> int:
        """複数のプロセスで並列に処理する。

        プロセスを起動できない・途中で落ちた場合は、終わっていない写真を1枚ずつの処理でやり直す。
        """
        logger.info("フレーム付与を %d プロセスで並列実行します", workers)
        success_count = 0
        done = 0
        retry: List[Tuple[Path, PhotoMetadata]] = []
        pending = dict(enumerate(jobs))
        try:
            with ProcessPoolExecutor(
                max_workers=workers, initializer=_init_worker, initargs=(str(self.config_path),)
            ) as pool:
                futures = {pool.submit(_render_in_worker, p, meta): i for i, (p, meta) in pending.items()}
                for future in as_completed(futures):
                    path, meta = pending.pop(futures[future])
                    try:
                        out_path = future.result()
                    except BrokenProcessPool:
                        retry.append((path, meta))
                        continue
                    except Exception as e:
                        done += 1
                        _notify_failed(events, path, e)
                        continue
                    done += 1
                    _notify_done(events, path, out_path, done, total)
                    success_count += 1
        except Exception as e:
            logger.warning("並列処理を続けられないため、残りを1枚ずつ処理します: %s", e)
        retry.extend(pending.values())

        if retry:
            logger.warning("並列処理で終わらなかった %d 枚を1枚ずつ処理します", len(retry))
            for p, meta in retry:
                done += 1
                success_count += self._process_one(p, meta, done, total, events)
        return success_count


class FrameEvents:
    """一括処理（ImageProcessor.run）の進み具合の通知先。既定では何もしない。

    画面に表示する場合はこのクラスを継承して、必要なメソッドだけを上書きする（_ConsoleFrameEvents を参照）。
    """

    def no_files(self, target: Path) -> None:
        """対象の写真が1枚も無かった。"""

    def skipped_existing(self, files: List[Path]) -> None:
        """フレーム付き画像が作成済みのため、これらの写真は飛ばす。"""

    def reading_metadata(self, files: List[Path]) -> None:
        """対象の写真（files）が決まり、メタデータの一括取得を始める。"""

    def no_metadata(self, path: Path) -> None:
        """この写真のメタデータを取得できなかったため、処理しない。"""

    def done(self, path: Path, out_path: Path, current: int, total: int) -> None:
        """1枚にフレームを付けた（current 枚目 / total 枚）。"""

    def failed(self, path: Path, error: Exception) -> None:
        """1枚の処理に失敗した。"""


def _notify_done(events: FrameEvents, path: Path, out_path: Path, current: int, total: int) -> None:
    events.done(path, out_path, current, total)
    logger.info("画像処理完了: %s -> %s", path.name, out_path)


def _notify_failed(events: FrameEvents, path: Path, error: Exception) -> None:
    logger.error("画像処理エラー (%s): %s", path.name, error, exc_info=error, extra=ON_SCREEN)
    events.failed(path, error)


# 並列処理用のプロセスごとに1つだけ作る ImageProcessor（YAML の読み込みを写真ごとに繰り返さないため）
_worker_processor: Optional[ImageProcessor] = None


def _init_worker(config_path: str) -> None:
    global _worker_processor
    # 並列処理用のプロセスでも、警告（フォントの代わりなど）を同じログ・画面の形式で出す
    setup_logging()
    _worker_processor = ImageProcessor(Path(config_path))


def _render_in_worker(path: Path, meta: PhotoMetadata) -> Path:
    assert _worker_processor is not None
    return _worker_processor.render(path, meta)


# ------------------------------------------------------------------
# ステップの入口（画面表示つき）
# ------------------------------------------------------------------
TITLE = "フレーム付与処理"


class _ConsoleFrameEvents(FrameEvents):
    """一括処理の進み具合を画面に表示する（写真1枚ごと）。"""

    def __init__(self) -> None:
        self.progress: Optional[console.Progress] = None

    def no_files(self, target: Path) -> None:
        console.message("有効な画像ファイルが見つかりません。")

    def skipped_existing(self, files: List[Path]) -> None:
        console.message(f"フレーム付き画像が作成済みの {len(files)} 枚は飛ばします。")

    def reading_metadata(self, files: List[Path]) -> None:
        console.message(f"メタデータを読み取り中...（{len(files)} 枚）")
        self.progress = console.Progress("フレームを付与", len(files), [p.name for p in files])

    def no_metadata(self, path: Path) -> None:
        if self.progress:
            self.progress.item(path.name, "メタデータが取得できないためスキップ", problem=True)

    def done(self, path: Path, out_path: Path, current: int, total: int) -> None:
        if self.progress:
            self.progress.item(path.name, f"完了 → {out_path.name}")

    def failed(self, path: Path, error: Exception) -> None:
        if self.progress:
            self.progress.item(path.name, f"失敗（{error}）", problem=True)

    def finish(self) -> None:
        if self.progress:
            self.progress.finish()

    def abort(self) -> None:
        if self.progress:
            self.progress.abort()


def run_frame_processing(target_dir: Path, yaml_config: str, app_config=None) -> bool:
    """フレーム付与処理の入口。進み具合とまとめを画面に表示し、1枚以上フレームを付けられたら True。

    Args:
        target_dir: 写真のあるフォルダ（直下のファイルだけを処理する）
        yaml_config: フレームのレイアウト設定（YAML）のパス
        app_config: ConfigManager（config.json）
    """
    started = console.step_start(TITLE)

    events = _ConsoleFrameEvents()
    try:
        processor = ImageProcessor(Path(yaml_config).resolve(), app_config=app_config)
        try:
            success_count = processor.run(target_dir, events=events)
        except BaseException:
            events.abort()
            raise
        events.finish()

        console.summary_start()
        console.item(f"フレーム付与完了: {success_count} ファイル")
        if processor.last_skipped:
            console.item(f"作成済みのため飛ばした写真: {processor.last_skipped} 枚"
                         "（すべて作り直す場合は config.json の frame.overwrite_existing を true に）")
        console.step_end(TITLE, started)
        # 新しく作る写真が無く、すべて作成済みだった場合も、やるべきことは済んでいるので成功とする
        return success_count > 0 or processor.last_skipped > 0

    except FileNotFoundError as e:
        logger.error("ファイル不見エラー: %s", e, extra=ON_SCREEN)
        console.error(str(e))
        return False
    except Exception as e:
        logger.error("フレーム処理エラー: %s", e, exc_info=True, extra=ON_SCREEN)
        console.error(f"フレーム処理中に予期しないエラーが発生しました: {e}", blank_line_before=True)
        return False


if __name__ == "__main__":
    setup_logging()

    if len(sys.argv) < 2:
        print("使用法: python -m modules.frame_processor <image_path_or_dir> [config_path]")
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    conf_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else APP_DIR / "standard.yaml"

    try:
        cli_events = _ConsoleFrameEvents()
        try:
            ImageProcessor(conf_path).run(input_path, events=cli_events)
        except BaseException:
            cli_events.abort()
            raise
        cli_events.finish()
    except Exception as e:
        logger.error("エラー: %s", e, exc_info=True)
        print(f"エラー: {e}")
        sys.exit(1)
