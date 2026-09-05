"""STEP 2: 写真ファイルの分類・コピー・整理モジュール。"""

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import send2trash
except ImportError:
    send2trash = None

from .exif_utils import ExifReader
from . import constants

logger = logging.getLogger(__name__)


def categorize_files(from_dir: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    """ディレクトリ内のファイルを(JPEG, NEF, GPX)に分類。
    
    Args:
        from_dir: 対象ディレクトリパス
        
    Returns:
        (JPEG ファイルリスト, RAW ファイルリスト, GPX ファイルリスト)のタプル
    """
    jpeg_files: List[Path] = []
    nef_files: List[Path] = []
    gpx_files: List[Path] = []

    try:
        for entry in from_dir.iterdir():
            if not entry.is_file():
                continue
            
            ext = entry.suffix.lower()
            
            if ext in constants.JPEG_EXTENSIONS:
                jpeg_files.append(entry)
            elif ext in constants.RAW_EXTENSIONS:
                nef_files.append(entry)
            elif ext in constants.GPX_EXTENSIONS:
                gpx_files.append(entry)

        logger.info("ファイル分類完了: JPEG=%d, RAW=%d, GPX=%d",
                   len(jpeg_files), len(nef_files), len(gpx_files))
    except Exception as e:
        logger.error("ファイル分類エラー: %s", e, exc_info=True)
        raise

    return jpeg_files, nef_files, gpx_files


def copy_and_organize_photos(
    jpeg_files: List[Path],
    nef_files: List[Path],
    to_note_dir: Path,
    to_amazon_jpeg_dir: Path,
    base_dir: Path,
) -> bool:
    """写真ファイルを規定のディレクトリへコピーし、元の写真をゴミ箱へ移動。
    
    Args:
        jpeg_files: JPEG ファイルリスト
        nef_files: RAW ファイルリスト
        to_note_dir: ブログ用コピー先ディレクトリ
        to_amazon_jpeg_dir: Amazon フォト用コピー先ディレクトリ
        base_dir: RAW ファイルのアーカイブベースディレクトリ
        
    Returns:
        処理が成功した場合True
    """
    print("\n==================================================")
    print(" 【開始】STEP 2: 写真ファイルのコピー・整理")
    print("==================================================")
    logger.info("--- STEP 2: 写真ファイルのコピー・整理 ---")

    # ディレクトリ作成
    try:
        to_note_dir.mkdir(parents=True, exist_ok=True)
        to_amazon_jpeg_dir.mkdir(parents=True, exist_ok=True)
        logger.info("出力ディレクトリ作成完了")
    except Exception as e:
        logger.error("ディレクトリ作成エラー: %s", e)
        print(f" -> [エラー] ディレクトリ作成に失敗しました: {e}")
        return False

    # JPEG コピー
    total_jpeg = len(jpeg_files)
    logger.info("JPEG ファイル (%d 件) のコピーを開始", total_jpeg)
    for idx, jpeg in enumerate(jpeg_files, start=1):
        try:
            shutil.copy2(jpeg, to_note_dir / jpeg.name)
            shutil.copy2(jpeg, to_amazon_jpeg_dir / jpeg.name)
            print(f"\r  - JPEG コピー中: {idx}/{total_jpeg} 件", end="", flush=True)
            logger.debug("JPEG コピー完了 [%d/%d]: %s", idx, total_jpeg, jpeg.name)
        except Exception as e:
            logger.error("JPEG コピーエラー (%s): %s", jpeg.name, e)
            print(f"\n  [警告] {jpeg.name} のコピーに失敗しました: {e}")

    if total_jpeg > 0:
        print()

    # RAW ファイルコピー
    total_nef = len(nef_files)
    logger.info("RAW ファイル (%d 件) のコピーを開始", total_nef)
    
    if total_nef > 0:
        exif_reader = ExifReader()
        date_map = exif_reader.get_exif_dates_batch(nef_files)
        
        for idx, nef in enumerate(nef_files, start=1):
            try:
                date_str = date_map.get(nef)
                
                # 撮影日（8桁数字: YYYYMMDD）が得られている場合に YYYY\MM\YYYYMMDD 構造を作成
                if date_str and len(date_str) == 8 and date_str.isdigit():
                    year = date_str[:4]
                    month = date_str[4:6]
                    target_dir = base_dir / year / month / date_str
                else:
                    logger.warning("RAW ファイルの撮影日が取得できません: %s", nef.name)
                    target_dir = base_dir / "unknown"
                
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(nef, target_dir / nef.name)
                
                print(f"\r  - RAW コピー中: {idx}/{total_nef} 件", end="", flush=True)
                logger.debug("RAW コピー完了 [%d/%d]: %s -> %s", idx, total_nef, nef.name, target_dir)
            except Exception as e:
                logger.error("RAW コピーエラー (%s): %s", nef.name, e)
                print(f"\n  [警告] {nef.name} のコピーに失敗しました: {e}")
        
        print()

    # ゴミ箱移動
    files_to_trash = jpeg_files + nef_files
    trashed_count = 0
    logger.info("元ファイル (%d 件) をゴミ箱へ移動開始", len(files_to_trash))
    print("  - 取り込み元の写真をゴミ箱へ移動中...")

    if send2trash is None:
        logger.warning("send2trash がインストールされていません。pip install send2trash を実行してください")
        print("  [警告] send2trash がインストールされていないため、ゴミ箱への移動をスキップします")
    else:
        for file_p in files_to_trash:
            try:
                send2trash.send2trash(str(file_p))
                trashed_count += 1
                logger.debug("ゴミ箱移動: %s", file_p.name)
            except Exception as e:
                logger.error("ゴミ箱移動エラー (%s): %s", file_p.name, e)
                print(f"  [警告] {file_p.name} のゴミ箱移動に失敗しました: {e}")

    print("\n")
    print(" 【処理のサマリー】")
    print(f"    ・JPEG コピー完了 : {total_jpeg} 件")
    print(f"    ・RAW コピー完了  : {total_nef} 件")
    print(f"    ・ゴミ箱移動完了 : {trashed_count}/{len(files_to_trash)} 件")
    print("\n")
    print("==================================================")
    print(" 【終了】STEP 2: 写真ファイルのコピー・整理 終了")
    print("==================================================")
    
    logger.info("STEP 2 完了: JPEG=%d, RAW=%d, Trash=%d/%d",
               total_jpeg, total_nef, trashed_count, len(files_to_trash))
    return True