"""STEP 2: 写真ファイルの分類・コピー・整理モジュール。"""

import logging
import shutil
from pathlib import Path
from typing import Any, List, Optional, Tuple

try:
    # ゴミ箱移動用の外部ライブラリ（未インストールでも動作を継続できるようにフォールバックする）
    import send2trash
except ImportError:
    # インストールされていない環境ではNoneのままにしておき、使用箇所で分岐する
    send2trash = None

# 同一パッケージ内の exif_utils モジュールから ExifReader（EXIF日付取得用）をインポート
from .exif_utils import ExifReader
# 同一パッケージ内の constants モジュール（拡張子一覧などの定数）をインポート
from . import constants

# このモジュール専用のロガーを取得
logger = logging.getLogger(__name__)


def categorize_files(from_dir: Path, config: Optional[Any] = None) -> Tuple[List[Path], List[Path], List[Path]]:
    """ディレクトリ内のファイルを(JPEG, RAW, GPX)に分類。
    
    Args:
        from_dir: 対象ディレクトリパス
        config: ConfigManager。対応拡張子のカスタマイズに使用
            （config.jsonの'file_types'。Noneの場合はconstants.pyのデフォルト拡張子を使用）
        
    Returns:
        (JPEG ファイルリスト, RAW ファイルリスト, GPX ファイルリスト)のタプル
    """
    # configが渡されていればconfig.jsonのfile_types設定から拡張子リストを取得し、
    # 渡されていなければconstants.pyに定義されたデフォルトの拡張子リストを使う
    if config is not None:
        ext_map = config.get_file_extensions()
        jpeg_exts, raw_exts, gpx_exts = ext_map["jpeg"], ext_map["raw"], ext_map["gpx"]
    else:
        jpeg_exts, raw_exts, gpx_exts = constants.JPEG_EXTENSIONS, constants.RAW_EXTENSIONS, constants.GPX_EXTENSIONS

    # 分類結果を格納するための空リストを用意
    jpeg_files: List[Path] = []
    nef_files: List[Path] = []
    gpx_files: List[Path] = []

    try:
        # 対象ディレクトリ直下（サブディレクトリは含まない）のエントリを1件ずつ確認
        for entry in from_dir.iterdir():
            if not entry.is_file():
                # ディレクトリなど、ファイル以外は対象外としてスキップ
                continue
            
            # 拡張子を小文字化して比較（大文字小文字の表記ゆれを吸収するため）
            ext = entry.suffix.lower()
            
            if ext in jpeg_exts:
                jpeg_files.append(entry)
            elif ext in raw_exts:
                nef_files.append(entry)
            elif ext in gpx_exts:
                gpx_files.append(entry)

        # 分類結果の件数をログに記録
        logger.info("ファイル分類完了: JPEG=%d, RAW=%d, GPX=%d",
                   len(jpeg_files), len(nef_files), len(gpx_files))
    except Exception as e:
        # ディレクトリ走査中に予期しない例外が発生した場合はログを残したうえで呼び出し元に再送出する
        logger.error("ファイル分類エラー: %s", e, exc_info=True)
        raise

    return jpeg_files, nef_files, gpx_files


def copy_and_organize_photos(
    jpeg_files: List[Path],
    nef_files: List[Path],
    to_note_dir: Path,
    copy_targets: List[Path],
    base_dir: Path,
    config: Optional[Any] = None,
) -> bool:
    """写真ファイルを規定のディレクトリへコピーし、元の写真をゴミ箱へ移動。
    
    Args:
        jpeg_files: JPEG ファイルリスト
        nef_files: RAW ファイルリスト
        to_note_dir: ブログ用コピー先ディレクトリ（フレーム付与・EXIF抽出の対象にもなる主要出力先）
        copy_targets: JPEGの追加コピー先ディレクトリのリスト（0件以上。
            例: Amazon Photos用フォルダ。config.jsonの'copy_targets'で
            台数を自由に増減できる。詳細は ConfigManager.get_copy_targets() 参照）
        base_dir: RAW ファイルのアーカイブベースディレクトリ
        config: ConfigManager。exiftoolのパス解決に使用（Noneの場合は自動検索）
        
    Returns:
        処理が成功した場合True
    """
    # 処理開始をコンソール表示・ログの両方に記録
    print("\n==================================================")
    print(" 【開始】STEP 2: 写真ファイルのコピー・整理")
    print("==================================================")
    logger.info("--- STEP 2: 写真ファイルのコピー・整理 ---")

    # ディレクトリ作成
    # コピー先となる各ディレクトリ（to_note_dirおよびcopy_targets全て）を事前に作成しておく
    try:
        to_note_dir.mkdir(parents=True, exist_ok=True)
        for target_dir in copy_targets:
            target_dir.mkdir(parents=True, exist_ok=True)
        logger.info("出力ディレクトリ作成完了（コピー先 %d 件）", len(copy_targets))
    except Exception as e:
        # ディレクトリ作成に失敗した場合はこれ以降の処理を続けても意味がないため、ここで打ち切る
        logger.error("ディレクトリ作成エラー: %s", e)
        print(f" -> [エラー] ディレクトリ作成に失敗しました: {e}")
        return False

    # JPEG コピー
    # JPEGファイルはto_note_dirおよび全てのcopy_targetsに1枚ずつコピーする
    total_jpeg = len(jpeg_files)
    logger.info("JPEG ファイル (%d 件) のコピーを開始 (コピー先: to_note + 追加%d件)", total_jpeg, len(copy_targets))
    for idx, jpeg in enumerate(jpeg_files, start=1):
        try:
            # まずブログ用ディレクトリへコピー
            shutil.copy2(jpeg, to_note_dir / jpeg.name)
            # 続けて追加のコピー先（copy_targets）全てにもコピー
            for target_dir in copy_targets:
                shutil.copy2(jpeg, target_dir / jpeg.name)
            # 進捗をその場（同じ行）で上書き表示する（末尾に改行を入れずキャリッジリターンで戻す）
            print(f"\r  - JPEG コピー中: {idx}/{total_jpeg} 件", end="", flush=True)
            logger.debug("JPEG コピー完了 [%d/%d]: %s", idx, total_jpeg, jpeg.name)
        except Exception as e:
            # 1枚のコピーに失敗しても他のファイルの処理は継続する
            logger.error("JPEG コピーエラー (%s): %s", jpeg.name, e)
            print(f"\n  [警告] {jpeg.name} のコピーに失敗しました: {e}")

    if total_jpeg > 0:
        # 進捗表示（\rで上書きしていた行）の後に改行を入れて次のセクションを見やすくする
        print()

    # RAW ファイルコピー
    # RAWファイルは撮影日ごとのフォルダ（年/月/年月日）に振り分けてコピーする
    total_nef = len(nef_files)
    logger.info("RAW ファイル (%d 件) のコピーを開始", total_nef)
    
    if total_nef > 0:
        # EXIFから撮影日を取得するためのリーダーを用意し、全RAWファイル分をまとめて取得（バッチ処理で高速化）
        exif_reader = ExifReader(config=config)
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
                    # 撮影日が取得できない場合はunknownフォルダにまとめる
                    logger.warning("RAW ファイルの撮影日が取得できません: %s", nef.name)
                    target_dir = base_dir / "unknown"
                
                # コピー先ディレクトリ（年/月/日、またはunknown）を必要に応じて作成してからコピー
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(nef, target_dir / nef.name)
                
                # JPEGと同様、進捗をその場で上書き表示
                print(f"\r  - RAW コピー中: {idx}/{total_nef} 件", end="", flush=True)
                logger.debug("RAW コピー完了 [%d/%d]: %s -> %s", idx, total_nef, nef.name, target_dir)
            except Exception as e:
                # 1枚のコピーに失敗しても他のファイルの処理は継続する
                logger.error("RAW コピーエラー (%s): %s", nef.name, e)
                print(f"\n  [警告] {nef.name} のコピーに失敗しました: {e}")
        
        print()

    # ゴミ箱移動
    # コピーが完了した元のJPEG・RAWファイルはゴミ箱（OSのゴミ箱機能）へ移動して取り込み元を空にする
    files_to_trash = jpeg_files + nef_files
    trashed_count = 0
    logger.info("元ファイル (%d 件) をゴミ箱へ移動開始", len(files_to_trash))
    print("  - 取り込み元の写真をゴミ箱へ移動中...")

    if send2trash is None:
        # send2trashライブラリが無い環境では安全のためゴミ箱移動自体をスキップする
        logger.warning("send2trash がインストールされていません。pip install send2trash を実行してください")
        print("  [警告] send2trash がインストールされていないため、ゴミ箱への移動をスキップします")
    else:
        for file_p in files_to_trash:
            try:
                send2trash.send2trash(str(file_p))
                trashed_count += 1
                logger.debug("ゴミ箱移動: %s", file_p.name)
            except Exception as e:
                # 1件の移動に失敗しても他のファイルの移動は継続する
                logger.error("ゴミ箱移動エラー (%s): %s", file_p.name, e)
                print(f"  [警告] {file_p.name} のゴミ箱移動に失敗しました: {e}")

    # 最終的な処理結果のサマリーを表示・ログに記録
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
