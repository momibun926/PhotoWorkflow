"""EXIF処理の統一的なユーティリティモジュール。"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import json

from . import constants

logger = logging.getLogger(__name__)


@dataclass
class PhotoMetadata:
    """写真のメタデータを保持するデータクラス。"""
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
    """EXIF値の形式変換を行うユーティリティクラス。"""

    @staticmethod
    def to_shutter_speed(exposure_time: Any) -> str:
        """シャッタースピードを表示形式に変換。
        
        Args:
            exposure_time: EXIF ExposureTime値
            
        Returns:
            フォーマットされたシャッタースピード文字列
        """
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
        except (ValueError, TypeError) as e:
            logger.warning("シャッタースピード変換失敗: %s, 元値: %s", e, exposure_time)
            return str(exposure_time)

    @staticmethod
    def to_exposure_mode(code: Any) -> str:
        """露出プログラムコードを文字列に変換。
        
        Args:
            code: EXIF ExposureProgram値
            
        Returns:
            フォーマットされた露出モード文字列
        """
        try:
            mode_code = int(code or 0)
            return constants.EXPOSURE_MODE_MAP.get(mode_code, f"({code})")
        except (ValueError, TypeError) as e:
            logger.warning("露出モード変換失敗: %s, 元値: %s", e, code)
            return f"({code})"

    @staticmethod
    def format_camera_name(name: Optional[str]) -> str:
        """カメラ・レンズ名の表記ゆれを補正。
        
        Args:
            name: フォーマット前のカメラ名
            
        Returns:
            フォーマット済みのカメラ名
        """
        if not name or name == "Unknown":
            return "Unknown"
        
        name_str = str(name)
        
        # 機材名の表記ゆれ補正
        for old, new in constants.CAMERA_NAME_REPLACEMENTS.items():
            name_str = name_str.replace(old, new)
        
        # 不要な単語を削除
        for word in constants.CAMERA_NAME_REMOVES:
            name_str = name_str.replace(word, "")
        
        # ブランド名の統一化
        for old, new in constants.CAMERA_NAME_BRAND_REPLACEMENTS.items():
            if name_str.upper().startswith(old.upper()):
                name_str = name_str.replace(name_str[:len(old)], new)
        
        return name_str.strip()


class ExifReader:
    """ExifToolを使用してメタデータを取得するクラス。"""

    def __init__(self, exiftool_path: Optional[str] = None):
        """初期化。
        
        Args:
            exiftool_path: exiftoolの実行ファイルパス。Noneの場合は自動検索
        """
        self.exiftool_path = exiftool_path or self._find_exiftool()
        logger.info("ExifTool パス: %s", self.exiftool_path)

    @staticmethod
    def _find_exiftool() -> str:
        """ExifToolを自動検索。"""
        import shutil
        path = shutil.which("exiftool.exe") or shutil.which("exiftool") or "exiftool"
        return path

    def get_photo_metadata_batch(self, file_paths: List[Path]) -> Dict[str, PhotoMetadata]:
        """複数ファイルのメタデータを一括取得。
        
        Args:
            file_paths: 対象ファイルパスリスト
            
        Returns:
            {ファイルパス正規化パス: PhotoMetadata}の辞書
        """
        if not file_paths:
            return {}

        str_paths = [str(p) for p in file_paths]
        metadata_map: Dict[str, PhotoMetadata] = {}

        try:
            # import exiftoolはここで動的に行う（依存性を軽くするため）
            import exiftool
            
            with exiftool.ExifToolHelper(executable=self.exiftool_path) as et:
                results = et.get_metadata(str_paths)
            
            for result in results:
                source_file = result.get("SourceFile")
                if not source_file:
                    continue
                
                meta = self._parse_metadata_result(result)
                metadata_map[os.path.normpath(source_file)] = meta
                
        except ImportError:
            logger.error("exiftool パッケージが未インストール。pip install exiftool を実行してください")
        except Exception as e:
            logger.error("ExifTool一括取得エラー: %s", e, exc_info=True)

        return metadata_map

    def get_exif_dates_batch(self, file_paths: List[Path]) -> Dict[str, str]:
        """複数ファイルの撮影日を一括取得。
        
        Args:
            file_paths: 対象ファイルパスリスト
            
        Returns:
            {ファイルパス: YYYYMMDD形式の日付}の辞書
        """
        if not file_paths:
            return {}

        date_map: Dict[str, str] = {}
        cmd = [
            self.exiftool_path,
            "-s3",
            "-DateTimeOriginal",
            "-d",
            constants.EXIF_DATE_FORMAT
        ] + [str(p) for p in file_paths]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            dates = result.stdout.splitlines()

            for path, date_str in zip(file_paths, dates):
                date_str = date_str.strip()
                if date_str and len(date_str) == 8 and date_str.isdigit():
                    date_map[path] = date_str
                else:
                    # フォールバック：ファイル更新日時を使用
                    from datetime import datetime
                    mtime = path.stat().st_mtime
                    date_map[path] = datetime.fromtimestamp(mtime).strftime(
                        constants.EXIF_DATE_FORMAT
                    )
                    logger.warning("ファイル %s の撮影日が取得できず、更新日時を使用: %s",
                                   path.name, date_map[path])
        except subprocess.CalledProcessError as e:
            logger.error("ExifTool実行エラー: %s", e.stderr, exc_info=True)
            # フォールバック：全ファイルのmtimeを使用
            from datetime import datetime
            for path in file_paths:
                mtime = path.stat().st_mtime
                date_map[path] = datetime.fromtimestamp(mtime).strftime(
                    constants.EXIF_DATE_FORMAT
                )
        except Exception as e:
            logger.error("日付取得エラー: %s", e, exc_info=True)

        return date_map

    def get_gps_info(self, file_path: Path) -> Optional[tuple]:
        """ファイルのGPS情報を取得。
        
        Args:
            file_path: 対象ファイルパス
            
        Returns:
            (緯度, 経度)のタプル、またはGPS情報がない場合はNone
        """
        cmd = [
            self.exiftool_path,
            "-j",
            "-n",
            "-GPSLatitude",
            "-GPSLongitude",
            str(file_path)
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            data = json.loads(result.stdout)
            if data and len(data) > 0:
                item = data[0]
                lat = item.get("GPSLatitude")
                lng = item.get("GPSLongitude")
                if lat is not None and lng is not None:
                    return (float(lat), float(lng))
        except Exception as e:
            logger.debug("GPS情報取得失敗 (%s): %s", file_path.name, e)
        
        return None

    @staticmethod
    def _parse_metadata_result(result: Dict[str, Any]) -> PhotoMetadata:
        """ExifToolの結果をPhotoMetadataに変換。"""
        bias_val = float(result.get("EXIF:ExposureCompensation", 0))
        bias_str = f"{bias_val:+.1f}" if bias_val != 0 else "0.0"

        wb_val = (
            result.get("MakerNotes:WhiteBalance")
            or result.get("EXIF:WhiteBalance")
            or "-"
        )
        if isinstance(wb_val, str):
            wb_val = wb_val.strip()

        ct_raw = (
            result.get("MakerNotes:ColorTemperature")
            or result.get("MakerNotes:WB_ColorTemperature")
            or result.get("Composite:ColorTemperature")
            or result.get("EXIF:ColorTemperature")
        )

        if ct_raw is not None and str(ct_raw).replace(".", "").isdigit():
            ct_str = f"({int(round(float(ct_raw)))}K)"
        elif ct_raw:
            ct_str = str(ct_raw) if str(ct_raw).endswith("K") else f"({ct_raw}K)"
        else:
            ct_str = ""

        return PhotoMetadata(
            camera=ExifConverter.format_camera_name(result.get("EXIF:Model")),
            lens=ExifConverter.format_camera_name(result.get("EXIF:LensModel")),
            focal_length=str(result.get("EXIF:FocalLength", "-")).replace(" mm", ""),
            iso=str(result.get("EXIF:ISO", "-")),
            f_number=str(result.get("EXIF:FNumber", "-")),
            shutter=ExifConverter.to_shutter_speed(result.get("EXIF:ExposureTime")),
            exposure_bias=bias_str,
            exposure_mode=ExifConverter.to_exposure_mode(result.get("EXIF:ExposureProgram")),
            picture_control=result.get("MakerNotes:PictureControlName", "-"),
            white_balance=str(wb_val),
            color_temperature=ct_str,
        )
