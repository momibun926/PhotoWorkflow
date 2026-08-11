"""写真整理ワークフロー統括プログラム (main.py)."""

import json
import logging
import subprocess
import sys
from pathlib import Path

# 自作モジュールのインポート
from modules.gps_tagger import apply_gps_tags
from modules.photo_copier import categorize_files, copy_and_organize_photos
from modules.frame_processor import run_frame_processing

# --- ログ設定 ---
LOG_FILE_PATH = Path(__file__).parent / "photo_organizer.log"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(console_handler)


def load_config(config_path: Path) -> dict:
    """設定ファイル(JSON)を読み込みます。"""
    logger.info("設定ファイルを読み込んでいます: %s", config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def prompt_next_action(next_step_name: str) -> str:
    """次のステップへの進行確認を行います (A:中断 / N:次へ / S:スキップ)。"""
    print("\n\n--------------------------------------------------")
    while True:
        choice = (
            input(
                f"次のステップ [{next_step_name}] に進みますか？\n"
                "  [N] 次へ進む / [S] 次をスキップ / [A] 処理を中断 (N/S/A): "
            )
            .strip()
            .upper()
        )
        if choice in ["N", "S", "A"]:
            print("--------------------------------------------------\n\n")
            return choice
        print(" [!] 無効な入力です。'N'、'S'、'A' のいずれかを入力してください。")


def prompt_manual_gps_option() -> bool:
    """手動でGPS情報を付与するかどうかを確認します。"""
    print("\n\n--------------------------------------------------")
    while True:
        choice = (
            input(
                "手動GPS付与ツールを起動しますか？\n"
                "  [Y] 起動する / [N] 起動せずに次へ進む (Y/N): "
            )
            .strip()
            .upper()
        )
        if choice in ["Y", "N"]:
            print("--------------------------------------------------\n")
            return choice == "Y"
        print(" [!] 無効な入力です。'Y' または 'N' を入力してください。")


def run_manual_gps_tagger() -> None:
    """modules配下に配置された手動GPS付与GUIツールを起動します。"""
    script_path = Path(__file__).parent / "modules" / "manual_gps_tagger.py"
    
    if not script_path.exists():
        print(f"[警告] 手動GPS付与プログラムが見つかりません: {script_path}\n")
        logger.warning("手動GPS付与プログラムが見つかりませんでした: %s", script_path)
        return

    print("手動GPS付与ツールを起動しています...")
    logger.info("手動GPS付与ツールを起動: %s", script_path)
    
    try:
        subprocess.run([sys.executable, str(script_path)], check=True)
        print("手動GPS付与ツールが終了しました。")
    except Exception as e:
        print(f"\n[エラー] 手動GPS付与ツールの実行中にエラーが発生しました: {e}")
        logger.error("手動GPS付与ツールの起動エラー: %s", e)


def main() -> None:
    """メイン実行エントリーポイント。"""
    print("==================================================")
    print("      写真整理ワークフロープログラム")
    print("==================================================")

    try:
        config_file = Path(__file__).parent / "config.json"
        config = load_config(config_file)

        dirs = config["directories"]
        tools = config["external_tools"]

        from_camera_dir = Path(dirs["from_camera"])
        to_amazon_jpeg_dir = Path(dirs["to_amazon_jpeg"])
        to_note_dir = Path(dirs["to_note"])
        base_dir = Path(dirs["base"])

        flame_yaml = tools["flame_config"]

        if not from_camera_dir.exists():
            print(f"\n[エラー] 取り込み元フォルダが存在しません: {from_camera_dir}")
            logger.error("取り込み元ディレクトリが存在しません: %s", from_camera_dir)
            return

        # ファイル分類
        jpeg_files, nef_files, gpx_files = categorize_files(from_camera_dir)

        # --------------------------------------------------
        # STEP 1: GPS書き込み
        # --------------------------------------------------
        if not apply_gps_tags(from_camera_dir, gpx_files):
            print("\nGPSタグ書き込みで重大なエラーが発生したため中断します。")
            return

        # --------------------------------------------------
        # STEP 1.5: 手動GPS情報付与 (追加ステップ)
        # --------------------------------------------------
        if prompt_manual_gps_option():
            run_manual_gps_tagger()
            # 手動付与でファイルが変更された可能性があるため再分類
            jpeg_files, nef_files, gpx_files = categorize_files(from_camera_dir)

        action = prompt_next_action("STEP 2: 写真整理・コピー")
        if action == "A":
            print("\nユーザーにより処理が中断されました。")
            return

        # --------------------------------------------------
        # STEP 2: 写真整理・コピー
        # --------------------------------------------------
        if action == "N":
            copy_and_organize_photos(
                jpeg_files=jpeg_files,
                nef_files=nef_files,
                to_note_dir=to_note_dir,
                to_amazon_jpeg_dir=to_amazon_jpeg_dir,
                base_dir=base_dir,
            )
        elif action == "S":
            print("\n[STEP 2: 写真整理・コピー] をスキップしました。")

        action = prompt_next_action("STEP 3: フレーム付与処理")
        if action == "A":
            print("\nユーザーにより処理が中断されました。")
            return

        # --------------------------------------------------
        # STEP 3: フレーム付与処理
        # --------------------------------------------------
        if action == "N":
            run_frame_processing(
                script_path_or_unused=Path(),
                target_dir=to_note_dir,
                yaml_config=flame_yaml,
            )
        elif action == "S":
            print("\n[STEP 3: フレーム付与処理] をスキップしました。")

        print("\n==================================================")
        print(" すべてのワークフロー工程が終了しました！")
        print("==================================================")

    except Exception as e:
        print(f"\n[重大なエラーが発生しました]: {e}")
        logger.critical("予期せぬ重大なエラーが発生しました: %s", e, exc_info=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        print()
        input("キーを押すと終了します...")