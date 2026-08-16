"""写真整理ワークフロー統括プログラム (main.py)."""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Tuple, List

# 自作モジュールのインポート
from modules.config_manager import ConfigManager
from modules.gps_tagger import apply_gps_tags
from modules.photo_copier import categorize_files, copy_and_organize_photos
from modules.frame_processor import run_frame_processing
from modules import constants

# --- ログ設定 ---
LOG_FILE_PATH = Path(__file__).parent / "photo_organizer.log"

# ルートロガーの設定（すべてのモジュールに適用）
logging.basicConfig(
    level=logging.INFO,
    format=constants.LOG_FORMAT,
    datefmt=constants.LOG_DATE_FORMAT,
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)

# ファイルハンドラのレベル調整
for handler in logging.root.handlers:
    if isinstance(handler, logging.FileHandler):
        handler.setLevel(logging.INFO)
    elif isinstance(handler, logging.StreamHandler):
        handler.setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def prompt_next_action(next_step_name: str) -> str:
    """次のステップへの進行確認を行います。
    
    Args:
        next_step_name: ステップ名
        
    Returns:
        'Y' (次へ進む)、'S' (スキップ)、'A' (中断)のいずれか
    """
    print("\n\n--------------------------------------------------")
    while True:
        try:
            choice = (
                input(
                    f"次のステップ [{next_step_name}] に進みますか？\n"
                    "  [Y] 次へ進む / [S] 次をスキップ / [A] 処理を中断 (Y/S/A): "
                )
                .strip()
                .upper()
            )
            if choice in ["Y", "S", "A"]:
                print("--------------------------------------------------\n\n")
                logger.info("ユーザー選択: %s", choice)
                return choice
            print(" [!] 無効な入力です。'Y'、'S'、'A' のいずれかを入力してください。")
        except KeyboardInterrupt:
            logger.warning("ユーザーが Ctrl+C で中断")
            print("\n[!] 処理が中断されました")
            return "A"
        except Exception as e:
            logger.error("入力エラー: %s", e)
            print(f" [!] 入力エラーが発生しました: {e}")


def prompt_manual_gps_option() -> bool:
    """手動でGPS情報を付与するかどうかを確認。
    
    Returns:
        True の場合は起動、False の場合はスキップ
    """
    print("\n\n--------------------------------------------------")
    while True:
        try:
            choice = (
                input(
                    "手動GPS付与ツールを起動しますか？\n"
                    "  [Y] 起動する / [S] 起動せずに次へ進む (Y/S): "
                )
                .strip()
                .upper()
            )
            if choice in ["Y", "S"]:
                print("--------------------------------------------------\n")
                result = choice == "Y"
                logger.info("手動GPS付与ツール: %s", "起動" if result else "スキップ")
                return result
            print(" [!] 無効な入力です。'Y' または 'S' を入力してください。")
        except KeyboardInterrupt:
            logger.warning("ユーザーが Ctrl+C で中断")
            print("\n[!] 入力がキャンセルされました")
            return False
        except Exception as e:
            logger.error("入力エラー: %s", e)
            print(f" [!] 入力エラーが発生しました: {e}")
            return False


def run_manual_gps_tagger() -> None:
    """手動GPS付与GUIツールを起動。"""
    script_path = Path(__file__).parent / "modules" / "manual_gps_tagger.py"
    
    if not script_path.exists():
        msg = f"手動GPS付与プログラムが見つかりません: {script_path}"
        print(f"[警告] {msg}\n")
        logger.warning(msg)
        return

    print("手動GPS付与ツールを起動しています...")
    logger.info("手動GPS付与ツールを起動: %s", script_path)
    
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            check=False,
            timeout=3600  # 1時間のタイムアウト
        )
        if result.returncode == 0:
            print("手動GPS付与ツールが正常に終了しました。")
            logger.info("手動GPS付与ツール終了: 正常")
        else:
            logger.warning("手動GPS付与ツール終了: 終了コード %d", result.returncode)
            print(f"[警告] 手動GPS付与ツールが異常終了しました (終了コード: {result.returncode})")
    except subprocess.TimeoutExpired:
        msg = "手動GPS付与ツールの実行がタイムアウトしました (1時間)"
        print(f"\n[エラー] {msg}")
        logger.error(msg)
    except Exception as e:
        msg = f"手動GPS付与ツールの実行中にエラーが発生しました: {e}"
        print(f"\n[エラー] {msg}")
        logger.error(msg, exc_info=True)


def main() -> None:
    """メイン実行エントリーポイント。"""
    print("==================================================")
    print("      写真整理ワークフロープログラム")
    print("==================================================")

    try:
        # 設定の読み込み
        config_file = Path(__file__).parent / "config.json"
        try:
            config = ConfigManager(config_file)
        except (FileNotFoundError, ValueError) as e:
            print(f"\n[エラー] {e}")
            logger.error("設定読み込みエラー: %s", e)
            return

        # ディレクトリパスを取得
        try:
            from_camera_dir = config.get_directory("from_camera")
            to_amazon_jpeg_dir = config.get_directory("to_amazon_jpeg")
            to_note_dir = config.get_directory("to_note")
            base_dir = config.get_directory("base")
            flame_yaml = config.get("external_tools.flame_config")
        except KeyError as e:
            print(f"\n[エラー] 設定キーが不足しています: {e}")
            logger.error("設定キーエラー: %s", e)
            return

        # 入力ディレクトリの確認
        if not from_camera_dir.exists():
            msg = f"取り込み元フォルダが存在しません: {from_camera_dir}"
            print(f"\n[エラー] {msg}")
            logger.error(msg)
            return

        # ファイル分類
        logger.info("ファイル分類開始")
        jpeg_files, nef_files, gpx_files = categorize_files(from_camera_dir)

        # --------------------------------------------------
        # STEP 1: GPS書き込み
        # --------------------------------------------------
        logger.info("STEP 1: GPS 書き込み開始")
        if not apply_gps_tags(from_camera_dir, gpx_files):
            print("\nGPSタグ書き込みで重大なエラーが発生したため中断します。")
            logger.error("STEP 1 エラーで中止")
            return

        # --------------------------------------------------
        # STEP 1.5: 手動GPS情報付与 (追加ステップ)
        # --------------------------------------------------
        if prompt_manual_gps_option():
            run_manual_gps_tagger()
            # 手動付与でファイルが変更された可能性があるため再分類
            logger.info("手動GPS付与後のファイル再分類")
            jpeg_files, nef_files, gpx_files = categorize_files(from_camera_dir)

        action = prompt_next_action("STEP 2: 写真整理・コピー")
        if action == "A":
            print("\nユーザーにより処理が中断されました。")
            logger.info("ユーザーが STEP 2 で中止")
            return

        # --------------------------------------------------
        # STEP 2: 写真整理・コピー
        # --------------------------------------------------
        if action == "Y":
            logger.info("STEP 2: 写真整理・コピー開始")
            copy_and_organize_photos(
                jpeg_files=jpeg_files,
                nef_files=nef_files,
                to_note_dir=to_note_dir,
                to_amazon_jpeg_dir=to_amazon_jpeg_dir,
                base_dir=base_dir,
            )
        elif action == "S":
            print("\n[STEP 2: 写真整理・コピー] をスキップしました。")
            logger.info("STEP 2 をスキップ")

        action = prompt_next_action("STEP 3: フレーム付与処理")
        if action == "A":
            print("\nユーザーにより処理が中断されました。")
            logger.info("ユーザーが STEP 3 で中止")
            return

        # --------------------------------------------------
        # STEP 3: フレーム付与処理
        # --------------------------------------------------
        if action == "Y":
            logger.info("STEP 3: フレーム付与処理開始")
            run_frame_processing(
                script_path_or_unused=Path(),
                target_dir=to_note_dir,
                yaml_config=str(flame_yaml),
            )
        elif action == "S":
            print("\n[STEP 3: フレーム付与処理] をスキップしました。")
            logger.info("STEP 3 をスキップ")

        print("\n==================================================")
        print(" すべてのワークフロー工程が終了しました！")
        print("==================================================")
        logger.info("すべてのワークフロー工程が正常に完了しました")

    except KeyboardInterrupt:
        print("\n\n[!] ユーザーが Ctrl+C で中断しました")
        logger.warning("ユーザーが Ctrl+C で中止")
    except Exception as e:
        print(f"\n[重大なエラーが発生しました]: {e}")
        logger.critical("予期せぬ重大なエラーが発生しました: %s", e, exc_info=True)


if __name__ == "__main__":
    try:
        logger.info("=" * 50)
        logger.info("写真整理ワークフロープログラムを開始します")
        logger.info("=" * 50)
        main()
    except KeyboardInterrupt:
        print("\n\n[!] プログラムが Ctrl+C で中断されました")
        logger.warning("プログラムが Ctrl+C で中止されました")
        sys.exit(130)
    except Exception as e:
        print(f"\n[エラー] 予期しないエラーが発生しました: {e}")
        logger.critical("予期しないエラーが発生しました", exc_info=True)
        sys.exit(1)
    finally:
        print()
        try:
            input("キーを押すと終了します...")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            logger.info("=" * 50)
            logger.info("プログラムを終了します")
            logger.info("=" * 50)