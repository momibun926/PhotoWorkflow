"""STEP 3: フレーム付与処理モジュール (modules/frame_processor.py)."""

import sys
import os
import shutil
import yaml
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any, Optional, Dict
from types import SimpleNamespace

import exiftool
from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass
class PhotoMetadata:
    camera: str
    lens: str
    focal_length: str
    iso: str
    f_number: str
    shutter: str
    exposure_bias: str
    exposure_mode: str
    picture_control: str
    white_balance: str
    color_temperature: str


class ExifConverter:
    @staticmethod
    def to_shutter_speed(exposure_time: Any) -> str:
        try:
            val = float(exposure_time)
            if val <= 0:
                return "-"
            if val < 1.0:
                denom = 1.0 / val
                denom_str = (
                    f"{denom:.1f}".rstrip("0").rstrip(".")
                    if denom < 10
                    else f"{int(round(denom))}"
                )
                return f"1/{denom_str}"
            sec_str = (
                f"{val:.1f}".rstrip("0").rstrip(".")
                if val % 1 != 0
                else str(int(val))
            )
            return f'{sec_str}"'
        except Exception:
            return str(exposure_time)

    @staticmethod
    def to_exposure_mode(code: Any) -> str:
        return {1: "M", 2: "P", 3: "A", 4: "S", 0: "AUTO"}.get(
            int(code or 0), f"({code})"
        )

    @staticmethod
    def format_name(name: Optional[str]) -> str:
        if not name or name == "Unknown":
            return "Unknown"
        name_str = str(name)
        
        # 機材名の表記ゆれ・フォーマット補正
        name_str = (
            name_str.replace("Z50_2", "Z50II")
            .replace("Z50 2", "Z50II")
            .replace("Z50ii", "Z50II")
            .replace("_3", "III")
            .replace("_2", "II")
            .replace("_4", "IV")
            .replace("_5", "V")
        )

        words_to_remove = ["NIKON", "Nikon", "nikon", "NIKKOR", "Nikkor", "nikkor"]
        for word in words_to_remove:
            name_str = name_str.replace(word, "")

        replacements = {"SONY": "Sony", "CANON": "Canon", "FUJIFILM": "Fujifilm"}
        for old, new in replacements.items():
            if name_str.upper().startswith(old.upper()):
                name_str = name_str.replace(name_str[: len(old)], new)

        return name_str.strip()


class ImageProcessor:
    def __init__(self, config_path: Path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)
        
        # dictを再帰的にSimpleNamespaceに変換（ドットアクセスを維持）
        self.config = self._dict_to_namespace(raw_config)
        
        self.exiftool_path = (
            shutil.which("exiftool.exe")
            or shutil.which("exiftool")
            or "exiftool"
        )
        self.logo_path = Path("nikon_logo.png")

    def _dict_to_namespace(self, data):
        if isinstance(data, dict):
            return SimpleNamespace(**{k: self._dict_to_namespace(v) for k, v in data.items()})
        elif isinstance(data, list):
            return [self._dict_to_namespace(v) for v in data]
        return data

    def _get_all_metadata_batch(self, file_paths: list) -> Dict[str, PhotoMetadata]:
        """ExifToolを使って全ファイルのメタデータを一括取得する"""
        str_paths = [str(p) for p in file_paths]
        metadata_map = {}

        try:
            with exiftool.ExifToolHelper(executable=self.exiftool_path) as et:
                results = et.get_metadata(str_paths)
            
            for m in results:
                source_file = m.get("SourceFile")
                if not source_file:
                    continue
                
                bias_val = float(m.get("EXIF:ExposureCompensation", 0))
                bias_str = f"{bias_val:+.1f}" if bias_val != 0 else "0.0"

                wb_val = (
                    m.get("MakerNotes:WhiteBalance")
                    or m.get("EXIF:WhiteBalance")
                    or "-"
                ).strip()

                ct_raw = (
                    m.get("MakerNotes:ColorTemperature")
                    or m.get("MakerNotes:WB_ColorTemperature")
                    or m.get("Composite:ColorTemperature")
                    or m.get("EXIF:ColorTemperature")
                )

                if ct_raw is not None and str(ct_raw).replace(".", "").isdigit():
                    ct_str = f"({int(round(float(ct_raw)))}K)"
                elif ct_raw:
                    ct_str = str(ct_raw) if str(ct_raw).endswith("K") else f"({ct_raw}K)"
                else:
                    ct_str = ""

                meta = PhotoMetadata(
                    camera=ExifConverter.format_name(m.get("EXIF:Model")),
                    lens=ExifConverter.format_name(m.get("EXIF:LensModel")),
                    focal_length=str(m.get("EXIF:FocalLength", "-")).replace(" mm", ""),
                    iso=str(m.get("EXIF:ISO", "-")),
                    f_number=str(m.get("EXIF:FNumber", "-")),
                    shutter=ExifConverter.to_shutter_speed(m.get("EXIF:ExposureTime")),
                    exposure_bias=bias_str,
                    exposure_mode=ExifConverter.to_exposure_mode(
                        m.get("EXIF:ExposureProgram")
                    ),
                    picture_control=m.get("MakerNotes:PictureControlName", "-"),
                    white_balance=str(wb_val),
                    color_temperature=ct_str,
                )
                metadata_map[os.path.normpath(source_file)] = meta
        except Exception as e:
            print(f"[!] ExifTool バッチ取得エラー: {e}")

        return metadata_map

    def _calculate_font_size(self, long_side: int, config_val: float) -> int:
        if self.config.fonts.size_type == "px":
            return int(config_val)
        return int(long_side * config_val)

    def _get_font(self, font_list: list, size: int):
        tried_fonts = []
        for name in font_list:
            tried_fonts.append(name)
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        raise FileNotFoundError(f"指定されたフォントが見つかりません。探した名前/パス: {tried_fonts}")

    def process_image(self, path: Path, meta: PhotoMetadata, current: int, total: int):
        progress_pct = int((current / total) * 100)
        progress_str = f"[{progress_pct:3d}%] ({current}/{total}) {path.name}"

        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        meta_dict = asdict(meta)

        w, h = img.size
        long_side = max(w, h)

        main_fs = self._calculate_font_size(
            long_side, self.config.fonts.main_size
        )
        sub_fs = self._calculate_font_size(
            long_side, self.config.fonts.sub_size
        )
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

        margin_area_x = 0
        margin_area_y = h + side_m

        top_padding_ratio = getattr(
            self.config.layout, "top_padding_ratio", 0.20
        )
        y_offset_px = getattr(self.config.layout, "y_offset_px", 0)
        line_spacing_px = getattr(self.config.layout, "line_spacing_px", 40)

        y_top_line = (
            margin_area_y + int(bott_m * top_padding_ratio) + y_offset_px
        )
        y_bottom_line = y_top_line + line_spacing_px

        # ロゴ描画処理
        if self.logo_path.exists():
            try:
                logo = Image.open(self.logo_path).convert("RGBA")
                logo_h = int(main_fs * 2.3)
                logo_w = int(logo.width * (logo_h / logo.height))
                logo = logo.resize((logo_w, logo_h), Image.Resampling.LANCZOS)

                logo_left = side_m + 70
                logo_top = 70

                logo_x = margin_area_x + logo_left
                logo_y = margin_area_y + logo_top

                canvas.paste(logo, (logo_x, logo_y), logo)
            except Exception:
                pass

        # テキスト描画（センタリング）
        tw_top = draw.textbbox((0, 0), text_top, font=f_main)[2]
        text_top_x = (canvas_w - tw_top) // 2
        draw.text(
            (text_top_x, y_top_line),
            text_top,
            fill=self.config.colors.main,
            font=f_main,
        )

        tw_bottom = draw.textbbox((0, 0), text_bottom, font=f_sub)[2]
        text_bottom_x = (canvas_w - tw_bottom) // 2
        draw.text(
            (text_bottom_x, y_bottom_line),
            text_bottom,
            fill=self.config.colors.sub,
            font=f_sub,
        )

        # 保存
        out_dir = path.parent / self.config.output.dir_name
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"{self.config.output.prefix}{path.stem}.jpg"
        canvas.save(
            out_path,
            "JPEG",
            quality=self.config.output.quality,
            subsampling=0,
        )

        print(progress_str)

    def run(self, target: Path):
        if target.is_file():
            files = [target]
        else:
            files = list(target.iterdir())

        valid_extensions = {".jpg", ".jpeg", ".png", ".nef", ".arw", ".dng"}
        valid_files = [f for f in files if f.suffix.lower() in valid_extensions]

        if not valid_files:
            print("No valid image files found.")
            return

        # メタデータを一度に一括取得
        print(" -> メタデータを一括取得中...")
        metadata_map = self._get_all_metadata_batch(valid_files)

        total_files = len(valid_files)
        for i, f in enumerate(valid_files, 1):
            norm_path = os.path.normpath(str(f))
            meta = metadata_map.get(norm_path)
            
            if not meta:
                print(f"Error: {f.name} - メタデータが取得できませんでした")
                continue

            try:
                self.process_image(f, meta, i, total_files)
            except Exception as e:
                print(f"Error: {f.name} - {e}")


def run_frame_processing(
    script_path_or_unused: Path, target_dir: Path, yaml_config: str
) -> bool:
    print("==================================================")
    print(" 【開始】STEP 3: フレーム付与処理")
    print("==================================================")

    conf_path = Path(yaml_config).resolve()
    if not conf_path.exists():
        print(f" -> [エラー] 設定ファイルが見つかりません: {conf_path}")
        return False

    try:
        processor = ImageProcessor(conf_path)
        processor.run(target_dir)

        print("\n")
        print(" 【処理のサマリー】")
        print("    ・フレーム付与処理: 正常完了")
        print("\n")
        print("==================================================")
        print(" 【終了】STEP 3: フレーム付与処理 終了")
        print("==================================================")
        return True
    except Exception as e:
        print(f"\n -> [エラー] フレーム処理中に例外が発生しました: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python frame_processor.py <image_path_or_dir> [config_path]")
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    conf_path = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) > 2
        else Path("standard.yaml").resolve()
    )

    if not conf_path.exists():
        print(f"Config file not found: {conf_path}")
        sys.exit(1)

    processor = ImageProcessor(conf_path)
    processor.run(input_path)