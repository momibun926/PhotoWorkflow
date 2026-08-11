"""STEP 1: GPXログに基づく位置情報の付与モジュール."""

import logging
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def apply_gps_tags(from_camera_dir: Path, gpx_files: List[Path]) -> bool:
    """GPXファイルをもとに写真へGPS位置情報を書き込みます。"""
    print("\n==================================================")
    print(" 【開始】STEP 1: GPS位置情報の書き込み")
    print("==================================================")
    logger.info("--- STEP 1: GPS位置情報の書き込み ---")

    if not gpx_files:
        print(" -> GPXファイルが見つからないため、処理をスキップします。")
        logger.info("GPXファイルが存在しないためスキップ")
        print("\n")
        print(" 【処理のサマリー】")
        print("    ・検出GPXファイル: 0 件")
        print("    ・ステータス: スキップ")
        print("==================================================")
        print(" 【終了】STEP 1: GPS位置情報の書き込み 終了")
        print("==================================================")
        return True

    logger.info("%d 件のGPXファイルを検出しました。", len(gpx_files))
    cmd = ["exiftool"] + [f"-geotag={gpx}" for gpx in gpx_files]
    cmd.extend(["-geosync=+09:00", "-overwrite_original", str(from_camera_dir)])

    try:
        logger.info("ExifToolを実行中...")
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        stdout_lines = result.stdout.splitlines() if result.stdout else []
        stderr_lines = result.stderr.splitlines() if result.stderr else []

        for line in stdout_lines:
            logger.info(line)
        if result.stderr:
            logger.warning("ExifTool 警告/エラー詳細:\n%s", result.stderr.strip())

        updated_count = 0
        unchanged_count = 0
        error_counter = Counter()

        all_lines = stdout_lines + stderr_lines
        for line in all_lines:
            line_str = line.strip()

            m_up = re.search(r"(\d+)\s+image files updated", line_str)
            if m_up:
                updated_count = int(m_up.group(1))

            m_un = re.search(r"(\d+)\s+image files unchanged", line_str)
            if m_un:
                unchanged_count = int(m_un.group(1))

            if line_str.startswith("Warning:") or line_str.startswith("Error:"):
                clean_msg = re.sub(r"\s*-\s*.*$", "", line_str)
                clean_msg = re.sub(r" (in|for) file '.*?'", "", clean_msg)
                error_counter[clean_msg] += 1

        print("\n")
        print(" 【処理のサマリー】")
        print(f"    ・検出GPXファイル : {len(gpx_files)} 件")
        print(f"    ・位置情報付与成功: {updated_count} 件")
        print(f"    ・変更なし        : {unchanged_count} 件")
        if error_counter:
            print("    ・エラー・警告内訳:")
            for err_msg, count in error_counter.items():
                print(f"       * {err_msg}: {count} 件")
        print("\n")
        print("==================================================")
        print(" 【終了】STEP 1: GPS位置情報の書き込み 終了")
        print("==================================================")
        return True

    except Exception as e:
        print("\n -> [エラー] GPS書き込み中に問題が発生しました")
        logger.error("GPS情報の書き込み中に例外が発生しました: %s", e, exc_info=True)
        print("==================================================")
        return False