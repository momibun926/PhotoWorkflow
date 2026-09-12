"""指定ディレクトリおよびサブフォルダ内の写真（NEF/JPEG）からEXIF情報を抽出し、
exif_data フォルダ内に全データTSVおよび個別の .exif ファイルを出力するスクリプト。
"""

import logging
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional

try:
    # パッケージとして実行された場合（本来の使われ方）
    from .exiftool_client import ExifToolClient, ExifToolError
    from .logging_config import setup_logging
except ImportError:
    # main.py から subprocess で単独スクリプトとして起動された場合のフォールバック
    # （manual_gps_tagger.py と同じパターン）
    from exiftool_client import ExifToolClient, ExifToolError
    from logging_config import setup_logging

logger = logging.getLogger(__name__)

# 個別 .exif ファイルに出力する対象タグの一覧
TARGET_TAGS = [
    "IFD0:Model",
    "Composite:LensID",
    "ExifIFD:FocalLength",
    "ExifIFD:FocalLengthIn35mmFormat",
    "ExifIFD:FNumber",
    "ExifIFD:ExposureTime",
    "ExifIFD:ISO",
    "ExifIFD:ExposureCompensation",
    "Nikon:AFAreaMode",
    "Nikon:FocusMode",
    "Nikon:PictureControlName",
    "Nikon:ShutterMode",
    "Nikon:WhiteBalance",
    "Composite:HyperfocalDistance"
]

# EXIFタグに対応する日本語表示名のマッピング
TAG_JAPANESE_NAMES = {
    "IFD0:Model": "カメラ",
    "Composite:LensID": "レンズ",
    "ExifIFD:FocalLength": "焦点距離",
    "ExifIFD:FocalLengthIn35mmFormat": "換算",
    "ExifIFD:FNumber": "F",
    "ExifIFD:ExposureTime": "SS",
    "ExifIFD:ISO": "ISO",
    "ExifIFD:ExposureCompensation": "露出補正",
    "Nikon:AFAreaMode": "AFエリアモード",
    "Nikon:FocusMode": "フォーカスモード",
    "Nikon:PictureControlName": "ピクチャーコントロール",
    "Nikon:ShutterMode": "シャッターモード",
    "Nikon:WhiteBalance": "ホワイトバランス",
    "Composite:HyperfocalDistance": "過焦点距離",
}


def export_individual_exif(item: Dict[str, Any], output_dir: Path) -> None:
    """写真1枚ごとの個別 .exif ファイルを生成する（日本語ラベル付き）。"""
    source_path_str = item.get("SourceFile", "")
    if not source_path_str:
        return

    source_path = Path(source_path_str)
    # 元ファイル名 + .exif（例: DSC_0001.JPG.exif）
    exif_filename = f"{source_path.name}.exif"
    exif_filepath = output_dir / exif_filename

    lines = []
    for tag in TARGET_TAGS:
        val = item.get(tag, "")
        if isinstance(val, (list, dict)):
            val = str(val)
        val_str = str(val).replace("\r", " ").replace("\n", " ")
        
        # 日本語ラベルを取得（未定義の場合は元のタグ名を表示）
        jp_label = TAG_JAPANESE_NAMES.get(tag, tag)
        lines.append(f"{jp_label}: {val_str}")

    try:
        with open(exif_filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.error("個別EXIF書き込みエラー (%s): %s", exif_filename, e)


def extract_all_exif_recursive(
    target_dir: Path,
    output_dir_name: str = "exif_data",
    tsv_filename: str = "exif_summary.tsv",
    config: Optional[Any] = None,
    exiftool_path: Optional[str] = None,
) -> bool:
    """ディレクトリ内の写真を再帰検索し、exif_dataフォルダ内にTSVおよび個別.exifを出力する。

    Args:
        config: ConfigManager。exiftoolのパス解決に使用（同一プロセス内呼び出し用）
        exiftool_path: exiftool実行ファイルの明示パス（別プロセス起動時など、configが
            渡せない場合に使用。config指定時はexiftool_pathが優先される）
    """
    if not target_dir.exists() or not target_dir.is_dir():
        logger.error("指定されたディレクトリが存在しません: %s", target_dir)
        return False

    # 出力先フォルダ（exif_data）の作成
    output_dir = target_dir / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)

    output_tsv_path = output_dir / tsv_filename

    et = ExifToolClient(exiftool_path=exiftool_path, config=config)
    logger.info("ExifTool を呼び出してサブフォルダ配下のメタデータを抽出中: %s (path=%s)", target_dir, et.path)

    try:
        data: List[Dict[str, Any]] = et.extract_recursive(
            directory=target_dir,
            extensions=["jpg", "jpeg", "nef"],
            group_names=True,
        )
    except ExifToolError as e:
        logger.error("ExifToolの実行に失敗しました: %s", e)
        return False

    if not data:
        logger.warning("対象の画像ファイル (.JPG, .NEF) または EXIF データが見つかりませんでした。")
        return False

    logger.info("%d 件のファイルを処理中...", len(data))

    # 1. 個別 .exif ファイルの出力
    for item in data:
        export_individual_exif(item, output_dir)

    # 2. 全体TSVの出力
    all_tags = set()
    for item in data:
        all_tags.update(item.keys())

    all_tags.discard("SourceFile")
    sorted_tags = ["SourceFile"] + sorted(list(all_tags))

    try:
        with open(output_tsv_path, "w", encoding="utf-8") as f:
            f.write("\t".join(sorted_tags) + "\n")
            for item in data:
                row_values = []
                for tag in sorted_tags:
                    val = item.get(tag, "")
                    if isinstance(val, (list, dict)):
                        val = str(val)
                    val_str = str(val).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                    row_values.append(val_str)
                f.write("\t".join(row_values) + "\n")

        logger.info("処理完了: %s フォルダにTSV(%s)および個別.exifを出力しました。", output_dir, tsv_filename)
        return True

    except Exception as e:
        logger.error("TSV書き込みエラー: %s", e, exc_info=True)
        return False


if __name__ == "__main__":
    # main.py と同じ共通ログ設定を使う（画面にはWARNING以上のみ、
    # 詳細は同じ photo_organizer.log に集約）
    setup_logging()

    # sys.argv[1]: 対象ディレクトリ
    # sys.argv[2]: TSVファイル名（省略可）
    # sys.argv[3]: exiftool実行ファイルパス（省略可。main.py経由の場合、config.jsonの値が渡される）
    input_directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    tsv_name = sys.argv[2] if len(sys.argv) > 2 else "exif_summary.tsv"
    exiftool_path = sys.argv[3] if len(sys.argv) > 3 else None

    extract_all_exif_recursive(input_directory, tsv_filename=tsv_name, exiftool_path=exiftool_path)
