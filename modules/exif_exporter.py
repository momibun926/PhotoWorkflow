"""EXIF抽出・書き出し: 写真の EXIF 情報を書き出す。

対象フォルダとそのサブフォルダの写真（JPEG・RAW）から EXIF を読み、
<対象フォルダ>/exif_data/ に次の2種類を出力する。
  - 全タグを1行1ファイルで並べた TSV
  - 写真ごとの .exif ファイル（決めたタグだけを「見出し: 値」で並べたもの）

通常は main.py から extract_all_exif_recursive() を呼ぶ。単独でも実行できる:
    python -m modules.exif_exporter <対象フォルダ> [TSVファイル名] [exiftoolのパス]
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if __package__ in (None, ""):
    # python modules/exif_exporter.py のようにファイルを直接実行すると、下の相対 import が失敗するため
    sys.exit("このファイルは、main.py のあるフォルダで python -m modules.exif_exporter として実行してください")

from . import console, constants
from .config_manager import ConfigManager, YamlConfigManager
from .exiftool_client import ExifToolClient, ExifToolError
from .logging_config import ON_SCREEN, setup_logging

logger = logging.getLogger(__name__)


def _clean_value(val: Any, extra_chars: str = "") -> str:
    """値を1行の文字列にする。改行（と extra_chars の文字）は空白に置き換える。

    値に改行やタブが含まれていると、.exif や TSV の行・列がずれるため。
    """
    text = str(val)
    for ch in "\r\n" + extra_chars:
        text = text.replace(ch, " ")
    return text


def export_individual_exif(item: Dict[str, Any], output_dir: Path, tags: Optional[Dict[str, str]] = None) -> bool:
    """写真1枚分の .exif ファイル（例: DSC_0001.JPG.exif）を書き出す。

    Args:
        item: exiftool の JSON 出力1件分
        tags: {タグ名: 見出し}（書き出す順）。None なら constants.EXIF_EXPORT_TAGS
    """
    tags = tags or constants.EXIF_EXPORT_TAGS
    source_path_str = item.get("SourceFile", "")
    if not source_path_str:
        return False

    exif_filename = f"{Path(source_path_str).name}.exif"
    lines = [f"{label}: {_clean_value(item.get(tag, ''))}" for tag, label in tags.items()]
    try:
        with open(output_dir / exif_filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        # 1枚書けなくても残りの写真は続ける
        logger.error("個別EXIF書き込みエラー (%s): %s", exif_filename, e, extra=ON_SCREEN)
        return False
    return True


def write_tsv(data: List[Dict[str, Any]], tsv_path: Path) -> None:
    """全写真・全タグの TSV を書き出す。列は SourceFile を先頭に、残りはタグ名順（列の並びを毎回同じにするため）。"""
    all_tags = set()
    for item in data:
        all_tags.update(item.keys())
    all_tags.discard("SourceFile")
    columns = ["SourceFile"] + sorted(all_tags)

    with open(tsv_path, "w", encoding="utf-8") as f:
        f.write("\t".join(columns) + "\n")
        for item in data:
            f.write("\t".join(_clean_value(item.get(tag, ""), "\t") for tag in columns) + "\n")


def _target_extensions(config: Optional[Any]) -> List[str]:
    """抽出対象の拡張子（ドットなし）。config.json の file_types（JPEG と RAW）に従う。"""
    jpeg_exts, raw_exts = constants.JPEG_EXTENSIONS, constants.RAW_EXTENSIONS
    if config is not None:
        ext_map = config.get_file_extensions()
        jpeg_exts, raw_exts = ext_map["jpeg"], ext_map["raw"]
    return sorted(e.lstrip(".") for e in set(jpeg_exts) | set(raw_exts))


def _frame_output_dir_name(config: Optional[Any]) -> Optional[str]:
    """フレーム付与処理がフレーム付き画像を書き出すフォルダ名（YAML の output.dir_name）。取得できなければ None。

    to_note を再帰的に読むと、フレーム付き画像まで TSV に混ざるため、このフォルダを除外するのに使う。
    """
    if config is None:
        return None
    try:
        flame_config = config.get("external_tools.flame_config")
        if not flame_config:
            return None
        dir_name = YamlConfigManager(config.resolve_path(flame_config)).get("output.dir_name")
        return str(dir_name) if dir_name else None
    except Exception as e:
        # YAML が読めなくても EXIF の書き出し自体は続ける
        logger.warning("フレーム出力フォルダ名を取得できないため、除外せずに抽出します: %s", e)
        return None


@dataclass
class ExportResult:
    """EXIF抽出・書き出しの結果。if result: のように成否として使える。"""

    ok: bool = False
    read: int = 0                       # EXIF を読み取った写真の数
    written: int = 0                    # 個別 .exif を書き出せた数
    output_dir: Optional[Path] = None
    tsv_path: Optional[Path] = None
    error: str = ""                     # 失敗したときの理由

    def __bool__(self) -> bool:
        return self.ok


#: 1件終わるたびに呼ぶ通知先 (何件目, 全体の件数, ファイルのパス)
OnProgress = Callable[[int, int, str], None]


def extract_all_exif_recursive(
    target_dir: Path,
    output_dir_name: str = "exif_data",
    tsv_filename: str = "exif_summary.tsv",
    config: Optional[Any] = None,
    exiftool_path: Optional[str] = None,
    on_read: Optional[OnProgress] = None,
    on_written: Optional[Callable[[str, bool], None]] = None,
) -> ExportResult:
    """target_dir 以下の写真の EXIF を、target_dir/output_dir_name に TSV と個別 .exif で書き出す（画面には表示しない）。

    Args:
        config: ConfigManager。exiftool のパス、対象拡張子、書き出すタグの設定に使う
        exiftool_path: exiftool のパス。指定すると config の設定より優先する
        on_read: 写真1枚の EXIF を読み取るごとに呼ぶ通知先 (何件目, 全体の件数, ファイルのパス)
        on_written: 個別 .exif を1件書き出すごとに呼ぶ通知先 (元の写真のパス, 書き出せたか)

    Returns:
        ExportResult。フォルダが無い・写真が無い・exiftool や書き込みに失敗したら ok=False（error に理由）
    """
    result = ExportResult()
    if not target_dir.is_dir():
        result.error = f"指定されたディレクトリが存在しません: {target_dir}"
        logger.error("%s", result.error, extra=ON_SCREEN)
        return result

    output_dir = target_dir / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)
    result.output_dir = output_dir

    et = ExifToolClient(exiftool_path=exiftool_path, config=config)
    tags = config.get_exif_export_tags() if config is not None else dict(constants.EXIF_EXPORT_TAGS)
    extensions = _target_extensions(config)
    # 自分の出力フォルダと、フレーム付き画像の出力フォルダは読まない
    exclude_dirs = [output_dir_name]
    frame_dir = _frame_output_dir_name(config)
    if frame_dir:
        exclude_dirs.append(frame_dir)
    logger.info(
        "EXIF抽出: %s (exiftool=%s, 拡張子=%s, 除外フォルダ=%s)",
        target_dir, et.path, ",".join(extensions), ",".join(exclude_dirs),
    )

    try:
        data: List[Dict[str, Any]] = et.extract_recursive(
            directory=target_dir, extensions=extensions, group_names=True, exclude_dirs=exclude_dirs,
            on_progress=on_read,
        )
    except ExifToolError as e:
        result.error = f"ExifToolの実行に失敗しました: {e}"
        logger.error("%s", result.error, extra=ON_SCREEN)
        return result

    if not data:
        result.error = f"対象の画像ファイル（{','.join(extensions)}）または EXIF データが見つかりませんでした"
        logger.warning("%s", result.error, extra=ON_SCREEN)
        return result

    result.read = len(data)
    logger.info("%d 件のファイルを処理中...", len(data))
    for item in data:
        ok = export_individual_exif(item, output_dir, tags)
        result.written += ok
        if on_written:
            on_written(str(item.get("SourceFile", "")), ok)

    result.tsv_path = output_dir / tsv_filename
    try:
        write_tsv(data, result.tsv_path)
    except Exception as e:
        result.error = f"TSV書き込みエラー: {e}"
        logger.error("%s", result.error, exc_info=True, extra=ON_SCREEN)
        return result

    logger.info("処理完了: %s にTSV(%s)および個別.exifを出力しました", output_dir, tsv_filename)
    result.ok = True
    return result


# ------------------------------------------------------------------
# ステップの入口（画面表示つき）
# ------------------------------------------------------------------
TITLE = "EXIF抽出・書き出し処理"


class _ConsoleExportProgress:
    """EXIF の読み取りと個別ファイルの書き出しの進み具合を、写真1枚ごとに画面に表示する。

    全体の枚数は exiftool の進み具合の行（[n/全体]）で初めて分かるため、最初の通知で表示を始める。
    """

    def __init__(self) -> None:
        self.total = 0
        self.reading: Optional[console.Progress] = None
        self.writing: Optional[console.Progress] = None

    def on_read(self, current: int, total: int, path: str) -> None:
        if self.reading is None:
            self.total = total
            self.reading = console.Progress("EXIF を読み取り", total)
        self.reading.item(_name(path))

    def on_written(self, path: str, ok: bool) -> None:
        if self.writing is None:
            self._finish(self.reading)
            self.writing = console.Progress(".exif を書き出し", self.total)
        name = _name(path)
        if ok:
            self.writing.item(name, f"→ {name}.exif")
        else:
            self.writing.item(name, "書き出しに失敗", problem=True)

    def finish(self) -> None:
        self._finish(self.reading)
        self._finish(self.writing)

    def abort(self) -> None:
        for progress in (self.reading, self.writing):
            if progress is not None and progress is console.Progress.active:
                progress.abort()

    @staticmethod
    def _finish(progress: Optional[console.Progress]) -> None:
        # 終わっていない表示だけを閉じる（同じ表示を2回閉じないように）
        if progress is not None and progress is console.Progress.active:
            progress.finish()


def export_exif(target_dir: Path, tsv_filename: str = "exif_summary.tsv", config: Optional[Any] = None,
                exiftool_path: Optional[str] = None) -> ExportResult:
    """EXIF を読み取って書き出し、進み具合とまとめを画面に表示する。"""
    started = console.step_start(TITLE)
    console.message("EXIF を読み取り中...")
    progress = _ConsoleExportProgress()
    try:
        result = extract_all_exif_recursive(target_dir, tsv_filename=tsv_filename, config=config,
                                            exiftool_path=exiftool_path,
                                            on_read=progress.on_read, on_written=progress.on_written)
    except BaseException:
        progress.abort()
        raise
    progress.finish()

    if result:
        console.summary_start()
        console.item(f"読み取った写真      : {result.read} 枚")
        console.item(f"個別ファイル（.exif）: {result.written}/{result.read} 件")
        console.item(f"出力先              : {result.output_dir}")
        console.item(f"一覧（TSV）         : {result.tsv_path.name}")
    else:
        console.error(result.error)
    console.step_end(TITLE, started)
    return result


def _name(path: str) -> str:
    return os.path.basename(path.replace("\\", "/"))


if __name__ == "__main__":
    setup_logging()

    input_directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    tsv_name = sys.argv[2] if len(sys.argv) > 2 else "exif_summary.tsv"
    exiftool_arg = sys.argv[3] if len(sys.argv) > 3 else None

    app_config = None
    try:
        app_config = ConfigManager()
    except (FileNotFoundError, ValueError) as e:
        # config.json が無くても既定のタグ・拡張子で書き出せる
        logger.debug("config.json を読み込まずに実行します（既定の設定を使用）: %s", e)

    if app_config is not None:
        console.configure(per_file=app_config.get("display.per_file", False))
    ok = export_exif(input_directory, tsv_filename=tsv_name, config=app_config, exiftool_path=exiftool_arg)
    sys.exit(0 if ok else 1)
