"""EXIF処理の統一的なユーティリティモジュール。

写真（JPEG/RAW）のEXIF情報を扱うための共通部品をまとめている。
主な役割は次の3つ：
  1. PhotoMetadata: 1枚の写真から抜き出したメタデータを保持するデータクラス
  2. ExifConverter: EXIFの生の値（コード値・数値）を人間が読みやすい文字列に変換する
  3. ExifReader: ExifToolClient を介して実際にexiftoolを呼び出し、複数ファイル分の
     メタデータ／撮影日／GPS情報をまとめて取得する
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from . import constants
from .exiftool_client import ExifToolClient, ExifToolError, path_key

logger = logging.getLogger(__name__)


@dataclass
class PhotoMetadata:
    """写真のメタデータを保持するデータクラス。

    ExifReader._parse_metadata_result() によって、exiftoolの生の結果から
    表示・出力用に整形された状態で生成される。全フィールドは表示用の文字列。
    """
    camera: str              # カメラ機種名（表記ゆれ補正済み）
    lens: str                # レンズ名（表記ゆれ補正済み）
    focal_length: str        # 焦点距離（"mm"の単位表記を除いた数値文字列）
    iso: str                 # ISO感度
    f_number: str            # F値（絞り）
    shutter: str             # シャッタースピード（例: "1/125" や "2\""）
    exposure_bias: str       # 露出補正値（例: "+0.3"）
    exposure_mode: str       # 露出モード（例: "A"。constants.EXPOSURE_MODE_MAP）
    picture_control: str     # ピクチャーコントロール名（Nikon独自のメーカーノート）
    white_balance: str       # ホワイトバランス設定
    color_temperature: str   # 色温度（例: "(5500K)"）


class ExifConverter:
    """EXIF値の形式変換を行うユーティリティクラス。

    exiftoolが返す生の数値・コード値を、そのまま表示に使える文字列へ変換する
    静的メソッド群。インスタンス化はせず、すべてstaticmethodとして呼び出す。
    """

    @staticmethod
    def to_shutter_speed(exposure_time: Any) -> str:
        """シャッタースピードを表示形式に変換。

        1秒未満は "1/125" のような分数表記、1秒以上は "2\"" のような
        秒表記（末尾にダブルクォート）に変換する。

        Args:
            exposure_time: EXIF ExposureTime値

        Returns:
            フォーマットされたシャッタースピード文字列
        """
        if exposure_time in (None, ""):
            # 露出時間が記録されていない写真（スクリーンショットなど）。他の項目と同じく "-" にする
            return "-"
        try:
            val = float(exposure_time)
            if val <= 0:
                return "-"
            if val < 1.0:
                # 1秒未満は「1/分母」にする（例: 0.008秒 → 1/125）。分母が10未満なら小数第1位まで（1/2.5 など）
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
            # 1秒以上は秒数に " を付ける（例: 2"）
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
    def format_camera_name(name: Optional[str], rules: Optional[Dict[str, Any]] = None) -> str:
        """カメラ・レンズ名の表記ゆれを補正。

        Args:
            name: フォーマット前のカメラ名
            rules: 補正ルール（replacements/removes/brand_replacements を持つ辞書）
                Noneの場合は constants.py のデフォルトルールを使用する。
                config.jsonの 'camera_name_rules' で機種別ルールを上書きできる
                （ConfigManager.get_camera_name_rules() 参照）。

        Returns:
            フォーマット済みのカメラ名
        """
        if not name or name == "Unknown":
            return "Unknown"

        if rules is None:
            rules = {
                "replacements": constants.CAMERA_NAME_REPLACEMENTS,
                "removes": constants.CAMERA_NAME_REMOVES,
                "brand_replacements": constants.CAMERA_NAME_BRAND_REPLACEMENTS,
            }

        name_str = str(name)

        for old, new in rules["replacements"].items():
            name_str = name_str.replace(old, new)

        for word in rules["removes"]:
            name_str = name_str.replace(word, "")

        # ブランド名の統一化：先頭が特定ブランド表記（大文字小文字を無視して判定）で
        # 始まっている場合、その先頭部分だけを統一表記に置き換える
        for old, new in rules["brand_replacements"].items():
            if name_str.upper().startswith(old.upper()):
                name_str = name_str.replace(name_str[:len(old)], new)

        return name_str.strip()


class ExifReader:
    """ExifToolClientを使用してメタデータを取得するクラス。

    生のexiftool呼び出しは行わず、すべて ExifToolClient に委譲する。
    """

    def __init__(self, exiftool_path: Optional[str] = None, config: Any = None):
        """初期化。

        Args:
            exiftool_path: exiftoolの実行ファイルパス。Noneの場合はconfig→自動検索の順で解決する
            config: ConfigManager。exiftoolパス解決およびカメラ名補正ルールの取得に使用
        """
        self._client = ExifToolClient(exiftool_path=exiftool_path, config=config)
        self._camera_rules: Optional[Dict[str, Any]] = (
            config.get_camera_name_rules() if config is not None else None
        )
        logger.info("ExifTool パス: %s", self._client.path)

    def get_photo_metadata_batch(self, file_paths: List[Path]) -> Dict[str, PhotoMetadata]:
        """複数ファイルのメタデータを一括取得。

        Args:
            file_paths: 対象ファイルパスリスト

        Returns:
            {path_key() で正規化したファイルパス: PhotoMetadata}の辞書。
            メタデータを取得・変換できなかったファイルは含まれない
        """
        if not file_paths:
            return {}

        metadata_map: Dict[str, PhotoMetadata] = {}

        try:
            # with の間だけ exiftool を常駐させ、全ファイルを1回の起動で読む
            with self._client as et:
                results = et.get_metadata_batch(file_paths)

            for result in results:
                source_file = result.get("SourceFile")
                if not source_file:
                    continue

                try:
                    meta = self._parse_metadata_result(result, self._camera_rules)
                except Exception as e:
                    # 1枚の値が想定外（数値でない露出補正値など）でも、他の写真の処理は続ける。
                    # このファイルは metadata_map に入らないため、呼び出し側で「メタデータなし」として扱われる
                    logger.warning("メタデータの変換に失敗しました (%s): %s", os.path.basename(str(source_file)), e)
                    continue
                # 区切り文字や大文字小文字の違いを吸収したキーにする（照合側も path_key を使う）
                metadata_map[path_key(source_file)] = meta

        except ExifToolError as e:
            logger.error("ExifTool一括取得エラー: %s", e, exc_info=True)

        return metadata_map

    def get_exif_dates_batch(self, file_paths: List[Path]) -> Dict[Path, str]:
        """複数ファイルの撮影日を一括取得。

        Args:
            file_paths: 対象ファイルパスリスト

        Returns:
            {ファイルパス: 撮影日}の辞書。撮影日を取得できなかったファイルは含まれない
        """
        return self._client.get_dates_batch(file_paths, date_format=constants.EXIF_DATE_FORMAT)

    @staticmethod
    def _parse_metadata_result(result: Dict[str, Any], camera_rules: Optional[Dict[str, Any]] = None) -> PhotoMetadata:
        """ExifToolの結果をPhotoMetadataに変換。

        Args:
            result: ExifToolから返された1ファイル分のメタデータ辞書
            camera_rules: カメラ名補正ルール（Noneならconstants.pyのデフォルトを使用）
        """
        # 露出補正値：0の場合は符号なしで"0.0"、それ以外は符号付き小数点1桁（例: "+0.3", "-1.0"）
        bias_val = float(result.get("EXIF:ExposureCompensation", 0))
        bias_str = f"{bias_val:+.1f}" if bias_val != 0 else "0.0"

        # ホワイトバランスはメーカーノート（機種固有タグ）優先、なければ標準EXIFタグ、
        # どちらもなければ "-" とする
        wb_val = (
            result.get("MakerNotes:WhiteBalance")
            or result.get("EXIF:WhiteBalance")
            or "-"
        )
        if isinstance(wb_val, str):
            wb_val = wb_val.strip()

        # 色温度も複数の候補タグを優先順位付きで探す（機種・撮影モードにより存在するタグが異なるため）
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
            camera=ExifConverter.format_camera_name(result.get("EXIF:Model"), camera_rules),
            lens=ExifConverter.format_camera_name(result.get("EXIF:LensModel"), camera_rules),
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
