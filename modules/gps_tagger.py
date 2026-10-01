"""STEP 1: GPXログに基づく位置情報の付与モジュール。"""

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 同一パッケージ内の exiftool_client モジュールから ExifToolClient・ExifToolError をインポート
from .exiftool_client import ExifToolClient, ExifToolError
from .constants import JPEG_EXTENSIONS, PROGRESS_FORMAT, RAW_EXTENSIONS

# このモジュール専用のロガーを取得
logger = logging.getLogger(__name__)

# 正規表現パターンのプリコンパイル
# exiftoolのgeotag実行結果（標準出力）から件数を抽出するための正規表現。
# exiftool本体の出力書式: "%5d image files updated" / "%5d image files unchanged" /
#                         "%5d files weren't updated due to errors"
PATTERN_UPDATED = re.compile(r"(\d+)\s+image files updated")      # 例: "3 image files updated"
PATTERN_UNCHANGED = re.compile(r"(\d+)\s+image files unchanged")  # 例: "1 image files unchanged"
PATTERN_ERRORS = re.compile(r"(\d+)\s+files weren't updated due to errors")
# 既にGPSを持つためスキップしたファイル数（-if "not $GPSLatitude" の条件に合わなかったもの）
PATTERN_FAILED_CONDITION = re.compile(r"(\d+)\s+files failed condition")
# 位置情報を1つも書き込めなかったファイル（GPXの範囲外・撮影日時なし等）を示す警告
PATTERN_NOT_SET = re.compile(r"^No writable tags set from (.+)$")

# 付与できなかった理由の分類（警告メッセージに含まれる語で判定する）
REASON_OUT_OF_RANGE = "GPXの記録範囲外"
REASON_NO_DATE = "撮影日時なし"
REASON_OTHER = "その他"

# サマリーに表示するファイル名の最大件数（多すぎると画面が流れてしまうため）
MAX_LIST_FILES = 20


@dataclass
class GeotagResult:
    """exiftool -geotag の実行結果を集計したもの。"""

    updated: int = 0                 # 位置情報を書き込めたファイル数
    unchanged: int = 0               # 書き込みが行われなかったファイル数（exiftoolの集計値）
    error_files: int = 0             # exiftoolが「エラーで更新できなかった」と報告したファイル数
    skipped_existing: int = 0        # 既にGPS情報を持っていたため書き換えなかったファイル数
    # 位置情報を付与できなかったファイル → 理由（REASON_*）
    not_tagged: Dict[str, str] = field(default_factory=dict)
    # 上記以外の警告・エラーを種類ごとに集計したもの（ファイル名部分は除去済み）
    messages: Counter = field(default_factory=Counter)
    has_error_line: bool = False     # "Error:" で始まる行があったか


def _split_message(line: str) -> Optional[tuple]:
    """'Warning: 本文 - ファイルパス' 形式の行を (種別, 本文, ファイルパス or None) に分解する。"""
    for kind in ("Warning", "Error"):
        prefix = f"{kind}: "
        if line.startswith(prefix):
            body = line[len(prefix):].strip()
            body = re.sub(r"^\[minor\]\s*", "", body)
            path = None
            # exiftool は末尾に " - <ファイルパス>" を付ける。本文中のハイフンで
            # 切らないよう、最後の " - " で分割する
            if " - " in body:
                head, tail = body.rsplit(" - ", 1)
                body, path = head.strip(), tail.strip()
            return kind, body, path
    return None


def _classify_reason(message: str) -> str:
    """警告本文から、位置情報を付与できなかった理由を分類する。"""
    if "too far" in message:
        # 例: "Time is too far beyond track" / "Time is too far before track"
        return REASON_OUT_OF_RANGE
    if "DateTimeOriginal" in message or "Geotime" in message:
        # 例: "Tag 'DateTimeOriginal' not defined"
        return REASON_NO_DATE
    return REASON_OTHER


def parse_geotag_output(stdout: str, stderr: str) -> GeotagResult:
    """exiftool -geotag の標準出力・標準エラーを解析して集計する。"""
    result = GeotagResult()
    stdout_lines = stdout.splitlines() if stdout else []
    stderr_lines = stderr.splitlines() if stderr else []

    # ファイルごとの直近の警告本文（"No writable tags set" の理由付けに使う）
    last_message_by_file: Dict[str, str] = {}

    for raw in stdout_lines + stderr_lines:
        line = raw.strip()
        if not line:
            continue

        # --- 件数行 ---
        m = PATTERN_UPDATED.search(line)
        if m:
            result.updated = int(m.group(1))
            continue
        m = PATTERN_UNCHANGED.search(line)
        if m:
            result.unchanged = int(m.group(1))
            continue
        m = PATTERN_ERRORS.search(line)
        if m:
            result.error_files = int(m.group(1))
            continue
        m = PATTERN_FAILED_CONDITION.search(line)
        if m:
            result.skipped_existing = int(m.group(1))
            continue

        # --- 警告・エラー行 ---
        parsed = _split_message(line)
        if parsed is None:
            continue
        kind, body, path = parsed
        if kind == "Error":
            result.has_error_line = True

        not_set = PATTERN_NOT_SET.match(body)
        if not_set:
            # 位置情報を1つも書き込めなかったファイル。同じファイルの直前の警告から理由を決める
            file_path = os.path.normpath(not_set.group(1).strip())
            reason = _classify_reason(last_message_by_file.get(file_path, ""))
            result.not_tagged[file_path] = reason
            continue

        if path:
            last_message_by_file[os.path.normpath(path)] = body
        result.messages[f"{kind}: {body}"] += 1

    return result


class _ProgressPrinter:
    """exiftool の進捗を1行で上書き表示する。"""

    # 前回より短いファイル名を表示したときに残りの文字が見えないよう、この幅まで空白で埋める。
    # 日本語は2桁幅で表示されるため、80桁のコンソールでも折り返さない長さにしている
    LINE_WIDTH = 68
    MAX_NAME = 30  # 長いファイル名は末尾側を残して省略する

    def __init__(self) -> None:
        self.last_total = 0
        self.last_current = 0

    def __call__(self, current: int, total: int, file_path: str) -> None:
        self.last_current, self.last_total = current, total
        pct = int(current * 100 / total) if total else 100
        name = os.path.basename(file_path.replace("\\", "/"))
        if len(name) > self.MAX_NAME:
            name = "…" + name[-(self.MAX_NAME - 1):]
        line = f"  - 位置情報を付与中: {PROGRESS_FORMAT.format(pct, current, total)} {name}"
        print("\r" + line.ljust(self.LINE_WIDTH), end="", flush=True)

    def finish(self) -> None:
        if self.last_total:
            # 既にGPSがあってスキップしたファイルは進捗行が出ないため、最後に100%表示で締める
            done = PROGRESS_FORMAT.format(100, self.last_total, self.last_total)
            print("\r" + f"  - 位置情報を付与中: {done} 完了".ljust(self.LINE_WIDTH), flush=True)


def _print_summary(gpx_count: int, geosync: Optional[str], result: GeotagResult) -> None:
    """集計結果をコンソールに表示する。"""
    print("\n")
    print(" 【処理のサマリー】")
    print(f"    ・検出GPXファイル : {gpx_count} 件")
    print(f"    ・時計ずれ補正    : {geosync if geosync else 'なし'}")
    print(f"    ・位置情報付与成功: {result.updated} 件")
    if result.skipped_existing:
        print(f"    ・既にGPSあり      : {result.skipped_existing} 件（書き換えずにスキップ）")

    if result.not_tagged:
        print(f"    ・位置情報を付与できなかった写真: {len(result.not_tagged)} 件"
              "（STEP 1.5 で手動付与が必要）")
        reasons = Counter(result.not_tagged.values())
        for reason, count in reasons.most_common():
            print(f"       * {reason}: {count} 件")
        names = sorted(os.path.basename(p) for p in result.not_tagged)
        for name in names[:MAX_LIST_FILES]:
            print(f"         - {name}")
        if len(names) > MAX_LIST_FILES:
            print(f"         ...ほか {len(names) - MAX_LIST_FILES} 件（全件はログに記録）")
    elif result.unchanged:
        # 理由付きの一覧が取れなかった場合でも件数だけは表示する
        print(f"    ・位置情報を付与できなかった写真: {result.unchanged} 件"
              "（STEP 1.5 で手動付与が必要）")

    if result.error_files:
        print(f"    ・書き込みエラー  : {result.error_files} 件（ファイルを更新できませんでした）")

    # 付与できなかった理由として上に表示済みの警告は除き、それ以外を種類ごとに表示する
    other = {
        msg: n for msg, n in result.messages.items()
        if _classify_reason(msg) == REASON_OTHER or msg.startswith("Error:")
    }
    if other:
        print("    ・その他の警告・エラー:")
        for msg, count in Counter(other).most_common():
            print(f"       * {msg}: {count} 件")

    print("\n")
    print("==================================================")
    print(" 【終了】STEP 1: GPS位置情報の書き込み 終了")
    print("==================================================")


def apply_gps_tags(from_camera_dir: Path, gpx_files: List[Path], config: Optional[Any] = None) -> bool:
    """GPXファイルをもとに写真へGPS位置情報を書き込み。

    Args:
        from_camera_dir: 対象写真ディレクトリ
        gpx_files: GPX ファイルリスト
        config: ConfigManager。exiftoolのパス解決・時計ずれ補正値の取得に使用（Noneの場合は補正なし）

    Returns:
        処理が成功した場合True。exiftool自体の実行に失敗した場合、または
        エラーが発生して1枚も書き込めなかった場合はFalse（ワークフローを中断する）。
        一部のファイルだけ付与できなかった場合は、サマリーに表示したうえでTrue。
    """
    # 処理開始をコンソール表示・ログの両方に記録
    print("\n==================================================")
    print(" 【開始】STEP 1: GPS位置情報の書き込み")
    print("==================================================")
    logger.info("--- STEP 1: GPS位置情報の書き込み ---")

    if not gpx_files:
        # GPXファイルが1件も無い場合はGPSタグ付けを行う対象が無いため、
        # エラーにはせず正常終了（True）としてスキップする
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
        # 対象ディレクトリ自体が存在しない場合はエラーとして処理を打ち切る
        error_msg = f"対象ディレクトリが見つかりません: {from_camera_dir}"
        logger.error(error_msg)
        print(f" -> [エラー] {error_msg}")
        return False

    logger.info("%d 件の GPX ファイルを検出しました", len(gpx_files))

    # ExifToolClient のインスタンスを生成
    et = ExifToolClient(config=config)
    # カメラ時計のずれ補正値（"GPS時刻 − カメラ時刻"）。未設定なら None＝補正なし。
    # ※タイムゾーンは exiftool が写真の OffsetTimeOriginal（なければPCの設定）から
    #   自動で扱うため、ここで +09:00 のような値を渡してはいけない
    #   （以前の "+09:00" は exiftool に「+9分」と解釈され、位置が9分ずれていた）
    geosync = config.get_camera_clock_offset() if config is not None else None
    # 既存GPSの保護・補間の上限秒数などの動作設定（config が無ければ既定値）
    options = config.get_gps_options() if config is not None else {
        "skip_existing": True, "max_interpolation_secs": None, "max_extrapolation_secs": None,
    }
    # 対象はワークフローが扱う JPEG / RAW のみ（動画など分類対象外のファイルは書き換えない）
    if config is not None:
        ext_map = config.get_file_extensions()
        target_exts = sorted(ext_map["jpeg"] | ext_map["raw"])
    else:
        target_exts = sorted(JPEG_EXTENSIONS | RAW_EXTENSIONS)

    try:
        logger.info(
            "ExifTool コマンド実行中... (path=%s, geosync=%s, 対象=%s, 既存GPSをスキップ=%s)",
            et.path, geosync or "なし", ",".join(target_exts), options["skip_existing"],
        )
        # exiftoolの-geotagコマンドを実行し、GPXの軌跡データから各写真の撮影時刻に対応する
        # 緯度経度を割り出してEXIFに書き込む。1ファイル処理するごとに進捗を1行で上書き表示する
        progress = _ProgressPrinter()
        try:
            output = et.geotag_from_gpx(
                target_dir=from_camera_dir,
                gpx_files=gpx_files,
                geosync=geosync,
                extensions=target_exts,
                skip_existing=options["skip_existing"],
                max_interpolation_secs=options["max_interpolation_secs"],
                max_extrapolation_secs=options["max_extrapolation_secs"],
                on_progress=progress,
            )
        finally:
            progress.finish()

        # exiftoolの生の出力はそのままログファイルに残す（後から詳細を確認できるように）。
        # 画面の進捗表示やサマリーと重複するため、コンソールには出さない（INFO）
        for line in output.splitlines():
            if not line.startswith("======== "):
                logger.info("exiftool: %s", line)

        result = parse_geotag_output(output, "")
        _print_summary(len(gpx_files), geosync, result)

        if result.not_tagged:
            logger.info(
                "位置情報を付与できなかった写真 (%d 件): %s",
                len(result.not_tagged),
                ", ".join(f"{os.path.basename(p)}[{r}]" for p, r in sorted(result.not_tagged.items())),
            )
        logger.info(
            "GPS タグ書き込み完了: 更新=%d, 既存GPSでスキップ=%d, 付与できず=%d, 書き込みエラー=%d",
            result.updated, result.skipped_existing,
            len(result.not_tagged) or result.unchanged, result.error_files,
        )

        if (result.error_files or result.has_error_line) and result.updated == 0:
            # エラーが出て1枚も書き込めていない場合は、後続ステップの前提が崩れるため失敗扱い
            print(" -> [エラー] 位置情報を1枚も書き込めませんでした。ログを確認してください。")
            logger.error("GPS タグ書き込み失敗: エラーにより更新 0 件")
            return False
        if result.error_files:
            # 一部だけ失敗した場合は処理を続けるが、目立つように警告しておく
            print(f" -> [警告] {result.error_files} 件のファイルを更新できませんでした。ログを確認してください。")
        return True

    except ExifToolError as e:
        # exiftool実行自体が失敗した場合（コマンドが見つからない、タイムアウト等）
        logger.error("GPSタグ書き込みエラー: %s", e)
        print(f" -> [エラー] {e}")
        return False
    except Exception as e:
        # その他の予期しない例外はスタックトレース付きでログに記録し、処理は失敗として終了する
        error_msg = f"GPS情報の書き込み中に予期しないエラーが発生しました: {e}"
        logger.error(error_msg, exc_info=True)
        print(f" -> [エラー] {error_msg}")
        return False
