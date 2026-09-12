"""写真整理ワークフロー統括プログラム (main.py).

ワークフローは STEP_DEFINITIONS の配列で定義されている。新しいステップを
追加したい場合はステップ関数を実装して配列に1行追加するだけでよく、
main() 本体を直接編集する必要はない。

CLIオプション:
    --config PATH   config.json のパスを明示指定
    --yes, -y       すべての確認プロンプトを自動でY（実行）として進める（無人実行向け）
    --skip STEP     指定したステップをスキップする（複数回指定可）
    --dry-run       実際のファイル操作は行わず、実行予定のステップのみ表示する
    --list-steps    実行可能なステップ一覧を表示して終了する
"""

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# 自作モジュールのインポート
from modules.config_manager import ConfigManager
from modules.gps_tagger import apply_gps_tags
from modules.photo_copier import categorize_files, copy_and_organize_photos
from modules.frame_processor import run_frame_processing
from modules.logging_config import setup_logging, DEFAULT_LOG_FILE
from modules import constants

# --- ログ設定 ---
# ファイルには詳細(INFO)を、画面にはWARNING以上のみを簡潔に表示する。
# manual_gps_tagger.py / exif_exporter.py もこの共通設定を使うため、
# 別プロセスとして起動された場合でも画面表示のポリシーが揃う。
LOG_FILE_PATH = DEFAULT_LOG_FILE
setup_logging(LOG_FILE_PATH)

logger = logging.getLogger(__name__)


# ==================================================
# ワークフローのコンテキスト・ステップ定義
# ==================================================

@dataclass
class WorkflowContext:
    """各ステップ関数が共有する実行時状態。"""

    config: ConfigManager
    from_camera_dir: Path
    to_note_dir: Path
    copy_target_dirs: List[Path]
    base_dir: Path
    flame_yaml: str
    jpeg_files: List[Path] = field(default_factory=list)
    nef_files: List[Path] = field(default_factory=list)
    gpx_files: List[Path] = field(default_factory=list)


@dataclass
class WorkflowStep:
    """1つのワークフローステップの定義。

    Attributes:
        key: config.json の 'steps' や --skip で参照するキー
        label: 画面表示用のステップ名
        action: ctx を受け取り成功可否(bool)を返す実行関数
        requires_confirmation: Trueなら実行前にY/S/Aで確認する
        abort_on_failure: Trueならこのステップの失敗でワークフロー全体を中断する
    """

    key: str
    label: str
    action: Callable[[WorkflowContext], bool]
    requires_confirmation: bool = True
    abort_on_failure: bool = False


def step_gps_tag(ctx: WorkflowContext) -> bool:
    """STEP 1: GPXログに基づくGPS位置情報の書き込み。"""
    return apply_gps_tags(ctx.from_camera_dir, ctx.gpx_files, config=ctx.config)


def step_manual_gps(ctx: WorkflowContext) -> bool:
    """STEP 1.5: 手動GPS付与GUIツールの起動。"""
    run_manual_gps_tagger()
    # 手動付与でファイルが変更された可能性があるため再分類
    logger.info("手動GPS付与後のファイル再分類")
    ctx.jpeg_files, ctx.nef_files, ctx.gpx_files = categorize_files(ctx.from_camera_dir, config=ctx.config)
    return True


def step_copy(ctx: WorkflowContext) -> bool:
    """STEP 2: 写真ファイルの分類・コピー・整理。"""
    return copy_and_organize_photos(
        jpeg_files=ctx.jpeg_files,
        nef_files=ctx.nef_files,
        to_note_dir=ctx.to_note_dir,
        copy_targets=ctx.copy_target_dirs,
        base_dir=ctx.base_dir,
        config=ctx.config,
    )


def step_exif_export(ctx: WorkflowContext) -> bool:
    """STEP 2.5: EXIF抽出・書き出し処理。"""
    run_exif_exporter(target_dir=ctx.to_note_dir, tsv_filename="exif_list.tsv", app_config=ctx.config)
    return True


def step_frame(ctx: WorkflowContext) -> bool:
    """STEP 3: フレーム付与処理。"""
    return run_frame_processing(
        script_path_or_unused=Path(),
        target_dir=ctx.to_note_dir,
        yaml_config=ctx.flame_yaml,
        app_config=ctx.config,
    )


def build_steps() -> List[WorkflowStep]:
    """ワークフローのステップ一覧を構築する。

    新しいステップを追加する場合はここに1行加えるだけでよい。
    """
    return [
        WorkflowStep(
            key="gps_tag",
            label="STEP 1: GPS位置情報の書き込み",
            action=step_gps_tag,
            requires_confirmation=False,  # 従来通り確認なしで自動実行
            abort_on_failure=True,        # 失敗時はワークフロー全体を中断
        ),
        WorkflowStep(
            key="manual_gps",
            label="STEP 1.5: 手動GPS情報付与",
            action=step_manual_gps,
        ),
        WorkflowStep(
            key="copy",
            label="STEP 2: 写真整理・コピー",
            action=step_copy,
        ),
        WorkflowStep(
            key="exif_export",
            label="STEP 2.5: EXIF抽出・書き出し処理",
            action=step_exif_export,
        ),
        WorkflowStep(
            key="frame",
            label="STEP 3: フレーム付与処理",
            action=step_frame,
        ),
    ]


# ==================================================
# CLI引数
# ==================================================

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(
        description="写真整理ワークフロープログラム",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="config.json のパス（省略時はカレントディレクトリ等を自動探索）",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="すべての確認プロンプトを自動でY（実行）として進める（無人実行向け）",
    )
    parser.add_argument(
        "--skip", action="append", metavar="STEP", default=[],
        choices=[s.key for s in build_steps()],
        help="指定したステップをスキップする（複数回指定可）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="実際のファイル操作は行わず、実行予定のステップのみ表示する",
    )
    parser.add_argument(
        "--list-steps", action="store_true",
        help="実行可能なステップ一覧を表示して終了する",
    )
    return parser.parse_args(argv)


# ==================================================
# 対話プロンプト
# ==================================================

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


# ==================================================
# 外部ツール起動ヘルパー
# ==================================================

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


def run_exif_exporter(target_dir: Path, tsv_filename: str = "exif_list.tsv", app_config=None) -> None:
    """外部スクリプト exif_exporter.py を呼び出して EXIF 情報を抽出・出力。

    別プロセスとして起動するため、config.json の exiftool パスは文字列として
    コマンドライン引数で渡す（ConfigManagerオブジェクト自体はプロセスをまたげない。
    exif_exporter.py 側は自分自身で config.json を再読み込みし、出力タグ設定等を取得する）。
    """
    script_path = Path(__file__).parent / "modules" / "exif_exporter.py"

    if not script_path.exists():
        msg = f"EXIF抽出プログラムが見つかりません: {script_path}"
        print(f"[警告] {msg}\n")
        logger.warning(msg)
        return

    exiftool_path = ""
    if app_config is not None:
        try:
            exiftool_path = str(app_config.get_tool_path("exiftool"))
        except KeyError:
            logger.warning("config.jsonにexiftoolパスが見つからないため、自動検索に委ねます")

    print("EXIF抽出・書き出しツールを起動しています...")
    logger.info("EXIF抽出ツールを実行: %s %s %s", script_path, target_dir, tsv_filename)

    cmd = [sys.executable, str(script_path), str(target_dir), tsv_filename]
    if exiftool_path:
        cmd.append(exiftool_path)

    try:
        result = subprocess.run(
            cmd,
            check=False,
            timeout=1800  # 30分のタイムアウト
        )
        if result.returncode == 0:
            print("EXIF抽出ツールが正常に完了しました。")
            logger.info("EXIF抽出ツール終了: 正常")
        else:
            logger.warning("EXIF抽出ツール終了: 終了コード %d", result.returncode)
            print(f"[警告] EXIF抽出ツールが異常終了しました (終了コード: {result.returncode})")
    except subprocess.TimeoutExpired:
        msg = "EXIF抽出ツールの実行がタイムアウトしました (30分)"
        print(f"\n[エラー] {msg}")
        logger.error(msg)
    except Exception as e:
        msg = f"EXIF抽出ツールの実行中にエラーが発生しました: {e}"
        print(f"\n[エラー] {msg}")
        logger.error(msg, exc_info=True)


# ==================================================
# ワークフロー実行
# ==================================================

def run_workflow(ctx: WorkflowContext, args: argparse.Namespace) -> None:
    """ステップ配列を順に実行する。"""
    enabled_steps = ctx.config.get_enabled_steps()
    skip_from_cli = set(args.skip)

    for step in build_steps():
        if not enabled_steps.get(step.key, True):
            print(f"\n[{step.label}] は config.json の設定によりスキップされました。")
            logger.info("%s: config設定によりスキップ", step.key)
            continue

        if step.key in skip_from_cli:
            print(f"\n[{step.label}] は --skip 指定によりスキップされました。")
            logger.info("%s: --skip指定によりスキップ", step.key)
            continue

        if step.requires_confirmation and not args.yes:
            choice = prompt_next_action(step.label)
            if choice == "A":
                print("\nユーザーにより処理が中断されました。")
                logger.info("ユーザーが %s で中止", step.key)
                return
            if choice == "S":
                print(f"\n[{step.label}] をスキップしました。")
                logger.info("%s をスキップ", step.key)
                continue

        if args.dry_run:
            print(f"\n[dry-run] {step.label} を実行します（実際にはファイル操作を行いません）")
            logger.info("[dry-run] %s は実行対象（実処理はスキップ）", step.key)
            continue

        logger.info("%s 開始", step.key)
        ok = step.action(ctx)
        if not ok and step.abort_on_failure:
            print(f"\n{step.label} で重大なエラーが発生したため中断します。")
            logger.error("%s エラーで中止", step.key)
            return

    print("\n==================================================")
    print(" すべてのワークフロー工程が終了しました！")
    print("==================================================")
    logger.info("すべてのワークフロー工程が正常に完了しました")


def main(args: argparse.Namespace) -> None:
    """メイン実行エントリーポイント。"""
    if args.list_steps:
        print("実行可能なステップ一覧:")
        for step in build_steps():
            confirm = "確認あり" if step.requires_confirmation else "自動実行"
            print(f"  - {step.key:<12} {step.label} ({confirm})")
        return

    print("==================================================")
    print("      写真整理ワークフロープログラム")
    print("==================================================")

    try:
        # 設定の読み込み
        config_file = args.config if args.config is not None else Path(__file__).parent / "config.json"
        try:
            config = ConfigManager(config_file)
        except (FileNotFoundError, ValueError) as e:
            print(f"\n[エラー] {e}")
            logger.error("設定読み込みエラー: %s", e)
            return

        # ディレクトリ・設定値を取得
        try:
            from_camera_dir = config.get_directory("from_camera")
            to_note_dir = config.get_directory("to_note")
            base_dir = config.get_directory("base")
            flame_yaml = config.get("external_tools.flame_config")
            copy_target_dirs = [Path(t["path"]) for t in config.get_copy_targets()]
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
        jpeg_files, nef_files, gpx_files = categorize_files(from_camera_dir, config=config)

        ctx = WorkflowContext(
            config=config,
            from_camera_dir=from_camera_dir,
            to_note_dir=to_note_dir,
            copy_target_dirs=copy_target_dirs,
            base_dir=base_dir,
            flame_yaml=str(flame_yaml),
            jpeg_files=jpeg_files,
            nef_files=nef_files,
            gpx_files=gpx_files,
        )

        run_workflow(ctx, args)

    except KeyboardInterrupt:
        print("\n\n[!] ユーザーが Ctrl+C で中断しました")
        logger.warning("ユーザーが Ctrl+C で中止")
    except Exception as e:
        print(f"\n[重大なエラーが発生しました]: {e}")
        logger.critical("予期せぬ重大なエラーが発生しました: %s", e, exc_info=True)


if __name__ == "__main__":
    cli_args = parse_args()
    try:
        logger.info("=" * 50)
        logger.info("写真整理ワークフロープログラムを開始します")
        logger.info("=" * 50)
        main(cli_args)
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
        # 無人実行（--yes）や標準入力が対話端末でない場合は
        # キー入力待ちで停止させない（cron/タスクスケジューラ運用を想定）
        if not cli_args.yes and sys.stdin.isatty():
            try:
                input("キーを押すと終了します...")
            except (KeyboardInterrupt, EOFError):
                pass
        logger.info("=" * 50)
        logger.info("プログラムを終了します")
        logger.info("=" * 50)
