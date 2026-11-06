"""STEP 3: フレーム付与処理モジュール (modules/frame_processor.py)."""

# 標準ライブラリのインポート
import logging       # ログ出力用
import sys            # コマンドライン引数・終了コード制御用
import os              # パス正規化(normpath)に使用
from pathlib import Path       # ファイル・ディレクトリパス操作
from dataclasses import asdict  # dataclass(PhotoMetadata)を辞書に変換するために使用
from typing import List, Optional

# 画像処理ライブラリ Pillow から必要なモジュールをインポート
# Image: 画像の読み込み・生成・保存, ImageDraw: 図形/文字の描画,
# ImageFont: フォント読み込み, ImageOps: EXIF Orientationに基づく自動回転(exif_transpose)
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    # パッケージとして実行された場合（本来の使われ方）
    # 相対importなので、`modules`パッケージの一部としてimportされたときに成功する
    from .exif_utils import PhotoMetadata, ExifReader
    from .config_manager import YamlConfigManager
    from .logging_config import setup_logging
    from . import constants
except ImportError:
    # python frame_processor.py として単独実行された場合のフォールバック
    # （manual_gps_tagger.py / exif_exporter.py と同じパターン）
    # 相対importが使えない（パッケージの外から直接実行された）場合は、
    # 同じディレクトリにあるモジュールとして絶対importし直す
    from exif_utils import PhotoMetadata, ExifReader
    from config_manager import YamlConfigManager
    from logging_config import setup_logging
    import constants

# このモジュール用のロガーを取得（main.py側でsetup_logging()済みならその設定を引き継ぐ）
logger = logging.getLogger(__name__)


class _SafeDict(dict):
    """テンプレートに未知のキー（例: {lens_serial}）があっても例外にせず空文字にする辞書。"""

    def __missing__(self, key):
        logger.warning("テンプレートに未知の項目があります: {%s}（空文字として扱います）", key)
        return ""


class ImageProcessor:
    """画像にフレームとEXIF情報を付与するクラス。"""

    def __init__(self, config_path: Path, app_config=None) -> None:
        """初期化。

        Args:
            config_path: YAML形式の設定ファイルパス（フレームのレイアウト設定）
            app_config: ConfigManager（config.json）。exiftoolのパス解決に使用
        """
        # YAML設定ファイルのパスを保持し、存在チェック
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            # 存在しない場合はconstants.pyの定型メッセージを使ってエラーを送出
            raise FileNotFoundError(constants.ERROR_MESSAGES["config_not_found"].format(config_path))

        # YAML設定（フォント/色/レイアウト/出力設定など）を読み込む
        self.config = YamlConfigManager(self.config_path)
        # config.json由来のConfigManager（exiftoolパス解決などに使用、Noneの場合もある）
        self.app_config = app_config
        # EXIF読み取り用のヘルパー（メタデータのバッチ取得に使用）
        self.exif_reader = ExifReader(config=app_config)
        # ロゴ画像ファイルのパス（constants.pyで定義されたファイル名）
        self.logo_path = Path(constants.LOGO_FILENAME)
        logger.info("ImageProcessor 初期化完了: config=%s", config_path)

    # ------------------------------------------------------------------
    # 設定値の読み取りヘルパー
    # ------------------------------------------------------------------
    def _section(self, name: str):
        """YAMLのトップレベルセクションを取得（未設定ならNone）。"""
        return getattr(self.config, name, None)

    @staticmethod
    def _opt(section, key: str, default=None):
        """セクション内の任意キーを取得（セクション自体がNoneでも安全）。"""
        if section is None:
            return default
        return getattr(section, key, default)

    @staticmethod
    def _to_px(value, long_side: int, default: int = 0) -> int:
        """長さの設定値をピクセル数に変換する。

        - 数値（例: 0.02）      : 画像長辺に対する比率
        - 文字列 "120px"        : ピクセル数で直接指定
        - 文字列 "0.02"         : 比率（数値と同じ扱い）
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
        """フォントサイズを計算。

        "40px" のような文字列なら px 指定、数値なら fonts.size_type に従う
        （"px" ならそのままピクセル、それ以外は長辺に対する比率）。
        """
        if isinstance(config_val, str) and config_val.strip().lower().endswith("px"):
            return int(float(config_val.strip()[:-2]))
        size_type = self._opt(self._section("fonts"), "size_type", "ratio")
        if size_type == "px":
            return int(config_val)
        return int(long_side * float(config_val))

    def _get_font(self, font_list: List[str], size: int) -> ImageFont.FreeTypeFont:
        """フォントを取得。"""
        # 候補のフォント名を順番に試し、最初に読み込めたものを返す（フォールバック方式）
        for font_name in font_list:
            try:
                return ImageFont.truetype(font_name, size)
            except OSError:
                # そのフォントが見つからない/読み込めない場合は次の候補を試す
                logger.debug("フォント読み込み失敗: %s", font_name)
                continue
        # すべての候補が失敗した場合はエラーとして送出
        error_msg = f"指定されたフォントが見つかりません: {font_list}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    def _resolve_font_list(self, font_spec) -> List[str]:
        """行ごとの font 指定を、フォント候補リストに変換する。

        - "bold" / "regular" など fonts セクションのキー名 → そのリスト
        - "fonts/xxx.ttf" のようなパス文字列 → [そのパス]
        - リスト → そのまま
        """
        if isinstance(font_spec, list):
            return [str(f) for f in font_spec]
        fonts = self._section("fonts")
        named = self._opt(fonts, str(font_spec))
        if isinstance(named, list):
            return [str(f) for f in named]
        if isinstance(named, str):
            return [named]
        return [str(font_spec)]

    def _resolve_color(self, color_spec, default: str) -> str:
        """色指定を解決する。"main" / "sub" など colors のキー名、またはHEX等の色文字列。"""
        if color_spec is None:
            return default
        named = self._opt(self._section("colors"), str(color_spec))
        return named if isinstance(named, str) else str(color_spec)

    def _resolve_path(self, path_str: str) -> Path:
        """相対パスを、カレントディレクトリ → YAMLファイルのあるフォルダの順に探す。"""
        p = Path(path_str)
        if p.is_absolute() or p.exists():
            return p
        alt = self.config_path.parent / p
        return alt if alt.exists() else p

    @staticmethod
    def _parse_aspect(value) -> Optional[float]:
        """"4:5" や "1:1"、1.5 のような指定を 幅/高さ の比に変換する（未指定はNone）。"""
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
    def _compute_margins(self, w: int, h: int, long_side: int) -> dict:
        """上下左右の余白（px）を計算する。

        1. 旧来の ratios.side_margin（上・左・右）/ ratios.bottom_margin（下）を基準にする
        2. canvas.margins.top/bottom/left/right があればそれで上書き
        3. canvas.aspect_ratio があれば、その比率になるよう余白を追加する
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

        # 縦長写真用の比率が指定されていれば縦長のときはそちらを使う
        aspect = self._opt(canvas, "aspect_ratio")
        if h > w and self._opt(canvas, "aspect_ratio_portrait") is not None:
            aspect = self._opt(canvas, "aspect_ratio_portrait")
        target = self._parse_aspect(aspect)
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

    def _line_specs(self) -> list:
        """描画する行の設定リストを返す。

        layout.lines があればそれを使い、なければ旧来の top/bottom の2行構成を組み立てる。
        """
        layout = self._section("layout")
        fonts = self._section("fonts")
        colors = self._section("colors")
        lines = self._opt(layout, "lines")
        if lines:
            specs = []
            for i, ln in enumerate(lines):
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
        # 旧形式（top / bottom の2行）
        return [
            {"text": layout.top, "fonts": list(fonts.bold), "size": fonts.main_size,
             "color": colors.main, "align": None, "spacing": None},
            {"text": layout.bottom, "fonts": list(fonts.regular), "size": fonts.sub_size,
             "color": colors.sub, "align": None, "spacing": None},
        ]

    def _line_spacing_px(self, long_side: int) -> int:
        """行の上端から次の行の上端までの距離（px）。

        layout.line_spacing（比率 or "px"）を優先し、なければ旧来の line_spacing_px。
        """
        layout = self._section("layout")
        val = self._opt(layout, "line_spacing")
        if val is not None:
            return self._to_px(val, long_side)
        return int(self._opt(layout, "line_spacing_px", 40))

    def _draw_logo(self, canvas: Image.Image, main_fs: int, long_side: int,
                   img_box: tuple, area: tuple, text_box: Optional[tuple]) -> None:
        """ロゴを描画。

        Args:
            main_fs: 1行目のフォントサイズ（ロゴの高さの基準）
            img_box: 写真の (left, top, right, bottom)
            area: 文字エリアの (top, bottom)
            text_box: 描画した文字全体の (left, top, right, bottom)。text_left/text_right 配置に使う
        """
        cfg = self._section("logo")
        if not self._opt(cfg, "enabled", True):
            return
        logo_path = self._resolve_path(self._opt(cfg, "path", constants.LOGO_FILENAME))
        # ロゴファイルが存在しなければ何もせず終了（ロゴなしでも処理は継続できる）
        if not logo_path.exists():
            logger.debug("ロゴファイルが見つかりません: %s", logo_path)
            return
        try:
            logo = Image.open(logo_path).convert("RGBA")
            # 高さ: logo.height（比率 or px）指定があればそれ、なければ1行目フォントサイズ×倍率
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

            # 横位置
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

            # 縦位置（文字エリア内）。text_left/right の既定は文字ブロックの中央
            default_v = "text" if position in ("text_left", "text_right") else "bottom"
            vertical = self._opt(cfg, "vertical", default_v)
            area_top, area_bottom = area
            if vertical == "top":
                y = area_top + off_y
            elif vertical == "center":
                y = (area_top + area_bottom - logo_h) // 2 + off_y
            elif vertical == "text" and text_box:
                y = (text_box[1] + text_box[3] - logo_h) // 2 + off_y
            else:  # bottom（既定）: 文字エリア下端から off_y 上
                y = area_bottom - logo_h - off_y

            canvas.paste(logo, (x, y), logo)
            logger.debug("ロゴ描画完了: position=%s vertical=%s", position, vertical)
        except Exception as e:
            # ロゴ描画に失敗しても致命的ではないため、警告のみでフレーム処理自体は継続する
            logger.warning("ロゴ描画失敗: %s", e)

    def process_image(self, path: Path, meta: PhotoMetadata, current: int, total: int) -> bool:
        """画像を処理してフレーム付与。"""
        try:
            # 進捗表示用の文字列を作成（[ XX%] (現在/合計) ファイル名）
            progress_pct = int((current / total) * 100)
            progress_str = f"[{progress_pct:3d}%] ({current}/{total}) {path.name}"

            # 画像を開き、EXIFのOrientation情報に基づいて向きを正しく補正した上でRGBに変換
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")

            # テンプレートに存在しないキーがあっても落ちないよう、欠けた値は空文字にする
            meta_dict = _SafeDict(asdict(meta))
            w, h = img.size
            # 長辺（縦横どちらか大きい方）を基準にフォントサイズやマージンを計算する
            long_side = max(w, h)

            # ---- キャンバス（余白） ----
            m = self._compute_margins(w, h, long_side)
            canvas_w = w + m["left"] + m["right"]
            canvas_h = h + m["top"] + m["bottom"]
            canvas = Image.new("RGB", (canvas_w, canvas_h), self.config.colors.bg)
            canvas.paste(img, (m["left"], m["top"]))
            draw = ImageDraw.Draw(canvas)
            img_box = (m["left"], m["top"], m["left"] + w, m["top"] + h)

            layout = self._section("layout")
            # 文字を置くエリア: 下余白（既定）または上余白
            if self._opt(layout, "text_area", "bottom") == "top":
                area = (0, m["top"])
            else:
                area = (m["top"] + h, canvas_h)
            area_h = area[1] - area[0]

            # ---- 各行のフォント・テキスト・縦位置 ----
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

            # 文字ブロックの縦位置
            y_offset = int(self._opt(layout, "y_offset_px", 0))
            if self._opt(layout, "y_offset") is not None:
                y_offset = self._to_px(self._opt(layout, "y_offset"), long_side)
            if self._opt(layout, "vertical_align", "padding") == "center":
                # ブロック全体（実際の文字の上端〜下端）を文字エリアの中央に置く
                tops = [draw.textbbox((0, r["y_rel"]), r["text"], font=r["font"])[1] for r in rows]
                bottoms = [draw.textbbox((0, r["y_rel"]), r["text"], font=r["font"])[3] for r in rows]
                block_top, block_bottom = min(tops), max(bottoms)
                base_y = area[0] + (area_h - (block_bottom - block_top)) // 2 - block_top + y_offset
            else:
                # 旧来どおり: 文字エリア上端から top_padding_ratio の位置に1行目
                top_padding_ratio = float(self._opt(layout, "top_padding_ratio", 0.20))
                base_y = area[0] + int(area_h * top_padding_ratio) + y_offset

            # ---- 横位置 ----
            default_align = self._opt(layout, "align", "center")
            align_to = self._opt(layout, "align_to", "image")
            pad_x = self._to_px(self._opt(layout, "padding_x", 0), long_side)
            if align_to == "canvas":
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

            # ロゴの text_left/right 配置用に、文字全体の外枠を求める
            text_box = None
            boxes = [draw.textbbox((r["x"], r["y"]), r["text"], font=r["font"]) for r in rows if r["text"]]
            if boxes:
                text_box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes))

            # ロゴを描画（文字より先に描くことで文字が前面に来る）
            main_fs = rows[0]["fs"] if rows else 0
            self._draw_logo(canvas, main_fs, long_side, img_box, area, text_box)

            for r in rows:
                draw.text((r["x"], r["y"]), r["text"], fill=r["color"], font=r["font"])

            # 出力先ディレクトリ（元画像と同じ場所の下にoutput.dir_nameで指定されたフォルダ）を作成
            out_dir = path.parent / self.config.output.dir_name
            out_dir.mkdir(exist_ok=True, parents=True)
            # 出力ファイル名は prefix + 元のファイル名(拡張子なし) + .jpg
            out_path = out_dir / f"{self.config.output.prefix}{path.stem}.jpg"

            # 出力品質はYAMLの output.quality を優先し、未指定ならconstants.pyのデフォルトを使用。
            quality = getattr(self.config.output, "quality", constants.JPEG_QUALITY)
            subsampling = getattr(self.config.output, "subsampling", constants.JPEG_SUBSAMPLING)

            # JPEGとして保存（品質・クロマサブサンプリング設定を反映）
            canvas.save(out_path, "JPEG", quality=quality, subsampling=subsampling)

            print(progress_str)
            logger.info("画像処理完了: %s -> %s", path.name, out_path)
            return True

        except Exception as e:
            # 1枚の処理失敗が全体を止めないよう、例外はここで捕捉してFalseを返す
            logger.error("画像処理エラー (%s): %s", path.name, e, exc_info=True)
            print(f" [エラー] {path.name}: {e}")
            return False

    def run(self, target: Path) -> int:
        """対象ディレクトリの画像をすべて処理。"""
        # 対象パスが存在しなければエラー
        if not target.exists():
            raise FileNotFoundError(constants.ERROR_MESSAGES["directory_not_found"].format(target))

        # targetがファイル単体ならそれだけを、ディレクトリならその直下のファイル一覧を対象にする
        if target.is_file():
            files = [target]
        else:
            files = [f for f in target.iterdir() if f.is_file()]

        # 有効な画像拡張子の一覧。app_configがあればconfig.jsonの設定を優先して使う
        valid_extensions = constants.VALID_IMAGE_EXTENSIONS
        if self.app_config is not None:
            try:
                # JPEG系・RAW系の拡張子に加えてPNGも許可する
                ext_map = self.app_config.get_file_extensions()
                valid_extensions = ext_map["jpeg"] | ext_map["raw"] | {".png"}
            except AttributeError:
                # app_configにget_file_extensionsメソッドが無い（想定外のオブジェクト）場合は
                # constants.py側のデフォルト拡張子セットにフォールバック
                logger.debug("app_configにget_file_extensionsが無いため、デフォルト拡張子を使用")

        # 拡張子でフィルタして処理対象ファイルを絞り込む
        valid_files = [f for f in files if f.suffix.lower() in valid_extensions]

        if not valid_files:
            logger.warning("有効な画像ファイルが見つかりません: %s", target)
            print("有効な画像ファイルが見つかりません。")
            return 0

        # EXIFメタデータを1枚ずつではなく一括で取得（exiftool呼び出し回数を減らし高速化するため）
        logger.info("メタデータ一括取得開始: %d ファイル", len(valid_files))
        print(" -> メタデータを一括取得中...")
        metadata_map = self.exif_reader.get_photo_metadata_batch(valid_files)

        success_count = 0
        total_files = len(valid_files)
        for i, file_path in enumerate(valid_files, 1):
            # metadata_mapのキーはos.path.normpathで正規化されたパス文字列なので、
            # 検索時も同じ正規化を行って一致させる
            norm_path = os.path.normpath(str(file_path))
            meta = metadata_map.get(norm_path)

            if not meta:
                # メタデータが取得できなかったファイルはスキップ（フレーム描画に必要な情報がないため）
                logger.warning("メタデータが取得できません: %s", file_path.name)
                print(f" [警告] {file_path.name}: メタデータが取得できませんでした")
                continue

            # 1枚ずつフレーム付与処理を実行し、成功した枚数をカウント
            if self.process_image(file_path, meta, i, total_files):
                success_count += 1

        logger.info("画像処理完了: %d/%d ファイル", success_count, total_files)
        return success_count


def run_frame_processing(script_path_or_unused: Path, target_dir: Path, yaml_config: str, app_config=None) -> bool:
    """フレーム付与処理を実行するエントリーポイント。

    Args:
        app_config: ConfigManager（config.json）。exiftoolのパス解決に使用
    """
    print("==================================================")
    print(" 【開始】STEP 3: フレーム付与処理")
    print("==================================================")

    # YAML設定ファイルの絶対パスを解決
    conf_path = Path(yaml_config).resolve()

    try:
        # ImageProcessorを生成し、対象ディレクトリ配下の画像をまとめて処理
        processor = ImageProcessor(conf_path, app_config=app_config)
        success_count = processor.run(target_dir)

        # 処理結果のサマリーを表示
        print("\n")
        print(" 【処理のサマリー】")
        print(f"    ・フレーム付与完了: {success_count} ファイル")
        print("\n")
        print("==================================================")
        print(" 【終了】STEP 3: フレーム付与処理 終了")
        print("==================================================")
        # 1件以上成功していればTrue（呼び出し元main.pyのワークフロー継続判定に使われる）
        return success_count > 0

    except FileNotFoundError as e:
        # 設定ファイルや対象ディレクトリが見つからない場合のエラー
        logger.error("ファイル不見エラー: %s", e)
        print(f" -> [エラー] {e}")
        return False
    except Exception as e:
        # 想定外の例外はここでまとめて捕捉し、ワークフロー全体を落とさずFalseを返す
        logger.error("フレーム処理エラー: %s", e, exc_info=True)
        print(f"\n -> [エラー] フレーム処理中に予期しないエラーが発生しました: {e}")
        return False


# このファイルを単独で実行した場合のエントリーポイント
# （通常はmain.pyから run_frame_processing() として呼び出されるが、
#   このモジュール単体でも動作確認できるようにしてある）
if __name__ == "__main__":
    setup_logging()

    # 引数チェック：画像パス（ファイルまたはディレクトリ）は必須
    if len(sys.argv) < 2:
        print("使用法: python frame_processor.py <image_path_or_dir> [config_path]")
        sys.exit(1)

    # 第1引数：処理対象の画像ファイルまたはディレクトリ
    input_path = Path(sys.argv[1]).resolve()
    # 第2引数（省略可）：YAML設定ファイルパス。省略時はカレントディレクトリのstandard.yamlを使用
    conf_path = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) > 2
        else Path("standard.yaml").resolve()
    )

    try:
        processor = ImageProcessor(conf_path)
        processor.run(input_path)
    except Exception as e:
        logger.error("エラー: %s", e, exc_info=True)
        print(f"エラー: {e}")
        sys.exit(1)
