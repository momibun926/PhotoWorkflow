"""STEP 2: 写真ファイルの分類・コピー・整理モジュール."""

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import send2trash

logger = logging.getLogger(__name__)


def categorize_files(from_dir: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    """ディレクトリ内のファイルを1回の走査で(JPEG, NEF, GPX)に分類します。"""
    jpeg_files, nef_files, gpx_files = [], [], []

    for entry in from_dir.iterdir():
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext in [".jpg", ".jpeg"]:
            jpeg_files.append(entry)
        elif ext == ".nef":
            nef_files.append(entry)
        elif ext == ".gpx":
            gpx_files.append(entry)

    return jpeg_files, nef_files, gpx_files


def get_exif_dates_batch(file_paths: List[Path]) -> Dict[Path, str]:
    """ExifToolを一括実行し、複数ファイルの撮影日(YYYYMMDD)をまとめて取得します。"""
    if not file_paths:
        return {}

    date_map: Dict[Path, str] = {}
    cmd = ["exiftool", "-s3", "-DateTimeOriginal", "-d", "%Y%m%d"] + [str(p) for p in file_paths]

    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )
        dates = result.stdout.splitlines()

        for path, date_str in zip(file_paths, dates):
            date_str = date_str.strip()
            if date_str and len(date_str) == 8 and date_str.isdigit():
                date_map[path] = date_str
            else:
                mtime = os.path.getmtime(path)
                date_map[path] = datetime.fromtimestamp(mtime).strftime("%Y%m%d")
    except Exception as e:
        logger.warning("一括Exif取得に失敗したため、ファイル更新日時を使用します: %s", e)
        for path in file_paths:
            mtime = os.path.getmtime(path)
            date_map[path] = datetime.fromtimestamp(mtime).strftime("%Y%m%d")

    return date_map


def copy_and_organize_photos(
    jpeg_files: List[Path],
    nef_files: List[Path],
    to_note_dir: Path,
    to_amazon_jpeg_dir: Path,
    base_dir: Path,
) -> bool:
    """写真ファイルを規定のディレクトリへコピーし、元の写真をゴミ箱へ移動します。"""
    print("\n==================================================")
    print(" 【開始】STEP 2: 写真ファイルのコピー・整理")
    print("==================================================")
    logger.info("--- STEP 2: 写真ファイルのコピー・整理 ---")

    to_note_dir.mkdir(parents=True, exist_ok=True)
    to_amazon_jpeg_dir.mkdir(parents=True, exist_ok=True)

    # 2.1 JPEGコピー
    total_jpeg = len(jpeg_files)
    logger.info("JPEGファイル (%d 件) のコピーを開始します...", total_jpeg)
    for idx, jpeg in enumerate(jpeg_files, start=1):
        shutil.copy2(jpeg, to_note_dir / jpeg.name)
        shutil.copy2(jpeg, to_amazon_jpeg_dir / jpeg.name)
        print(f"\r  - JPEG コピー中: {idx}/{total_jpeg} 件", end="", flush=True)
        logger.info(" [%d/%d] Copied JPEG: %s", idx, total_jpeg, jpeg.name)
    if total_jpeg > 0:
        print()

    # 2.2 NEFコピー
    total_nef = len(nef_files)
    logger.info("NEFファイル (%d 件) のコピーを開始します...", total_nef)
    if total_nef > 0:
        date_map = get_exif_dates_batch(nef_files)
        for idx, nef in enumerate(nef_files, start=1):
            date_str = date_map.get(nef, datetime.now().strftime("%Y%m%d"))
            target_dir = base_dir / date_str
            target_dir.mkdir(parents=True, exist_ok=True)

            shutil.copy2(nef, target_dir / nef.name)
            print(f"\r  - NEF コピー中: {idx}/{total_nef} 件", end="", flush=True)
            logger.info(" [%d/%d] Copied NEF: %s -> %s", idx, total_nef, nef.name, date_str)
        print()

    # 2.3 ゴミ箱移動
    files_to_trash = jpeg_files + nef_files
    trashed_count = 0
    logger.info("元ファイル (%d 件) をゴミ箱へ移動します...", len(files_to_trash))
    print("  - 取り込み元の写真をゴミ箱へ移動中...")
    for file_p in files_to_trash:
        try:
            send2trash.send2trash(str(file_p))
            trashed_count += 1
            logger.info(" Trash: %s", file_p.name)
        except Exception as e:
            logger.error("ゴミ箱移動に失敗しました (%s): %s", file_p.name, e)

    print("\n")
    print(" 【処理のサマリー】")
    print(f"    ・JPEGコピー完了 : {total_jpeg} 件")
    print(f"    ・NEFコピー完了  : {total_nef} 件")
    print(f"    ・ゴミ箱移動完了 : {trashed_count}/{len(files_to_trash)} 件")
    print("\n")
    print("==================================================")
    print(" 【終了】STEP 2: 写真ファイルのコピー・整理 終了")
    print("==================================================")
    return True