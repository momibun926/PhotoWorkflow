"""STEP 1: GPXログに基づく位置情報の付与モジュール。"""

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional

# 同一パッケージ内の constants モジュール（タイムゾーン等の定数）をインポート
from . import constants
# 同一パッケージ内の exiftool_client モジュールから ExifToolClient・ExifToolError をインポート
from .exiftool_client import ExifToolClient, ExifToolError

# このモジュール専用のロガーを取得
logger = logging.getLogger(__name__)

# 正規表現パターンのプリコンパイル
# exiftoolのgeotag実行結果（標準出力・標準エラー）から件数を抽出するための正規表現
PATTERN_UPDATED = re.compile(r"(\d+)\s+image files updated")      # 例: "3 image files updated"
PATTERN_UNCHANGED = re.compile(r"(\d+)\s+image files unchanged")  # 例: "1 image files unchanged"


def apply_gps_tags(from_camera_dir: Path, gpx_files: List[Path], config: Optional[Any] = None) -> bool:
    """GPXファイルをもとに写真へGPS位置情報を書き込み。

    Args:
        from_camera_dir: 対象写真ディレクトリ
        gpx_files: GPX ファイルリスト
        config: ConfigManager。exiftoolのパス解決に使用（Noneの場合は自動検索）

    Returns:
        処理が成功した場合True
    """
    # 処理開始をコンソール表示・ログの両方に記録
    print("\n==================================================")
    print(" 【開始】STEP 1: GPS位置情報の書き込み")
    print("==================================================")
    logger.info("--- STEP 1: GPS位置情報の書き込み ---")

    if not gpx_files:
        # GPXファイルが1件も無い場合はGPSタグ付けを行う対象が無いため、
        # エラーにはせず正常終了（True）としてスキップする
        logger.info("GPXファイルが存在しないためスキップ")
        print(" -> GPXファイルが見つからないため、処理をスキップします。")
        print("\n")
        print(" 【処理のサマリー】")
        print("    ・検出GPXファイル: 0 件")
        print("    ・ステータス: スキップ")
        print("==================================================")
        print(" 【終了】STEP 1: GPS位置情報の書き込み 終了")
        print("==================================================")
        return True

    if not from_camera_dir.exists():
        # 対象ディレクトリ自体が存在しない場合はエラーとして処理を打ち切る
        error_msg = f"対象ディレクトリが見つかりません: {from_camera_dir}"
        logger.error(error_msg)
        print(f" -> [エラー] {error_msg}")
        return False

    logger.info("%d 件の GPX ファイルを検出しました", len(gpx_files))

    # ExifToolClient のインスタンスを生成
    et = ExifToolClient(config=config)
    # geosync（GPXのタイムスタンプと写真のタイムスタンプの時刻ずれ補正用タイムゾーン）を
    # configから取得。configが無い場合はconstants.pyのデフォルト値を使う
    geosync = config.get_gps_sync_timezone() if config is not None else constants.GPS_SYNC_TIMEZONE

    try:
        logger.info("ExifTool コマンド実行中... (path=%s, geosync=%s)", et.path, geosync)
        # exiftoolの-geotagコマンドを実行し、GPXの軌跡データから各写真の撮影時刻に対応する
        # 緯度経度を割り出してEXIFに書き込む。標準出力・標準エラーの両方を受け取る
        stdout, stderr = et.geotag_from_gpx(
            target_dir=from_camera_dir,
            gpx_files=gpx_files,
            geosync=geosync,
        )

        # 出力が空文字/Noneの場合に備えて空リストにフォールバックしつつ、行単位に分割
        stdout_lines = stdout.splitlines() if stdout else []
        stderr_lines = stderr.splitlines() if stderr else []

        # ログ出力
        # exiftoolの標準出力を1行ずつロガーに記録（実行結果の詳細を残すため）
        for line in stdout_lines:
            logger.info(line)

        if stderr:
            # 標準エラー出力があれば警告としてまとめてログに記録
            logger.warning("ExifTool 警告/エラー詳細:\n%s", stderr.strip())

        # 処理結果の解析
        updated_count = 0
        unchanged_count = 0
        error_counter: Counter = Counter()

        # 標準出力・標準エラーを合わせた全行に対して、更新件数やエラー内容を正規表現で抽出する
        all_lines = stdout_lines + stderr_lines
        for line in all_lines:
            line_str = line.strip()

            # 更新ファイル数の抽出（"N image files updated" のNを取得）
            m_up = PATTERN_UPDATED.search(line_str)
            if m_up:
                updated_count = int(m_up.group(1))

            # 変更なしのファイル数（"N image files unchanged" のNを取得）
            m_un = PATTERN_UNCHANGED.search(line_str)
            if m_un:
                unchanged_count = int(m_un.group(1))

            # エラー・警告の集計
            # "Warning:" または "Error:" で始まる行を集計対象とする
            if line_str.startswith("Warning:") or line_str.startswith("Error:"):
                # エラーメッセージの正規化
                # ファイル固有の詳細部分（" - ..." やファイル名部分）を取り除き、
                # 同種のエラーメッセージをCounterでまとめて集計できるようにする
                clean_msg = re.sub(r"\s*-\s*.*$", "", line_str)
                clean_msg = re.sub(r" (in|for) file '.*?'", "", clean_msg)
                error_counter[clean_msg] += 1

        # 結果表示
        # 処理件数のサマリーをコンソールとログの両方に出力する
        print("\n")
        print(" 【処理のサマリー】")
        print(f"    ・検出GPXファイル : {len(gpx_files)} 件")
        print(f"    ・位置情報付与成功: {updated_count} 件")
        print(f"    ・変更なし        : {unchanged_count} 件")

        if error_counter:
            # エラー・警告が発生していた場合は種類ごとの件数を多い順に表示する
            print("    ・エラー・警告内訳:")
            for err_msg, count in error_counter.most_common():
                print(f"       * {err_msg}: {count} 件")

        print("\n")
        print("==================================================")
        print(" 【終了】STEP 1: GPS位置情報の書き込み 終了")
        print("==================================================")

        logger.info("GPS タグ書き込み完了: 更新=%d, 変更なし=%d", updated_count, unchanged_count)
        return True

    except ExifToolError as e:
        # exiftool実行自体が失敗した場合（コマンドが見つからない、GPX解析エラー等）
        logger.error("GPSタグ書き込みエラー: %s", e)
        print(f" -> [エラー] {e}")
        return False
    except Exception as e:
        # その他の予期しない例外はスタックトレース付きでログに記録し、処理は失敗として終了する
        error_msg = f"GPS情報の書き込み中に予期しないエラーが発生しました: {e}"
        logger.error(error_msg, exc_info=True)
        print(f" -> [エラー] {error_msg}")
        return False
