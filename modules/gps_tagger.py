"""GPS位置情報の書き込み: GPX ログをもとに、写真へ位置情報を書き込む。

処理の関数 geotag_photos() は画面に表示せず、結果（GeotagResult）を返す。
画面に表示するのはステップの入口 apply_gps_tags() だけ。
"""

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import console
from .constants import JPEG_EXTENSIONS, RAW_EXTENSIONS
from .exiftool_client import PATTERN_PROGRESS, ExifToolClient, ExifToolError
from .logging_config import ON_SCREEN

logger = logging.getLogger(__name__)

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


def target_extensions(config: Optional[Any]) -> List[str]:
    """位置情報を書き込む対象の拡張子。ワークフローが扱う JPEG / RAW だけ（動画などは書き換えない）。"""
    if config is not None:
        ext_map = config.get_file_extensions()
        return sorted(ext_map["jpeg"] | ext_map["raw"])
    return sorted(JPEG_EXTENSIONS | RAW_EXTENSIONS)


def target_files(from_camera_dir: Path, config: Optional[Any] = None) -> List[Path]:
    """位置情報を書き込む対象の写真（フォルダ直下の JPEG / RAW）。ファイル名順。"""
    exts = {e.lower() for e in target_extensions(config)}
    return sorted((p for p in from_camera_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
                  key=lambda p: p.name.lower())


STATUS_WRITTEN = "完了"
STATUS_SKIPPED = "既に位置情報あり（書き換えずにスキップ）"

#: 写真1枚の結果の通知先 (ファイルのパス, 結果の説明, 問題があったか)
OnFile = Callable[[str, str, bool], None]


class _FileStatusTracker:
    """exiftool の出力を1行ずつ受け取り、写真1枚の処理が終わるごとに結果を通知する。

    exiftool は写真ごとに "======== ファイル [n/N]" の行を出し、その写真の警告・エラーを続けて出す。
    次の写真の行が来た時点（最後の写真は終了時）で、前の写真の結果が決まる。
    """

    def __init__(self, has_date: Dict[str, bool], on_file: Optional[OnFile]) -> None:
        self.has_date = has_date
        self.on_file = on_file
        self.current: Optional[str] = None
        self.messages: List[tuple] = []

    def feed(self, line: str) -> None:
        m = PATTERN_PROGRESS.match(line)
        if m:
            self.finish()
            self.current = m.group(1)
            return
        parsed = _split_message(line.strip())
        if parsed and self.current is not None:
            self.messages.append(parsed)

    def finish(self) -> None:
        if self.current is None:
            return
        status, problem = self._status()
        if self.on_file:
            self.on_file(self.current, status, problem)
        self.current, self.messages = None, []

    def _status(self) -> tuple:
        bodies = [body for _, body, _ in self.messages]
        if any(PATTERN_NOT_SET.match(body) for body in bodies):
            others = [body for body in bodies if not PATTERN_NOT_SET.match(body)]
            reason = reason_for(self.current, others[-1] if others else "", self.has_date)
            return f"付与できず（{reason}）", True
        errors = [body for kind, body, _ in self.messages if kind == "Error"]
        if errors:
            return f"エラー（{errors[-1]}）", True
        return STATUS_WRITTEN, False


def reason_for(path: str, message: str, has_date: Dict[str, bool]) -> str:
    """位置情報を付与できなかった理由。exiftool の警告で分からないときは、撮影日時の有無で判断する。"""
    reason = _classify_reason(message)
    name = os.path.basename(path.replace("\\", "/")).lower()
    if reason == REASON_OTHER and has_date.get(name) is False:
        return REASON_NO_DATE
    return reason


def geotag_photos(
    from_camera_dir: Path,
    gpx_files: List[Path],
    config: Optional[Any] = None,
    on_start: Optional[Callable[[List[Path]], None]] = None,
    on_file: Optional[OnFile] = None,
) -> Tuple[GeotagResult, Optional[str]]:
    """GPX ファイルをもとに、フォルダ内の写真へ位置情報を書き込む（画面には表示しない）。

    先に写真ごとに「位置情報が既にあるか」「撮影日時があるか」を調べ、書き込む写真だけを
    exiftool に渡す。スキップした写真も含めて、1枚ごとの結果を on_file に知らせる。

    Args:
        on_start: 対象の写真が決まったときに呼ぶ通知先（対象の写真の一覧）
        on_file: 写真1枚の結果が決まるごとに呼ぶ通知先 (ファイルのパス, 結果の説明, 問題があったか)

    Returns:
        (集計結果, 使ったカメラ時計のずれ補正値。補正なしなら None)

    Raises:
        ExifToolError: exiftool を実行できなかった場合
    """
    et = ExifToolClient(config=config)
    # カメラ時計のずれ補正値（"GPS時刻 − カメラ時刻"）。未設定なら None＝補正なし。
    # ※タイムゾーンは exiftool が写真の OffsetTimeOriginal（なければPCの設定）から
    #   自動で扱うため、ここで +09:00 のような値を渡してはいけない
    #   （以前の "+09:00" は exiftool に「+9分」と解釈され、位置が9分ずれていた）
    geosync = config.get_camera_clock_offset() if config is not None else None
    options = config.get_gps_options() if config is not None else {
        "skip_existing": True, "max_interpolation_secs": None, "max_extrapolation_secs": None,
    }
    targets = target_files(from_camera_dir, config)
    if on_start:
        on_start(targets)
    result = GeotagResult()
    if not targets:
        return result, geosync

    exts = target_extensions(config)
    status = et.get_gps_and_date_batch(from_camera_dir, exts)
    has_date = {name: info["has_date"] for name, info in status.items()}
    to_tag: List[Path] = []
    for path in targets:
        if options["skip_existing"] and status.get(path.name.lower(), {}).get("has_gps"):
            result.skipped_existing += 1
            if on_file:
                on_file(str(path), STATUS_SKIPPED, False)
        else:
            to_tag.append(path)

    logger.info(
        "ExifTool コマンド実行中... (path=%s, geosync=%s, 対象=%d 枚, 既存GPSでスキップ=%d 枚)",
        et.path, geosync or "なし", len(to_tag), result.skipped_existing,
    )
    if to_tag:
        tracker = _FileStatusTracker(has_date, on_file)
        output = et.geotag_from_gpx(
            target_dir=None,
            gpx_files=gpx_files,
            geosync=geosync,
            max_interpolation_secs=options["max_interpolation_secs"],
            max_extrapolation_secs=options["max_extrapolation_secs"],
            # 事前の確認で除いているが、確認に失敗した場合でも既存の位置情報を上書きしないよう、念のため条件も付ける
            skip_existing=options["skip_existing"],
            files=to_tag,
            on_line=tracker.feed,
        )
        tracker.finish()

        # exiftoolの生の出力はそのままログファイルに残す（後から詳細を確認できるように）。
        # 画面の進捗表示やサマリーと重複するため、コンソールには出さない（INFO）
        for line in output.splitlines():
            if not line.startswith("======== "):
                logger.info("exiftool: %s", line)
        parsed = parse_geotag_output(output, "")
        parsed.skipped_existing += result.skipped_existing
        parsed.not_tagged = {
            path: (REASON_NO_DATE if reason == REASON_OTHER and has_date.get(os.path.basename(path).lower()) is False
                   else reason)
            for path, reason in parsed.not_tagged.items()
        }
        result = parsed

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
    return result, geosync


def other_messages(result: GeotagResult) -> Dict[str, int]:
    """付与できなかった理由として表示する警告を除いた、その他の警告・エラーと件数。"""
    return {
        msg: n for msg, n in result.messages.items()
        if _classify_reason(msg) == REASON_OTHER or msg.startswith("Error:")
    }


# ------------------------------------------------------------------
# ステップの入口（画面表示つき）
# ------------------------------------------------------------------
TITLE = "GPS位置情報の書き込み"


def _print_summary(gpx_count: int, geosync: Optional[str], result: GeotagResult, started: float) -> None:
    console.summary_start()
    console.item(f"検出GPXファイル : {gpx_count} 件")
    console.item(f"時計ずれ補正    : {geosync if geosync else 'なし'}")
    console.item(f"位置情報付与成功: {result.updated} 件")
    if result.skipped_existing:
        console.item(f"既にGPSあり      : {result.skipped_existing} 件（書き換えずにスキップ）")

    if result.not_tagged:
        console.item(f"位置情報を付与できなかった写真: {len(result.not_tagged)} 件（手動GPS情報付与で付与が必要）")
        for reason, count in Counter(result.not_tagged.values()).most_common():
            console.sub_item(f"{reason}: {count} 件")
        console.file_names(sorted(os.path.basename(p) for p in result.not_tagged), MAX_LIST_FILES)
    elif result.unchanged:
        # 理由付きの一覧が取れなかった場合でも件数だけは表示する
        console.item(f"位置情報を付与できなかった写真: {result.unchanged} 件（手動GPS情報付与で付与が必要）")

    if result.error_files:
        console.item(f"書き込みエラー  : {result.error_files} 件（ファイルを更新できませんでした）")

    other = other_messages(result)
    if other:
        console.item("その他の警告・エラー:")
        for msg, count in Counter(other).most_common():
            console.sub_item(f"{msg}: {count} 件")
    console.step_end(TITLE, started)


def apply_gps_tags(from_camera_dir: Path, gpx_files: List[Path], config: Optional[Any] = None) -> bool:
    """GPX ファイルをもとに写真へ位置情報を書き込み、進み具合とまとめを画面に表示する。

    Args:
        from_camera_dir: 対象写真ディレクトリ
        gpx_files: GPX ファイルリスト
        config: ConfigManager。exiftoolのパス解決・時計ずれ補正値の取得に使用（Noneの場合は補正なし）

    Returns:
        処理が成功した場合True。exiftool自体の実行に失敗した場合、または
        エラーが発生して1枚も書き込めなかった場合はFalse（ワークフローを中断する）。
        一部のファイルだけ付与できなかった場合は、サマリーに表示したうえでTrue。
    """
    started = console.step_start(TITLE)
    logger.info("--- %s ---", TITLE)

    if not gpx_files:
        # GPXファイルが1件も無い場合は対象が無いだけなので、エラーにせずスキップする
        logger.info("GPXファイルが存在しないためスキップ")
        console.message("GPXファイルが見つからないため、処理をスキップします。")
        console.summary_start()
        console.item("検出GPXファイル: 0 件")
        console.item("ステータス: スキップ")
        console.step_end(TITLE, started, blank_line_before=False)
        return True

    if not from_camera_dir.exists():
        error_msg = f"対象ディレクトリが見つかりません: {from_camera_dir}"
        logger.error(error_msg, extra=ON_SCREEN)
        console.error(error_msg)
        return False

    logger.info("%d 件の GPX ファイルを検出しました", len(gpx_files))
    progress: Optional[console.Progress] = None

    def on_start(targets: List[Path]) -> None:
        nonlocal progress
        if targets:
            console.message(f"位置情報と撮影日時の有無を確認中...（{len(targets)} 枚）")
            progress = console.Progress("位置情報を書き込み", len(targets), [p.name for p in targets])
        else:
            console.message("位置情報を書き込む写真がありません。")

    def on_file(path: str, status: str, problem: bool) -> None:
        if progress is not None:
            progress.item(os.path.basename(path.replace("\\", "/")), status, problem)

    try:
        try:
            result, geosync = geotag_photos(from_camera_dir, gpx_files, config, on_start=on_start, on_file=on_file)
        except BaseException:
            if progress is not None:
                progress.abort()
            raise
        if progress is not None:
            progress.finish()
        _print_summary(len(gpx_files), geosync, result, started)

        if (result.error_files or result.has_error_line) and result.updated == 0:
            # エラーが出て1枚も書き込めていない場合は、後続ステップの前提が崩れるため失敗扱い
            console.error("位置情報を1枚も書き込めませんでした。ログを確認してください。")
            logger.error("GPS タグ書き込み失敗: エラーにより更新 0 件", extra=ON_SCREEN)
            return False
        if result.error_files:
            # 一部だけ失敗した場合は処理を続けるが、目立つように警告しておく
            console.warning(f"{result.error_files} 件のファイルを更新できませんでした。ログを確認してください。")
        return True

    except ExifToolError as e:
        logger.error("GPSタグ書き込みエラー: %s", e, extra=ON_SCREEN)
        console.error(str(e))
        return False
    except Exception as e:
        error_msg = f"GPS情報の書き込み中に予期しないエラーが発生しました: {e}"
        logger.error(error_msg, exc_info=True, extra=ON_SCREEN)
        console.error(error_msg)
        return False
