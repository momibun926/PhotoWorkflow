"""STEP 1: GPXログに基づく位置情報の付与モジュール。"""

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional

from . import constants
from .exiftool_client import ExifToolClient, ExifToolError

logger = logging.getLogger(__name__)

# 正規表現パターンのプリコンパイル
PATTERN_UPDATED = re.compile(r"(\d+)\s+image files updated")
PATTERN_UNCHANGED = re.compile(r"(\d+)\s+image files unchanged")


def apply_gps_tags(from_camera_dir: Path, gpx_files: List[Path], config: Optional[Any] = None) -> bool:
    """GPXファイルをもとに写真へGPS位置情報を書き込み。

    Args:
        from_camera_dir: 対象写真ディレクトリ
        gpx_files: GPX ファイルリスト
        config: ConfigManager。exiftoolのパス解決に使用（Noneの場合は自動検索）

    Returns:
        処理が成功した場合True
    """
    print("\n==================================================")
    print(" 【開始】STEP 1: GPS位置情報の書き込み")
    print("==================================================")
    logger.info("--- STEP 1: GPS位置情報の書き込み ---")

    if not gpx_files:
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
        error_msg = f"対象ディレクトリが見つかりません: {from_camera_dir}"
        logger.error(error_msg)
        print(f" -> [エラー] {error_msg}")
        return False

    logger.info("%d 件の GPX ファイルを検出しました", len(gpx_files))

    et = ExifToolClient(config=config)
    geosync = config.get_gps_sync_timezone() if config is not None else constants.GPS_SYNC_TIMEZONE

    try:
        logger.info("ExifTool コマンド実行中... (path=%s, geosync=%s)", et.path, geosync)
        stdout, stderr = et.geotag_from_gpx(
            target_dir=from_camera_dir,
            gpx_files=gpx_files,
            geosync=geosync,
        )

        stdout_lines = stdout.splitlines() if stdout else []
        stderr_lines = stderr.splitlines() if stderr else []

        # ログ出力
        for line in stdout_lines:
            logger.info(line)

        if stderr:
            logger.warning("ExifTool 警告/エラー詳細:\n%s", stderr.strip())

        # 処理結果の解析
        updated_count = 0
        unchanged_count = 0
        error_counter: Counter = Counter()

        all_lines = stdout_lines + stderr_lines
        for line in all_lines:
            line_str = line.strip()

            # 更新ファイル数の抽出
            m_up = PATTERN_UPDATED.search(line_str)
            if m_up:
                updated_count = int(m_up.group(1))

            # 変更なしのファイル数
            m_un = PATTERN_UNCHANGED.search(line_str)
            if m_un:
                unchanged_count = int(m_un.group(1))

            # エラー・警告の集計
            if line_str.startswith("Warning:") or line_str.startswith("Error:"):
                # エラーメッセージの正規化
                clean_msg = re.sub(r"\s*-\s*.*$", "", line_str)
                clean_msg = re.sub(r" (in|for) file '.*?'", "", clean_msg)
                error_counter[clean_msg] += 1

        # 結果表示
        print("\n")
        print(" 【処理のサマリー】")
        print(f"    ・検出GPXファイル : {len(gpx_files)} 件")
        print(f"    ・位置情報付与成功: {updated_count} 件")
        print(f"    ・変更なし        : {unchanged_count} 件")

        if error_counter:
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
        logger.error("GPSタグ書き込みエラー: %s", e)
        print(f" -> [エラー] {e}")
        return False
    except Exception as e:
        error_msg = f"GPS情報の書き込み中に予期しないエラーが発生しました: {e}"
        logger.error(error_msg, exc_info=True)
        print(f" -> [エラー] {error_msg}")
        return False
