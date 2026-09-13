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

# constants: カメラ名補正ルールや露出モードのコード対応表などの定数を集めたモジュール
from . import constants
# exiftool_client: exiftool実行ファイルの探索・プロセス起動・結果パースを担うモジュールから
# クライアントクラスと専用の例外クラスをインポート
from .exiftool_client import ExifToolClient, ExifToolError

# このモジュール専用のロガー（呼び出し元のログ設定に従って出力される）
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
    exposure_mode: str       # 露出モード名（例: "絞り優先"）
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
        try:
            # 文字列や数値で渡ってくるexposure_timeをfloatに正規化
            val = float(exposure_time)
            if val <= 0:
                # 0以下（未設定・異常値）は表示しようがないのでハイフンを返す
                return "-"
            if val < 1.0:
                # 1秒未満は「1/分母」の形式にする（例: 0.008秒 → 1/125）
                denom = 1.0 / val
                denom_str = (
                    # 小数点以下1桁に丸めた上で末尾の0や"."を削り、整数風に見せる
                    f"{denom:.1f}".rstrip("0").rstrip(".")
                    if denom < 10
                    # 分母が10以上のときは四捨五入した整数にする（例: 1/125）
                    else f"{int(round(denom))}"
                )
                return f"1/{denom_str}"
            # 1秒以上はそのまま秒数として表示（小数第1位までで、末尾の0や"."は削る）
            sec_str = (
                f"{val:.1f}".rstrip("0").rstrip(".")
                if val % 1 != 0
                else str(int(val))
            )
            # 「秒」であることを示すためダブルクォートを付与（例: 2"）
            return f'{sec_str}"'
        except (ValueError, TypeError) as e:
            # floatに変換できない・型が想定外などの場合は警告を出し、元の値を文字列化して返す
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
            # コードが未設定(None)の場合は0（未定義）として扱う
            mode_code = int(code or 0)
            # constants.EXPOSURE_MODE_MAP にある対応表から日本語名を引く。
            # 未知のコードの場合は "(コード値)" の形でそのまま表示する
            return constants.EXPOSURE_MODE_MAP.get(mode_code, f"({code})")
        except (ValueError, TypeError) as e:
            # intに変換できない場合も同様にログを残してフォールバック表示にする
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
        # 名前が空、またはexiftoolが値を取得できなかった場合の"Unknown"はそのまま返す
        if not name or name == "Unknown":
            return "Unknown"

        # ルールが渡されなかった場合は constants.py のデフォルトルールを組み立てて使用する
        if rules is None:
            rules = {
                "replacements": constants.CAMERA_NAME_REPLACEMENTS,
                "removes": constants.CAMERA_NAME_REMOVES,
                "brand_replacements": constants.CAMERA_NAME_BRAND_REPLACEMENTS,
            }

        name_str = str(name)

        # 機材名の表記ゆれ補正（例: "NIKON Z 50_2" → "NIKON Z 50II" のような単純な文字列置換）
        for old, new in rules["replacements"].items():
            name_str = name_str.replace(old, new)

        # 不要な単語を削除（例: 冗長なブランド接頭辞や記号などを取り除く）
        for word in rules["removes"]:
            name_str = name_str.replace(word, "")

        # ブランド名の統一化：先頭が特定ブランド表記（大文字小文字を無視して判定）で
        # 始まっている場合、その先頭部分だけを統一表記に置き換える
        for old, new in rules["brand_replacements"].items():
            if name_str.upper().startswith(old.upper()):
                name_str = name_str.replace(name_str[:len(old)], new)

        # 前後の余分な空白を削って完成
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
        # exiftoolの起動・実行はすべてExifToolClientに委譲する
        self._client = ExifToolClient(exiftool_path=exiftool_path, config=config)
        # config が渡されていればカメラ名補正ルールを事前に取得しておく（未指定ならNone＝デフォルトルール使用）
        self._camera_rules: Optional[Dict[str, Any]] = (
            config.get_camera_name_rules() if config is not None else None
        )
        # 解決されたexiftoolのパスをログに残しておく（トラブルシュート用）
        logger.info("ExifTool パス: %s", self._client.path)

    @property
    def exiftool_path(self) -> str:
        """互換性のため：解決済みのexiftoolパスを返す。"""
        return self._client.path

    def get_photo_metadata_batch(self, file_paths: List[Path]) -> Dict[str, PhotoMetadata]:
        """複数ファイルのメタデータを一括取得。

        Args:
            file_paths: 対象ファイルパスリスト

        Returns:
            {正規化済みのファイルパス: PhotoMetadata}の辞書
        """
        # 対象が空なら exiftool を呼び出すまでもなく空辞書を返す
        if not file_paths:
            return {}

        metadata_map: Dict[str, PhotoMetadata] = {}

        try:
            # withブロックでExifToolClientをコンテキストマネージャとして使う
            # （常駐プロセスの起動・終了などをクライアント側で管理している想定）
            with self._client as et:
                # 1ファイルずつではなく一括でexiftoolに問い合わせることで高速化する
                results = et.get_metadata_batch(file_paths)

            # 返ってきた結果を1件ずつPhotoMetadataに変換していく
            for result in results:
                source_file = result.get("SourceFile")
                if not source_file:
                    # SourceFileが取れない＝どのファイルの結果か分からないためスキップ
                    continue

                meta = self._parse_metadata_result(result, self._camera_rules)
                # OS依存の区切り文字などの差異を吸収するため normpath で正規化してキーにする
                metadata_map[os.path.normpath(source_file)] = meta

        except ExifToolError as e:
            # exiftool呼び出し自体が失敗した場合はエラーログを出し、空（または途中まで）の辞書を返す
            logger.error("ExifTool一括取得エラー: %s", e, exc_info=True)

        return metadata_map

    def get_exif_dates_batch(self, file_paths: List[Path]) -> Dict[Path, str]:
        """複数ファイルの撮影日を一括取得。

        Args:
            file_paths: 対象ファイルパスリスト

        Returns:
            {正規化済みのファイルパス: 撮影日}の辞書
        """
        # 日付取得もクライアント側に委譲。フォーマットはconstants.pyで一元管理している
        return self._client.get_dates_batch(file_paths, date_format=constants.EXIF_DATE_FORMAT)

    def get_gps_info(self, file_path: Path) -> Optional[tuple]:
        """ファイルのGPS情報を取得。

        Args:
            file_path: 対象ファイルパス

        Returns:
            (緯度, 経度)のタプル、またはGPS情報がない場合はNone
        """
        return self._client.get_gps(file_path)

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
            # 数値（"5500" や "5500.0"）として取得できた場合は丸めて "(5500K)" の形式にする
            ct_str = f"({int(round(float(ct_raw)))}K)"
        elif ct_raw:
            # すでに "K" 付きの文字列ならそのまま、そうでなければ "(値K)" の形式に整形する
            ct_str = str(ct_raw) if str(ct_raw).endswith("K") else f"({ct_raw}K)"
        else:
            # 色温度情報が全く取得できない場合は空文字列
            ct_str = ""

        # 各フィールドをそれぞれの変換ロジック・デフォルト値付きで組み立ててPhotoMetadataを生成
        return PhotoMetadata(
            camera=ExifConverter.format_camera_name(result.get("EXIF:Model"), camera_rules),
            lens=ExifConverter.format_camera_name(result.get("EXIF:LensModel"), camera_rules),
            # "24.0 mm" のような値から " mm" を取り除いて数値部分だけにする
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
