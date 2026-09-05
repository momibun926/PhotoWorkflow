"""STEP 3: フレーム付与処理モジュール (modules/frame_processor.py)."""

import logging
import sys
import os
from pathlib import Path
from dataclasses import asdict
from typing import List

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .exif_utils import PhotoMetadata, ExifReader
from .config_manager import YamlConfigManager
from . import constants

logger = logging.getLogger(__name__)


class ImageProcessor:
    """画像にフレームとEXIF情報を付与するクラス。"""

    def __init__(self, config_path: Path) -> None:
        """初期化。
        
        Args:
            config_path: YAML形式の設定ファイルパス
        """
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            raise FileNotFoundError(constants.ERROR_MESSAGES["config_not_found"].format(config_path))
        
        self.config = YamlConfigManager(self.config_path)
        self.exif_reader = ExifReader()
        self.logo_path = Path(constants.LOGO_FILENAME)
        logger.info("ImageProcessor 初期化完了: config=%s", config_path)

    def _calculate_font_size(self, long_side: int, config_val: float) -> int:
        """フォントサイズを計算。"""
        if self.config.fonts.size_type == "px":
            return int(config_val)
        return int(long_side * config_val)

    def _get_font(self, font_list: List[str], size: int) -> ImageFont.FreeTypeFont:
        """フォントを取得。"""
        for font_name in font_list:
            try:
                return ImageFont.truetype(font_name, size)
            except OSError as e:
                logger.debug("フォント読み込み失敗: %s", font_name)
                continue
        error_msg = f"指定されたフォントが見つかりません: {font_list}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    def _draw_logo(self, canvas: Image.Image, draw: ImageDraw.ImageDraw,
                  main_fs: int, side_m: int) -> None:
        """ロゴを画像に描画。"""
        if not self.logo_path.exists():
            return
        try:
            logo = Image.open(self.logo_path).convert("RGBA")
            logo_h = int(main_fs * constants.LOGO_SIZE_RATIO)
            logo_w = int(logo.width * (logo_h / logo.height))
            logo = logo.resize((logo_w, logo_h), Image.Resampling.LANCZOS)
            logo_x = side_m + constants.DEFAULT_LOGO_LEFT_OFFSET
            # ロゴを左下に配置（キャンバス下部）
            logo_y = canvas.height - logo_h - constants.DEFAULT_LOGO_TOP_OFFSET
            canvas.paste(logo, (logo_x, logo_y), logo)
            logger.debug("ロゴ描画完了（左下）")
        except Exception as e:
            logger.warning("ロゴ描画失敗: %s", e)

    def process_image(self, path: Path, meta: PhotoMetadata, current: int, total: int) -> bool:
        """画像を処理してフレーム付与。"""
        try:
            progress_pct = int((current / total) * 100)
            progress_str = f"[{progress_pct:3d}%] ({current}/{total}) {path.name}"

            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")

            meta_dict = asdict(meta)
            w, h = img.size
            long_side = max(w, h)

            main_fs = self._calculate_font_size(long_side, self.config.fonts.main_size)
            sub_fs = self._calculate_font_size(long_side, self.config.fonts.sub_size)
            f_main = self._get_font(self.config.fonts.bold, main_fs)
            f_sub = self._get_font(self.config.fonts.regular, sub_fs)

            side_m = int(long_side * self.config.ratios.side_margin)
            bott_m = int(long_side * self.config.ratios.bottom_margin)
            canvas_w = w + side_m * 2
            canvas_h = h + side_m + bott_m

            canvas = Image.new("RGB", (canvas_w, canvas_h), self.config.colors.bg)
            canvas.paste(img, (side_m, side_m))
            draw = ImageDraw.Draw(canvas)

            text_top = self.config.layout.top.format(**meta_dict)
            text_bottom = self.config.layout.bottom.format(**meta_dict)

            margin_area_y = h + side_m
            top_padding_ratio = getattr(self.config.layout, "top_padding_ratio", 0.20)
            y_offset_px = getattr(self.config.layout, "y_offset_px", 0)
            line_spacing_px = getattr(self.config.layout, "line_spacing_px", 40)

            y_top_line = margin_area_y + int(bott_m * top_padding_ratio) + y_offset_px
            y_bottom_line = y_top_line + line_spacing_px

            self._draw_logo(canvas, draw, main_fs, side_m)

            tw_top = draw.textbbox((0, 0), text_top, font=f_main)[2]
            text_top_x = (canvas_w - tw_top) // 2
            draw.text((text_top_x, y_top_line), text_top, fill=self.config.colors.main, font=f_main)

            tw_bottom = draw.textbbox((0, 0), text_bottom, font=f_sub)[2]
            text_bottom_x = (canvas_w - tw_bottom) // 2
            draw.text((text_bottom_x, y_bottom_line), text_bottom, fill=self.config.colors.sub, font=f_sub)

            out_dir = path.parent / self.config.output.dir_name
            out_dir.mkdir(exist_ok=True, parents=True)
            out_path = out_dir / f"{self.config.output.prefix}{path.stem}.jpg"

            canvas.save(out_path, "JPEG", quality=constants.JPEG_QUALITY, subsampling=constants.JPEG_SUBSAMPLING)

            print(progress_str)
            logger.info("画像処理完了: %s -> %s", path.name, out_path)
            return True

        except Exception as e:
            logger.error("画像処理エラー (%s): %s", path.name, e, exc_info=True)
            print(f" [エラー] {path.name}: {e}")
            return False

    def run(self, target: Path) -> int:
        """対象ディレクトリの画像をすべて処理。"""
        if not target.exists():
            raise FileNotFoundError(constants.ERROR_MESSAGES["directory_not_found"].format(target))

        if target.is_file():
            files = [target]
        else:
            files = [f for f in target.iterdir() if f.is_file()]

        valid_files = [f for f in files if f.suffix.lower() in constants.VALID_IMAGE_EXTENSIONS]

        if not valid_files:
            logger.warning("有効な画像ファイルが見つかりません: %s", target)
            print("有効な画像ファイルが見つかりません。")
            return 0

        logger.info("メタデータ一括取得開始: %d ファイル", len(valid_files))
        print(" -> メタデータを一括取得中...")
        metadata_map = self.exif_reader.get_photo_metadata_batch(valid_files)

        success_count = 0
        total_files = len(valid_files)
        for i, file_path in enumerate(valid_files, 1):
            norm_path = os.path.normpath(str(file_path))
            meta = metadata_map.get(norm_path)
            
            if not meta:
                logger.warning("メタデータが取得できません: %s", file_path.name)
                print(f" [警告] {file_path.name}: メタデータが取得できませんでした")
                continue

            if self.process_image(file_path, meta, i, total_files):
                success_count += 1

        logger.info("画像処理完了: %d/%d ファイル", success_count, total_files)
        return success_count


def run_frame_processing(script_path_or_unused: Path, target_dir: Path, yaml_config: str) -> bool:
    """フレーム付与処理を実行するエントリーポイント。"""
    print("==================================================")
    print(" 【開始】STEP 3: フレーム付与処理")
    print("==================================================")

    conf_path = Path(yaml_config).resolve()

    try:
        processor = ImageProcessor(conf_path)
        success_count = processor.run(target_dir)

        print("\n")
        print(" 【処理のサマリー】")
        print(f"    ・フレーム付与完了: {success_count} ファイル")
        print("\n")
        print("==================================================")
        print(" 【終了】STEP 3: フレーム付与処理 終了")
        print("==================================================")
        return success_count > 0

    except FileNotFoundError as e:
        logger.error("ファイル不見エラー: %s", e)
        print(f" -> [エラー] {e}")
        return False
    except Exception as e:
        logger.error("フレーム処理エラー: %s", e, exc_info=True)
        print(f"\n -> [エラー] フレーム処理中に予期しないエラーが発生しました: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format=constants.LOG_FORMAT,
        datefmt=constants.LOG_DATE_FORMAT
    )

    if len(sys.argv) < 2:
        print("使用法: python frame_processor.py <image_path_or_dir> [config_path]")
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
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
