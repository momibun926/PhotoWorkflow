"""写真整理・コピー: 取り込んだ写真を分類してコピーし、元のファイルをゴミ箱へ移す。

  - JPEG: to_note と追加のコピー先（copy_targets）すべてへコピーする
  - RAW: 撮影日ごとのフォルダ（base/年/月/年月日）へコピーする。撮影日が分からなければ base/unknown
  - コピー先すべてへのコピーに成功したファイルだけを、取り込み元からゴミ箱へ移す

処理の関数（copy_jpegs など）は画面に表示せず、進み具合や失敗は引数の通知先（on_copied / on_failed）
に知らせる。画面に表示するのはステップの入口 copy_and_organize_photos() だけ。
"""

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

try:
    # 未インストールでもコピーまでは動くようにする（ゴミ箱への移動だけスキップする）
    import send2trash
except ImportError:
    send2trash = None

from . import console, constants
from .exif_utils import ExifReader
from .logging_config import ON_SCREEN

logger = logging.getLogger(__name__)

#: 1件終わるたびに呼ぶ通知先 (何件目, 全体の件数, ファイル, コピー先のフォルダ)
OnCopied = Callable[[int, int, Path, Path], None]
#: 1件ゴミ箱へ移すたびに呼ぶ通知先 (ファイル)
OnTrashed = Callable[[Path], None]
#: 失敗したときに呼ぶ通知先 (ファイル, 例外)
OnFailed = Callable[[Path, Exception], None]

UNKNOWN_DATE_DIR = "unknown"


@dataclass
class CopyResult:
    copied: List[Path] = field(default_factory=list)        # コピー先すべてへのコピーに成功したファイル
    failed: List[Path] = field(default_factory=list)        # コピーに失敗したファイル（取り込み元に残す）
    unknown_date: List[Path] = field(default_factory=list)  # 撮影日が分からず unknown へ入れた RAW


# ------------------------------------------------------------------
# 処理
# ------------------------------------------------------------------
def categorize_files(from_dir: Path, config: Optional[Any] = None) -> Tuple[List[Path], List[Path], List[Path]]:
    """フォルダ直下のファイルを (JPEG, RAW, GPX) に分ける。拡張子は config.json の file_types に従う。"""
    if config is not None:
        ext_map = config.get_file_extensions()
        jpeg_exts, raw_exts, gpx_exts = ext_map["jpeg"], ext_map["raw"], ext_map["gpx"]
    else:
        jpeg_exts, raw_exts, gpx_exts = constants.JPEG_EXTENSIONS, constants.RAW_EXTENSIONS, constants.GPX_EXTENSIONS

    jpeg_files: List[Path] = []
    raw_files: List[Path] = []
    gpx_files: List[Path] = []
    try:
        for entry in from_dir.iterdir():
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext in jpeg_exts:
                jpeg_files.append(entry)
            elif ext in raw_exts:
                raw_files.append(entry)
            elif ext in gpx_exts:
                gpx_files.append(entry)
        logger.info("ファイル分類完了: JPEG=%d, RAW=%d, GPX=%d", len(jpeg_files), len(raw_files), len(gpx_files))
    except Exception as e:
        logger.error("ファイル分類エラー: %s", e, exc_info=True)
        raise
    return jpeg_files, raw_files, gpx_files


def _copy_verified(src: Path, dst: Path) -> None:
    """src を dst にコピーし、コピー後のサイズが元と一致することを確認する。

    サイズが一致しない場合（ディスク容量不足などで途中までしか書けなかった場合）は
    OSError を送出する。呼び出し側はこれを「コピー失敗」として扱い、元ファイルを削除しない。
    """
    shutil.copy2(src, dst)
    src_size = src.stat().st_size
    dst_size = dst.stat().st_size
    if src_size != dst_size:
        raise OSError(f"コピー後のサイズが一致しません（元 {src_size} バイト / コピー先 {dst_size} バイト）: {dst}")


def make_output_dirs(dirs: List[Path]) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    logger.info("出力ディレクトリ作成完了（%d 件）", len(dirs))


def copy_jpegs(jpeg_files: List[Path], destinations: List[Path],
               on_copied: Optional[OnCopied] = None, on_failed: Optional[OnFailed] = None) -> CopyResult:
    """JPEG を destinations のすべてへコピーする。1か所でも失敗したファイルは failed に入る。"""
    result = CopyResult()
    total = len(jpeg_files)
    logger.info("JPEG ファイル (%d 件) のコピーを開始 (コピー先: %d 件)", total, len(destinations))
    for idx, jpeg in enumerate(jpeg_files, start=1):
        try:
            for dest in destinations:
                _copy_verified(jpeg, dest / jpeg.name)
        except Exception as e:
            logger.error("JPEG コピーエラー (%s): %s", jpeg.name, e, extra=ON_SCREEN)
            result.failed.append(jpeg)
            if on_failed:
                on_failed(jpeg, e)
            continue
        result.copied.append(jpeg)
        logger.debug("JPEG コピー完了 [%d/%d]: %s", idx, total, jpeg.name)
        if on_copied:
            on_copied(idx, total, jpeg, destinations[0] if destinations else Path())
    return result


def raw_destination(base_dir: Path, date_str: Optional[str]) -> Optional[Path]:
    """撮影日（"YYYYMMDD"）から RAW の保存先 base/年/月/年月日 を返す。撮影日が使えなければ None。"""
    if date_str and len(date_str) == 8 and date_str.isdigit():
        return base_dir / date_str[:4] / date_str[4:6] / date_str
    return None


def copy_raws(raw_files: List[Path], base_dir: Path, config: Optional[Any] = None,
              on_copied: Optional[OnCopied] = None, on_failed: Optional[OnFailed] = None) -> CopyResult:
    """RAW を撮影日ごとのフォルダへコピーする。

    撮影日が分からないものは、推測の日付で振り分けず base/unknown にまとめる
    （ファイルの更新日時・作成日時は、GPS を書き込んだ日やコピーした日に変わるため使わない）。
    """
    result = CopyResult()
    total = len(raw_files)
    logger.info("RAW ファイル (%d 件) のコピーを開始", total)
    if not raw_files:
        return result

    date_map = ExifReader(config=config).get_exif_dates_batch(raw_files)
    for idx, raw in enumerate(raw_files, start=1):
        try:
            target_dir = raw_destination(base_dir, date_map.get(raw))
            if target_dir is None:
                logger.warning("RAW ファイルの撮影日が取得できません: %s", raw.name, extra=ON_SCREEN)
                target_dir = base_dir / UNKNOWN_DATE_DIR
                result.unknown_date.append(raw)
            target_dir.mkdir(parents=True, exist_ok=True)
            _copy_verified(raw, target_dir / raw.name)
        except Exception as e:
            logger.error("RAW コピーエラー (%s): %s", raw.name, e, extra=ON_SCREEN)
            result.failed.append(raw)
            if on_failed:
                on_failed(raw, e)
            continue
        result.copied.append(raw)
        logger.debug("RAW コピー完了 [%d/%d]: %s -> %s", idx, total, raw.name, target_dir)
        if on_copied:
            on_copied(idx, total, raw, target_dir)
    return result


#: ゴミ箱への移動に失敗したときに、やり直すまで待つ秒数（この回数だけやり直す）。
#: コピーした直後は、ウイルス対策ソフトや Windows の検索インデックスがファイルを開いていて
#: 一時的に失敗することがあるため、少し待ってからやり直す
TRASH_RETRY_DELAYS: Tuple[float, ...] = (0.5, 1.0, 2.0)


def _send_to_trash_with_retry(path: Path) -> None:
    """path をゴミ箱へ移す。失敗したら TRASH_RETRY_DELAYS の秒数だけ待ってやり直し、最後の失敗は例外のまま送出する。"""
    for attempt, delay in enumerate((*TRASH_RETRY_DELAYS, None), start=1):
        try:
            send2trash.send2trash(str(path))
            if attempt > 1:
                logger.info("ゴミ箱移動: %s（%d 回目で成功）", path.name, attempt)
            return
        except Exception as e:
            if delay is None:
                raise
            logger.info("ゴミ箱移動に失敗したため %.1f 秒後にやり直します (%s): %s", delay, path.name, e)
            time.sleep(delay)


def trash_available() -> bool:
    return send2trash is not None


def trash_files(files: List[Path], on_failed: Optional[OnFailed] = None,
                on_trashed: Optional[OnTrashed] = None) -> int:
    """files をゴミ箱へ移し、移せた件数を返す。send2trash が無ければ何もせず 0。"""
    logger.info("元ファイル (%d 件) をゴミ箱へ移動開始", len(files))
    if not trash_available():
        logger.warning("send2trash がインストールされていません。pip install send2trash を実行してください", extra=ON_SCREEN)
        return 0
    trashed = 0
    for path in files:
        try:
            _send_to_trash_with_retry(path)
        except Exception as e:
            logger.error("ゴミ箱移動エラー (%s): %s", path.name, e, extra=ON_SCREEN)
            if on_failed:
                on_failed(path, e)
            continue
        trashed += 1
        logger.debug("ゴミ箱移動: %s", path.name)
        if on_trashed:
            on_trashed(path)
    return trashed


# ------------------------------------------------------------------
# ステップの入口（画面表示つき）
# ------------------------------------------------------------------
TITLE = "写真整理・コピー"


def copy_and_organize_photos(
    jpeg_files: List[Path],
    nef_files: List[Path],
    to_note_dir: Path,
    copy_targets: List[Path],
    base_dir: Path,
    config: Optional[Any] = None,
) -> bool:
    """写真を整理・コピーし、コピーできた元のファイルをゴミ箱へ移す。進み具合とまとめを画面に表示する。

    Args:
        jpeg_files / nef_files: 取り込み元の JPEG / RAW
        to_note_dir: JPEG のコピー先（EXIF抽出・書き出しとフレーム付与の対象にもなる）
        copy_targets: JPEG の追加のコピー先（Amazon Photos 用フォルダなど。0件でもよい）
        base_dir: RAW を撮影日ごとに保存するフォルダ
        config: ConfigManager。exiftool のパスの取得に使う

    Returns:
        すべてのファイルのコピーに成功したら True。1件でも失敗したら False
        （失敗したファイルはゴミ箱へ移さず、取り込み元に残す）
    """
    started = console.step_start(TITLE)
    logger.info("--- %s ---", TITLE)

    try:
        make_output_dirs([to_note_dir, *copy_targets])
    except Exception as e:
        logger.error("ディレクトリ作成エラー: %s", e, extra=ON_SCREEN)
        console.error(f"ディレクトリ作成に失敗しました: {e}")
        return False

    def failed(progress: console.Progress, what: str) -> OnFailed:
        return lambda path, e: progress.item(path.name, f"{what}に失敗（{e}）", problem=True)

    jpeg_result = CopyResult()
    if jpeg_files:
        with console.Progress("JPEG をコピー", len(jpeg_files), [p.name for p in jpeg_files]) as progress:
            jpeg_result = copy_jpegs(
                jpeg_files, [to_note_dir, *copy_targets],
                on_copied=lambda i, n, path, dest: progress.item(path.name),
                on_failed=failed(progress, "コピー"),
            )

    raw_result = CopyResult()
    if nef_files:
        console.message(f"RAW の撮影日を読み取り中...（{len(nef_files)} 枚）")
        with console.Progress("RAW をコピー", len(nef_files), [p.name for p in nef_files]) as progress:

            def raw_copied(i: int, n: int, path: Path, dest: Path) -> None:
                if dest.name == UNKNOWN_DATE_DIR:
                    progress.item(path.name, f"{UNKNOWN_DATE_DIR} に保存（撮影日なし）", problem=True)
                else:
                    progress.item(path.name, f"完了 → {dest.relative_to(base_dir).as_posix()}")

            raw_result = copy_raws(nef_files, base_dir, config, on_copied=raw_copied,
                                   on_failed=failed(progress, "コピー"))

    copied = jpeg_result.copied + raw_result.copied
    failed_files = jpeg_result.failed + raw_result.failed
    if failed_files:
        logger.warning("コピーに失敗したため取り込み元に残すファイル: %d 件", len(failed_files), extra=ON_SCREEN)

    trashed = 0
    trash_failed: List[Path] = []
    if not trash_available():
        trash_files(copied)  # ログに理由を残す
        console.warning("send2trash がインストールされていないため、ゴミ箱への移動をスキップします")
    elif copied:
        with console.Progress("ゴミ箱へ移動", len(copied), [p.name for p in copied]) as progress:
            on_trash_failed = failed(progress, "ゴミ箱への移動")

            def trash_error(path: Path, e: Exception) -> None:
                trash_failed.append(path)
                on_trash_failed(path, e)

            trashed = trash_files(copied, on_failed=trash_error, on_trashed=lambda path: progress.item(path.name))

    console.summary_start()
    console.item(f"JPEG コピー完了 : {len(jpeg_result.copied)}/{len(jpeg_files)} 件")
    console.item(f"RAW コピー完了  : {len(raw_result.copied)}/{len(nef_files)} 件")
    if failed_files:
        console.item(f"コピー失敗      : {len(failed_files)} 件（取り込み元に残しています。原因を確認して再実行してください）")
        console.file_names(sorted(p.name for p in failed_files))
    if raw_result.unknown_date:
        console.item(f"撮影日不明      : {len(raw_result.unknown_date)} 件 → {base_dir / UNKNOWN_DATE_DIR} に保存"
                     "（手動で振り分けてください）")
        console.file_names(sorted(p.name for p in raw_result.unknown_date))
    console.item(f"ゴミ箱移動完了 : {trashed}/{len(copied)} 件")
    if trash_failed:
        console.item(f"ゴミ箱へ移せず  : {len(trash_failed)} 件（コピーは済んでいます。取り込み元に残っているので、"
                     "もう一度「写真整理・コピー」を実行すると移せます）")
        console.file_names(sorted(p.name for p in trash_failed))
    console.step_end(TITLE, started)

    logger.info("%s完了: JPEG=%d, RAW=%d, コピー失敗=%d, Trash=%d/%d",
                TITLE, len(jpeg_files), len(nef_files), len(failed_files), trashed, len(copied))
    if failed_files:
        logger.warning("コピー失敗ファイル: %s", ", ".join(sorted(p.name for p in failed_files)), extra=ON_SCREEN)
    return not failed_files
