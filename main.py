"""写真整理ワークフロー統括プログラム (main.py).

ワークフローのステップは build_steps() の一覧で定義している。新しいステップを
追加する場合はステップ関数を実装して一覧に1行追加するだけでよく、
main() 本体を直接編集する必要はない。

起動するとメニューを表示し、実行したいステップを1つずつ選んで実行できる
（全ステップを順に実行する項目もある）。--yes や --all を付けた場合、
または標準入力が対話端末でない場合（タスクスケジューラ等）はメニューを出さず、
従来どおり全ステップを順に実行する。

CLIオプション:
    --config PATH   config.json のパスを明示指定
    --yes, -y       すべての確認プロンプトを自動でY（実行）として進める（無人実行向け。メニューは出さない）
    --all           メニューを出さずに全ステップを順に実行する（各ステップの前に確認あり）
    --step STEP     メニューを出さずに指定したステップだけを実行する（複数回指定可）
    --skip STEP     指定したステップをスキップする（複数回指定可。全ステップ実行時のみ有効）
    --dry-run       実際のファイル操作は行わず、実行予定のステップのみ表示する
    --list-steps    実行可能なステップ一覧を表示して終了する
"""

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from modules.config_manager import ConfigManager
from modules.gps_tagger import apply_gps_tags
from modules.photo_copier import categorize_files, copy_and_organize_photos
from modules.frame_processor import run_frame_processing
from modules.exif_exporter import export_exif
from modules import __version__, console
from modules.console import paint
from modules.logging_config import ON_SCREEN, setup_logging, DEFAULT_LOG_FILE

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
        requires_confirmation: Trueなら実行前にY/S/Aで確認する（全ステップ実行時のみ）
        abort_on_failure: Trueならこのステップの失敗でワークフロー全体を中断する
        requires_source: Trueなら取り込み元フォルダ（from_camera）が存在しないと実行できない
    """

    key: str
    label: str
    action: Callable[[WorkflowContext], bool]
    requires_confirmation: bool = True
    abort_on_failure: bool = False
    requires_source: bool = True


def step_gps_tag(ctx: WorkflowContext) -> bool:
    """GPSログに基づくGPS位置情報の書き込み。"""
    return apply_gps_tags(ctx.from_camera_dir, ctx.gpx_files, config=ctx.config)


def step_manual_gps(ctx: WorkflowContext) -> bool:
    """手動GPS付与GUIツールの起動。"""
    run_manual_gps_tagger()
    # GUI でファイルが書き換えられた可能性があるため、最新の状態に分類し直す
    logger.info("手動GPS付与後のファイル再分類")
    ctx.jpeg_files, ctx.nef_files, ctx.gpx_files = categorize_files(ctx.from_camera_dir, config=ctx.config)
    # このステップ自体はGUI起動が成功すれば常にTrue（GUI内のエラーはこの関数からは判別できない）
    return True


def step_copy(ctx: WorkflowContext) -> bool:
    """写真ファイルの分類・コピー・整理。"""
    return copy_and_organize_photos(
        jpeg_files=ctx.jpeg_files,
        nef_files=ctx.nef_files,
        to_note_dir=ctx.to_note_dir,
        copy_targets=ctx.copy_target_dirs,
        base_dir=ctx.base_dir,
        config=ctx.config,
    )


def step_exif_export(ctx: WorkflowContext) -> bool:
    """EXIF抽出・書き出し処理。"""
    return run_exif_exporter(target_dir=ctx.to_note_dir, tsv_filename="exif_list.tsv", app_config=ctx.config)


def step_frame(ctx: WorkflowContext) -> bool:
    """フレーム付与処理。"""
    return run_frame_processing(
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
            label="GPS位置情報の書き込み",
            action=step_gps_tag,
            requires_confirmation=False,
            abort_on_failure=True,
        ),
        WorkflowStep(
            key="manual_gps",
            label="手動GPS情報付与",
            action=step_manual_gps,
        ),
        WorkflowStep(
            key="copy",
            label="写真整理・コピー",
            action=step_copy,
        ),
        WorkflowStep(
            key="exif_export",
            label="EXIF抽出・書き出し処理",
            action=step_exif_export,
            requires_source=False,  # to_note を処理対象にするため取り込み元は不要
        ),
        WorkflowStep(
            key="frame",
            label="フレーム付与処理",
            action=step_frame,
            requires_source=False,  # to_note を処理対象にするため取り込み元は不要
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
    parser.add_argument("--version", action="version", version=f"写真整理ワークフロー {__version__}",
                        help="バージョンを表示して終了する")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="config.json のパス（省略時は main.py のあるフォルダ → カレントディレクトリの順に探す）",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="すべての確認プロンプトを自動でY（実行）として進める（無人実行向け。メニューは出さない）",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="メニューを出さずに全ステップを順に実行する",
    )
    parser.add_argument(
        "--step", action="append", metavar="STEP", default=[],
        choices=[s.key for s in build_steps()],
        help="メニューを出さずに指定したステップだけを実行する（複数回指定可）",
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
    print("\n\n" + paint("-" * 50, "muted"))
    while True:
        try:
            choice = (
                input(
                    f"次のステップ [{paint(next_step_name, 'heading')}] に進みますか？\n"
                    f"  {paint('[Y]', 'key')} 次へ進む / {paint('[S]', 'key')} 次をスキップ / "
                    f"{paint('[A]', 'key')} 処理を中断 (Y/S/A): "
                )
                .strip()
                .upper()
            )
            if choice in ["Y", "S", "A"]:
                print(paint("-" * 50, "muted") + "\n\n")
                logger.info("ユーザー選択: %s", choice)
                return choice
            print(paint(" [!] 無効な入力です。'Y'、'S'、'A' のいずれかを入力してください。", "warning"))
        except KeyboardInterrupt:
            logger.warning("ユーザーが Ctrl+C で中断", extra=ON_SCREEN)
            print("\n" + paint("[!] 処理が中断されました", "warning"))
            return "A"
        except EOFError:
            # 標準入力が閉じている（対話端末でない）場合は入力を待てないため中断する。
            # ※以前は下の汎用 except で捕まえてループし続け、プログラムが終わらなくなっていた
            logger.warning("標準入力から確認の応答を読めないため中断（無人実行では --yes を指定）", extra=ON_SCREEN)
            print("\n" + paint("[!] 確認の入力を受け取れないため中断します（無人で実行する場合は --yes を付けてください）", "warning"))
            return "A"
        except Exception as e:
            # それ以外の予期しない入力エラーはログに残して聞き直す
            logger.error("入力エラー: %s", e, extra=ON_SCREEN)
            print(paint(f" [!] 入力エラーが発生しました: {e}", "warning"))


# ==================================================
# 外部ツール起動ヘルパー
# ==================================================

def run_manual_gps_tagger() -> None:
    """手動GPS付与GUIツールを起動。"""
    app_dir = Path(__file__).resolve().parent
    script_path = app_dir / "modules" / "manual_gps_tagger.py"

    if not script_path.exists():
        # スクリプトが見つからない場合は警告のみでワークフローは継続する（致命的エラーにしない）
        msg = f"手動GPS付与プログラムが見つかりません: {script_path}"
        print(paint(f"[警告] {msg}", "warning") + "\n")
        logger.warning(msg, extra=ON_SCREEN)
        return

    print("手動GPS付与ツールを起動しています...")
    logger.info("手動GPS付与ツールを起動: %s", script_path)

    try:
        # パッケージ（modules）の一部として起動する。カレントディレクトリは変えずに、
        # main.py のフォルダを import の検索先に加える（config.json の探し方を変えないため）
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(app_dir), env.get("PYTHONPATH")]))
        if not console.color_enabled():
            # 色を付けない設定（display.color: never など）を、別プロセスの手動GPSツールの画面表示にも伝える
            env["NO_COLOR"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "modules.manual_gps_tagger"],
            check=False,
            timeout=3600,
            env=env,
        )
        if result.returncode == 0:
            print(paint("手動GPS付与ツールが正常に終了しました。", "success"))
            logger.info("手動GPS付与ツール終了: 正常")
        else:
            # 0以外の終了コードは異常終了として警告扱い（ワークフロー自体は継続）
            logger.warning("手動GPS付与ツール終了: 終了コード %d", result.returncode, extra=ON_SCREEN)
            print(paint(f"[警告] 手動GPS付与ツールが異常終了しました (終了コード: {result.returncode})", "warning"))
    except subprocess.TimeoutExpired:
        msg = "手動GPS付与ツールの実行がタイムアウトしました (1時間)"
        print("\n" + paint(f"[エラー] {msg}", "error"))
        logger.error(msg, extra=ON_SCREEN)
    except Exception as e:
        msg = f"手動GPS付与ツールの実行中にエラーが発生しました: {e}"
        print("\n" + paint(f"[エラー] {msg}", "error"))
        logger.error(msg, exc_info=True, extra=ON_SCREEN)


def run_exif_exporter(target_dir: Path, tsv_filename: str = "exif_list.tsv", app_config=None) -> bool:
    """EXIF 情報を抽出し、TSV と個別 .exif ファイルに書き出す（進み具合とまとめを画面に表示する）。

    Returns:
        書き出しに成功した場合True。対象ディレクトリが無い・対象画像が無い・
        exiftool や書き込みに失敗した場合はFalse
    """
    logger.info("EXIF抽出を実行: %s -> %s", target_dir, tsv_filename)
    try:
        ok = bool(export_exif(target_dir, tsv_filename=tsv_filename, config=app_config))
    except Exception as e:
        # 想定外のエラーでもメニューやワークフロー全体は止めず、このステップの失敗として扱う
        msg = f"EXIF抽出の実行中にエラーが発生しました: {e}"
        console.error(msg, blank_line_before=True)
        logger.error(msg, exc_info=True, extra=ON_SCREEN)
        return False
    logger.info("EXIF抽出: %s", "正常終了" if ok else "失敗")
    return ok


# ==================================================
# ワークフロー実行
# ==================================================

def refresh_files(ctx: WorkflowContext) -> None:
    """取り込み元フォルダのファイル分類を取り直す。

    ステップを実行するとファイルが増減する（例: 写真整理・コピーで元ファイルがゴミ箱へ移る）ため、
    メニュー表示やステップ実行の前に毎回最新の状態にする。
    """
    if ctx.from_camera_dir.exists():
        ctx.jpeg_files, ctx.nef_files, ctx.gpx_files = categorize_files(ctx.from_camera_dir, config=ctx.config)
    else:
        ctx.jpeg_files, ctx.nef_files, ctx.gpx_files = [], [], []


def run_single_step(ctx: WorkflowContext, step: WorkflowStep, args: argparse.Namespace) -> bool:
    """1つのステップだけを実行する（メニュー・--step 用）。確認プロンプトは出さない。"""
    if not ctx.config.get_enabled_steps().get(step.key, True):
        print("\n" + paint(f"[{step.label}] は config.json の steps 設定で無効になっているため実行できません。", "warning"))
        logger.info("%s: config設定により無効", step.key)
        return False

    if step.requires_source and not ctx.from_camera_dir.exists():
        msg = f"取り込み元フォルダが存在しないため実行できません: {ctx.from_camera_dir}"
        print("\n" + paint(f"[エラー] {msg}", "error"))
        logger.error("%s: %s", step.key, msg, extra=ON_SCREEN)
        return False

    if args.dry_run:
        print(f"\n[dry-run] {step.label} を実行します（実際にはファイル操作を行いません）")
        logger.info("[dry-run] %s は実行対象（実処理はスキップ）", step.key)
        return True

    try:
        return execute_step(ctx, step, mode="単独実行")
    except KeyboardInterrupt:
        # ステップ実行中の Ctrl+C はそのステップだけを中断し、メニューに戻れるようにする
        print("\n" + paint(f"[!] {step.label} を Ctrl+C で中断しました", "warning"))
        logger.warning("%s: ユーザーが Ctrl+C で中断", step.key, extra=ON_SCREEN)
        return False


def execute_step(ctx: WorkflowContext, step: WorkflowStep, mode: str) -> bool:
    """1つのステップを実行し、成否を表示して返す（単独実行・全ステップ実行で共通）。

    ステップ内の予期しない例外はここで捕捉して「失敗」として扱い、
    メニューや残りのステップまで巻き込んで止まらないようにする。
    Ctrl+C（KeyboardInterrupt）は捕捉しない。単独実行ではそのステップだけ、
    全ステップ実行では残りすべてを中断するよう、呼び出し側で扱いを決める。

    Args:
        mode: ログに残す実行の種類（"単独実行" / "全ステップ実行"）
    """
    # ステップを実行するとファイルが増減するため、実行直前に分類を取り直す
    refresh_files(ctx)
    logger.info("%s 開始（%s）", step.key, mode)
    try:
        ok = bool(step.action(ctx))
    except Exception as e:
        print("\n" + paint(f"[エラー] {step.label} の実行中にエラーが発生しました: {e}", "error"))
        logger.error("%s: 予期しないエラー", step.key, exc_info=True, extra=ON_SCREEN)
        ok = False

    status = "完了" if ok else "失敗"
    print("\n" + paint(f">>> {step.label}: {status}", "success" if ok else "error"))
    logger.info("%s %s（%s）", step.key, status, mode)
    return ok


def print_menu(ctx: WorkflowContext, steps: List[WorkflowStep]) -> None:
    """メニューを表示する。"""
    enabled = ctx.config.get_enabled_steps()
    print("\n" + paint("=" * 50, "muted"))
    print(paint("      写真整理ワークフロー  メニュー", "heading"))
    print(paint("=" * 50, "muted"))
    if ctx.from_camera_dir.exists():
        print(f"  取り込み元: {ctx.from_camera_dir}")
        print(f"    JPEG {len(ctx.jpeg_files)} 件 / RAW {len(ctx.nef_files)} 件 / GPX {len(ctx.gpx_files)} 件")
    else:
        print(f"  取り込み元: {ctx.from_camera_dir}" + paint("（フォルダがありません）", "warning"))
    print(paint("-" * 50, "muted"))
    for i, step in enumerate(steps, 1):
        note = ""
        if not enabled.get(step.key, True):
            note = "  ※config で無効"
        elif step.requires_source and not ctx.from_camera_dir.exists():
            note = "  ※取り込み元がないため実行不可"
        if note:
            # 実行できないステップは番号ごと灰色にして、選べないことが分かるようにする
            print(paint(f"  [{i}] {step.label}{note}", "muted"))
        else:
            print(f"  {paint(f'[{i}]', 'key')} {step.label}")
    print(f"  {paint('[A]', 'key')} 全ステップを順に実行" + paint("（各ステップの前に確認あり）", "muted"))
    print(f"  {paint('[Q]', 'key')} 終了")
    print(paint("-" * 50, "muted"))


def run_menu(ctx: WorkflowContext, args: argparse.Namespace) -> None:
    """メニューを表示し、選ばれたステップを1つずつ実行する。Q で終了。"""
    steps = build_steps()
    while True:
        refresh_files(ctx)
        print_menu(ctx, steps)
        try:
            choice = input("番号を入力してください: ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            print()
            return

        if choice in ("Q", "0"):
            return
        if choice == "A":
            try:
                run_workflow(ctx, args)
            except KeyboardInterrupt:
                # 全ステップ実行中の Ctrl+C は実行を打ち切ってメニューに戻る
                print("\n" + paint("[!] 全ステップの実行を Ctrl+C で中断しました", "warning"))
                logger.warning("全ステップ実行: ユーザーが Ctrl+C で中断", extra=ON_SCREEN)
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(steps):
            run_single_step(ctx, steps[int(choice) - 1], args)
            continue
        print(paint(f" [!] 無効な入力です: '{choice}'。1〜{len(steps)}、A、Q のいずれかを入力してください。", "warning"))


def run_workflow(ctx: WorkflowContext, args: argparse.Namespace) -> None:
    """ステップ配列を順に実行する。"""
    enabled_steps = ctx.config.get_enabled_steps()
    skip_from_cli = set(args.skip)
    failed_steps: List[str] = []  # 失敗したステップ（最後にまとめて表示する）
    refresh_files(ctx)

    if not ctx.from_camera_dir.exists():
        # 取り込み元がなければ先頭のステップから前提が崩れるため、全体実行はしない
        msg = f"取り込み元フォルダが存在しません: {ctx.from_camera_dir}"
        print("\n" + paint(f"[エラー] {msg}", "error"))
        logger.error(msg, extra=ON_SCREEN)
        return

    for step in build_steps():
        if not enabled_steps.get(step.key, True):
            print("\n" + paint(f"[{step.label}] は config.json の設定によりスキップされました。", "muted"))
            logger.info("%s: config設定によりスキップ", step.key)
            continue

        if step.key in skip_from_cli:
            print("\n" + paint(f"[{step.label}] は --skip 指定によりスキップされました。", "muted"))
            logger.info("%s: --skip指定によりスキップ", step.key)
            continue

        if step.requires_confirmation and not args.yes:
            choice = prompt_next_action(step.label)
            if choice == "A":
                print("\n" + paint("ユーザーにより処理が中止されました。", "warning"))
                logger.info("ユーザーが %s で中止", step.key)
                return
            if choice == "S":
                print("\n" + paint(f"[{step.label}] をスキップしました。", "muted"))
                logger.info("%s をスキップ", step.key)
                continue

        if args.dry_run:
            print(f"\n[dry-run] {step.label} を実行します（実際にはファイル操作を行いません）")
            logger.info("[dry-run] %s は実行対象（実処理はスキップ）", step.key)
            continue

        ok = execute_step(ctx, step, mode="全ステップ実行")
        if not ok:
            failed_steps.append(step.label)
        if not ok and step.abort_on_failure:
            # abort_on_failure=True のステップが失敗した場合はワークフロー全体を中断する
            # （例: GPS位置情報の書き込みが失敗した場合、後続ステップの前提が崩れるため）
            print("\n" + paint(f"{step.label} で重大なエラーが発生したため中止します。", "error"))
            logger.error("%s エラーで中止", step.key, extra=ON_SCREEN)
            return

    print("\n" + paint("=" * 50, "muted"))
    if failed_steps:
        # 一部のステップが失敗しても最後まで進んだ場合は、「正常に完了」とは表示しない
        print(paint(" すべてのワークフロー工程が終了しました（失敗したステップがあります）", "warning"))
        for label in failed_steps:
            print(paint(f"   - {label}", "error"))
        print(paint("=" * 50, "muted"))
        logger.warning("ワークフロー終了: 失敗したステップ %s", ", ".join(failed_steps), extra=ON_SCREEN)
    else:
        print(paint(" すべてのワークフロー工程が終了しました！", "success"))
        print(paint("=" * 50, "muted"))
        logger.info("すべてのワークフロー工程が正常に完了しました")


def use_menu(args: argparse.Namespace) -> bool:
    """メニューを表示するかどうか。

    無人実行（--yes）・全ステップ実行の明示（--all / --skip）・対話端末でない場合
    （タスクスケジューラ等）は、従来どおりメニューを出さずに全ステップを順に実行する。
    """
    if args.yes or args.all or args.skip or args.step or args.list_steps:
        return False
    return sys.stdin.isatty()


def print_banner() -> None:
    """起動時の見出し（プログラム名とバージョン）。"""
    print(paint("=" * 50, "muted"))
    print(paint(f"      写真整理ワークフロープログラム  v{__version__}", "heading"))
    print(paint("=" * 50, "muted"))


def main(args: argparse.Namespace) -> None:
    """メイン実行エントリーポイント。"""
    if args.list_steps:
        print("実行可能なステップ一覧:")
        for step in build_steps():
            confirm = "確認あり" if step.requires_confirmation else "自動実行"
            print(f"  - {step.key:<12} {step.label} ({confirm})")
        return

    try:
        # --config 指定があればそのファイルだけを使う（存在しなければエラー）。
        # 指定がなければ main.py のあるフォルダ → カレントディレクトリの順に config.json を探す
        try:
            config = ConfigManager(args.config)
        except (FileNotFoundError, ValueError) as e:
            print_banner()  # 設定を読めないので、色は既定（auto）のまま
            print("\n" + paint(f"[エラー] {e}", "error"))
            logger.error("設定読み込みエラー: %s", e, extra=ON_SCREEN)
            return

        # 処理中の表示の形（1行を上書き / 1枚ごとに1行）と文字の色（auto / always / never）。
        # 見出しにも display.color を反映させるため、設定を読んでから見出しを表示する
        console.configure(
            per_file=config.get("display.per_file", False),
            color=config.get("display.color", "auto"),
        )
        print_banner()

        try:
            from_camera_dir = config.get_directory("from_camera")
            to_note_dir = config.get_directory("to_note")
            base_dir = config.get_directory("base")
            flame_config = config.get("external_tools.flame_config")
            if not flame_config:
                raise KeyError("external_tools.flame_config")
            # 相対パスはカレントディレクトリではなく config.json / main.py のフォルダ基準で解決する
            flame_yaml = config.resolve_path(flame_config)
            copy_target_dirs = [Path(t["path"]) for t in config.get_copy_targets()]
        except KeyError as e:
            print("\n" + paint(f"[エラー] 設定キーが不足しています: {e}", "error"))
            logger.error("設定キーエラー: %s", e, extra=ON_SCREEN)
            return

        # 以降の各ステップ関数が共有する実行コンテキストを構築。
        # ファイル分類（JPEG/RAW/GPX）は各ステップの実行直前に refresh_files() で取り直す。
        # 取り込み元フォルダがなくても、EXIF抽出・書き出しとフレーム付与は to_note を対象に単独実行できるため、
        # ここでは打ち切らない（取り込み元が必要なステップは実行時に個別にチェックする）
        ctx = WorkflowContext(
            config=config,
            from_camera_dir=from_camera_dir,
            to_note_dir=to_note_dir,
            copy_target_dirs=copy_target_dirs,
            base_dir=base_dir,
            flame_yaml=str(flame_yaml),
        )

        if args.step:
            steps_by_key = {s.key: s for s in build_steps()}
            for key in args.step:
                run_single_step(ctx, steps_by_key[key], args)
        elif use_menu(args):
            run_menu(ctx, args)
        else:
            run_workflow(ctx, args)

    except KeyboardInterrupt:
        print("\n\n" + paint("[!] ユーザーが Ctrl+C で中断しました", "warning"))
        logger.warning("ユーザーが Ctrl+C で中止", extra=ON_SCREEN)
    except Exception as e:
        print("\n" + paint(f"[エラー] 重大なエラーが発生しました: {e}", "error"))
        logger.critical("予期せぬ重大なエラーが発生しました: %s", e, exc_info=True, extra=ON_SCREEN)


if __name__ == "__main__":
    # ファイルには詳細(INFO)を、画面にはWARNING以上だけを簡潔に表示する。
    # import しただけ（テストや並列処理用のプロセス）ではログの設定を変えないよう、ここで行う。
    # 別プロセスで起動する manual_gps_tagger.py も同じ設定を使い、画面表示をそろえる。
    setup_logging(DEFAULT_LOG_FILE)
    logger.info("写真整理ワークフロー v%s を起動", __version__)
    cli_args = parse_args()
    try:
        logger.info("=" * 50)
        logger.info("写真整理ワークフロープログラムを開始")
        logger.info("=" * 50)
        main(cli_args)
    except KeyboardInterrupt:
        print("\n\n" + paint("[!] プログラムが Ctrl+C で中断されました", "warning"))
        logger.warning("プログラム Ctrl+C で中止されました", extra=ON_SCREEN)
        sys.exit(130)
    except Exception as e:
        print("\n" + paint(f"[エラー] 予期しないエラーが発生しました: {e}", "error"))
        logger.critical("予期しないエラーが発生しました", exc_info=True, extra=ON_SCREEN)
        sys.exit(1)
    finally:
        print()
        # 無人実行（--yes）や標準入力が対話端末でない場合は
        # キー入力待ちで停止させない（cron/タスクスケジューラ運用を想定）
        # メニューは Q で終了した時点で操作が終わっているため、ここでは待たない
        if not cli_args.yes and sys.stdin.isatty() and not use_menu(cli_args):
            try:
                input("キーを押すと終了します...")
            except (KeyboardInterrupt, EOFError):
                pass
        logger.info("=" * 50)
        logger.info("プログラムを終了します")
        logger.info("=" * 50)
