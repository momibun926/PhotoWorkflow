"""STEP 1: GPXログに基づく位置情報の付与モジュール。"""

import logging
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import List, Tuple

from . import constants

logger = logging.getLogger(__name__)

# 正規表現パターンのプリコンパイル
PATTERN_UPDATED = re.compile(r"(\d+)\s+image files updated")
PATTERN_UNCHANGED = re.compile(r"(\d+)\s+image files unchanged")


def apply_gps_tags(from_camera_dir: Path, gpx_files: List[Path]) -> bool:
    """GPXファイルをもとに写真へGPS位置情報を書き込み。
    
    Args:
        from_camera_dir: 対象写真ディレクトリ
        gpx_files: GPX ファイルリスト
        
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
    
    # ExifTool コマンド構築
    cmd = ["exiftool"]
    cmd.extend([f"-geotag={gpx}" for gpx in gpx_files])
    cmd.extend([
        f"-geosync={constants.GPS_SYNC_TIMEZONE}",
        "-overwrite_original",
        str(from_camera_dir)
    ])

    try:
        logger.info("ExifTool コマンド実行中...")
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False  # 終了コードの検証は後で行う
        )

        stdout_lines = result.stdout.splitlines() if result.stdout else []
        stderr_lines = result.stderr.splitlines() if result.stderr else []

        # ログ出力
        for line in stdout_lines:
            logger.info(line)
        
        if result.stderr:
            logger.warning("ExifTool 警告/エラー詳細:\n%s", result.stderr.strip())

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

    except FileNotFoundError:
        error_msg = "exiftool が見つかりません。PATH に exiftool を追加するか、install してください"
        logger.error(error_msg)
        print(f" -> [エラー] {error_msg}")
        return False
    except subprocess.TimeoutExpired:
        error_msg = "ExifTool の実行がタイムアウトしました"
        logger.error(error_msg)
        print(f" -> [エラー] {error_msg}")
        return False
    except Exception as e:
        error_msg = f"GPS情報の書き込み中に予期しないエラーが発生しました: {e}"
        logger.error(error_msg, exc_info=True)
        print(f" -> [エラー] {error_msg}")
        return False
