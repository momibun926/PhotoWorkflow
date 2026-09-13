"""指定ディレクトリおよびそのサブフォルダ内の写真（NEF/JPEG）からEXIF情報を抽出し、
exif_data フォルダ内に全データTSVおよび個別の .exif ファイルを出力するスクリプト。
"""

import logging
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional

try:
    # パッケージとして実行された場合（本来の使われ方）
    # 同一パッケージ内の exiftool_client / logging_config / config_manager モジュールから
    # それぞれ ExifToolClient・ExifToolError、setup_logging、ConfigManager をインポートする
    from .exiftool_client import ExifToolClient, ExifToolError
    from .logging_config import setup_logging
    from .config_manager import ConfigManager
except ImportError:
    # main.py から subprocess で単独スクリプトとして起動された場合のフォールバック
    # （manual_gps_tagger.py と同じパターン）
    # この場合はパッケージの相対importが使えないため、絶対importで同名モジュールを読み込む
    from exiftool_client import ExifToolClient, ExifToolError
    from logging_config import setup_logging
    from config_manager import ConfigManager

# このモジュール専用のロガーを取得（ログにはモジュール名が記録される）
logger = logging.getLogger(__name__)

# 個別 .exif ファイルに出力する対象タグのデフォルト一覧。
# config.json の 'exif_export.target_tags' で上書きできる
# （ConfigManager.get_exif_export_config() 参照）。
DEFAULT_TARGET_TAGS = [
    "IFD0:Model",                              # カメラ本体のモデル名
    "Composite:LensID",                        # 使用レンズの識別情報
    "ExifIFD:FocalLength",                     # 焦点距離（実際の値）
    "ExifIFD:FocalLengthIn35mmFormat",         # 35mm換算の焦点距離
    "ExifIFD:FNumber",                         # 絞り値（F値）
    "ExifIFD:ExposureTime",                    # シャッタースピード（露光時間）
    "ExifIFD:ISO",                             # ISO感度
    "ExifIFD:ExposureCompensation",            # 露出補正量
    "Nikon:AFAreaMode",                        # AFエリアモード（Nikon固有タグ）
    "Nikon:FocusMode",                         # フォーカスモード（Nikon固有タグ）
    "Nikon:PictureControlName",                # ピクチャーコントロールの名前（Nikon固有タグ）
    "Nikon:ShutterMode",                       # シャッターモード（Nikon固有タグ）
    "Nikon:WhiteBalance",                      # ホワイトバランス設定（Nikon固有タグ）
    "Composite:HyperfocalDistance"             # 過焦点距離（合成計算値）
]

# EXIFタグに対応する日本語表示名のデフォルトマッピング。
# config.json の 'exif_export.tag_labels' で上書きできる。
DEFAULT_TAG_JAPANESE_NAMES = {
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


def export_individual_exif(
    item: Dict[str, Any],
    output_dir: Path,
    target_tags: Optional[List[str]] = None,
    tag_labels: Optional[Dict[str, str]] = None,
) -> None:
    """写真1枚ごとの個別 .exif ファイルを生成する（日本語ラベル付き）。

    Args:
        target_tags: 出力するタグの一覧。Noneの場合は DEFAULT_TARGET_TAGS を使用
        tag_labels: タグ→日本語ラベルのマッピング。Noneの場合は DEFAULT_TAG_JAPANESE_NAMES を使用
    """
    # 引数が渡されていない場合はモジュール冒頭で定義したデフォルト値にフォールバックする
    target_tags = target_tags or DEFAULT_TARGET_TAGS
    tag_labels = tag_labels or DEFAULT_TAG_JAPANESE_NAMES

    # exiftoolの出力結果（辞書）から元の画像ファイルパスを取得
    source_path_str = item.get("SourceFile", "")
    if not source_path_str:
        # SourceFileが無い＝この項目は対象外として何もせず終了
        return

    source_path = Path(source_path_str)
    # 元ファイル名 + .exif（例: DSC_0001.JPG.exif）
    exif_filename = f"{source_path.name}.exif"
    exif_filepath = output_dir / exif_filename

    # 出力する行（"ラベル: 値" の形式）を1タグずつ組み立てる
    lines = []
    for tag in target_tags:
        val = item.get(tag, "")
        # 値がlist/dict（配列やネストされた構造）の場合は文字列化しておく
        if isinstance(val, (list, dict)):
            val = str(val)
        # 改行やキャリッジリターンが値に混入しているとテキストファイルの体裁が崩れるためスペースに置換
        val_str = str(val).replace("\r", " ").replace("\n", " ")

        # 日本語ラベルを取得（未定義の場合は元のタグ名を表示）
        jp_label = tag_labels.get(tag, tag)
        lines.append(f"{jp_label}: {val_str}")

    # 組み立てた行をテキストファイルとして書き出す
    try:
        with open(exif_filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        # ファイル書き込みに失敗しても処理全体は止めず、ログに記録して継続する
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
    # 対象ディレクトリが存在しない、またはディレクトリでない場合はエラーとして早期リターン
    if not target_dir.exists() or not target_dir.is_dir():
        logger.error("指定されたディレクトリが存在しません: %s", target_dir)
        return False

    # 出力先フォルダ（exif_data）の作成
    output_dir = target_dir / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)

    output_tsv_path = output_dir / tsv_filename

    # ExifToolClient のインスタンスを生成（exiftool実行ファイルのパス・config設定を渡す）
    et = ExifToolClient(exiftool_path=exiftool_path, config=config)
    logger.info("ExifTool を呼び出してサブフォルダ配下のメタデータを抽出中: %s (path=%s)", target_dir, et.path)

    # 個別.exifファイルに出力するタグ・ラベルを解決（config優先、なければデフォルト）
    target_tags = DEFAULT_TARGET_TAGS
    tag_labels = DEFAULT_TAG_JAPANESE_NAMES
    if config is not None:
        try:
            # ConfigManagerからexif_export設定（target_tags, tag_labels）を取得
            ee_config = config.get_exif_export_config()
            target_tags = ee_config.get("target_tags") or DEFAULT_TARGET_TAGS
            tag_labels = ee_config.get("tag_labels") or DEFAULT_TAG_JAPANESE_NAMES
        except AttributeError:
            # configにget_exif_export_configメソッドが存在しない場合はデフォルト設定を使う
            logger.debug("configにget_exif_export_configが無いため、デフォルトのタグ設定を使用")

    # exiftoolを使って対象ディレクトリ配下のjpg/jpeg/nefファイルを再帰的にスキャンし、
    # 各ファイルのEXIF情報を辞書のリストとして取得する
    try:
        data: List[Dict[str, Any]] = et.extract_recursive(
            directory=target_dir,
            extensions=["jpg", "jpeg", "nef"],
            group_names=True,
        )
    except ExifToolError as e:
        # exiftool実行自体が失敗した場合（コマンドが見つからない等）はここで打ち切る
        logger.error("ExifToolの実行に失敗しました: %s", e)
        return False

    if not data:
        # 対象ファイルが1件も見つからなかった場合は処理不要としてFalseを返す
        logger.warning("対象の画像ファイル (.JPG, .NEF) または EXIF データが見つかりませんでした")
        return False

    logger.info("%d 件のファイルを処理中...", len(data))

    # 1. 個別 .exif ファイルの出力
    # 取得した各ファイルのEXIF情報を1件ずつ個別ファイルとして書き出す
    for item in data:
        export_individual_exif(item, output_dir, target_tags=target_tags, tag_labels=tag_labels)

    # 2. 全体TSVの出力
    # まず全アイテムに登場する全タグ名（列名）の集合を作る
    all_tags = set()
    for item in data:
        all_tags.update(item.keys())

    # SourceFile列は先頭に固定表示したいので集合から除外し、後で先頭に付け直す
    all_tags.discard("SourceFile")
    # 残りのタグはアルファベット順にソートしてSourceFileの後ろに並べる（列の並びを安定させるため）
    sorted_tags = ["SourceFile"] + sorted(list(all_tags))

    try:
        with open(output_tsv_path, "w", encoding="utf-8") as f:
            # 1行目にヘッダー（タグ名の一覧）をタブ区切りで書き出す
            f.write("\t".join(sorted_tags) + "\n")
            # 続けて各ファイルの値をタグの並び順に従って1行ずつ書き出す
            for item in data:
                row_values = []
                for tag in sorted_tags:
                    val = item.get(tag, "")
                    if isinstance(val, (list, dict)):
                        val = str(val)
                    # タブ・改行・キャリッジリターンが値に含まれるとTSVの列がずれるためスペースに置換
                    val_str = str(val).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                    row_values.append(val_str)
                f.write("\t".join(row_values) + "\n")

        logger.info("処理完了: %s フォルダにTSV(%s)および個別.exifを出力しました", output_dir, tsv_filename)
        return True

    except Exception as e:
        # TSV書き込み時に予期しないエラーが発生した場合はスタックトレース付きでログに記録
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

    # 別プロセスとして起動されるため config.json を自分で読み込み直す
    # （出力対象タグ・日本語ラベルのカスタマイズを config.json 経由で反映するため）
    app_config = None
    try:
        app_config = ConfigManager()
    except (FileNotFoundError, ValueError) as e:
        # config.jsonが存在しない・不正な場合はNoneのまま（デフォルトのタグ設定を使用）継続する
        logger.debug("config.json の自動読み込みなし（デファイルのタグ設定を使用）: %s", e)

    # 実際にEXIF抽出処理を実行する
    extract_all_exif_recursive(
        input_directory,
        tsv_filename=tsv_name,
        exiftool_path=exiftool_path,
        config=app_config,
    )
