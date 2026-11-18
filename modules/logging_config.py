"""ログの設定をプロジェクト全体でそろえる。

方針: 詳細（INFO 以上）は photo_organizer.log に、画面には WARNING 以上だけを簡潔に出す。

エントリーポイント（main.py と、各モジュールを単独で実行したときの __main__）は
必ず setup_logging() を呼ぶ。特に別プロセスで起動する manual_gps_tagger.py が
独自に logging.basicConfig() を呼ぶと、INFO ログが画面に流れて表示が埋もれたり、
ログの書き先が分かれたりするため。
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from . import constants

# main.py と同じフォルダの photo_organizer.log。どこから起動しても同じファイルにまとめる
DEFAULT_LOG_FILE: Path = Path(__file__).resolve().parent.parent / "photo_organizer.log"

# 同じプロセスで2回呼ばれても、ハンドラを重複して登録しないためのフラグ
_configured = False

#: logger.warning(..., extra=ON_SCREEN) のように付けると、画面のログ表示では省く（ファイルには残す）。
#: 同じ内容を進み具合の行（console.Progress）などで既に画面に出していて、二重に表示しないため
ON_SCREEN = {"shown_on_screen": True}


class _ConsoleHandler(logging.StreamHandler):
    """画面へのログ表示。上書き中の進み具合の行を消してから表示し、表示後に描き直す。"""

    def __init__(self, stream=None) -> None:
        super().__init__(stream)
        self.addFilter(lambda record: not getattr(record, "shown_on_screen", False))

    def emit(self, record: logging.LogRecord) -> None:
        from .console import Progress  # console は logging_config に依存しないため、ここで読み込む

        active = Progress.active
        if active is not None:
            active.clear()
        super().emit(record)
        if active is not None:
            active.redraw()


class _ConsoleFormatter(logging.Formatter):
    """画面にはエラーの詳細（トレースバック）を出さない。詳細はログファイルに残る。"""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text, record.stack_info
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """切り替えに失敗してもログ出力を止めない RotatingFileHandler。

    手動GPS付与ツール（manual_gps_tagger.py）は別プロセスで同じログファイルに書き込む。
    Windows では他のプロセスが開いているファイルの名前を変えられないため、
    切り替え（リネーム）が失敗することがある。その場合は今のファイルに追記を続け、
    このプロセスでは以後切り替えを試みない（次回起動時に改めて切り替わる）。
    """

    _rollover_failed = False

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self._rollover_failed:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._rollover_failed = True
            if self.stream is None:
                self.stream = self._open()


def setup_logging(
    log_file: Path = DEFAULT_LOG_FILE,
    console_level: int = logging.WARNING,
    file_level: int = logging.INFO,
    max_bytes: int = constants.LOG_MAX_BYTES,
    backup_count: int = constants.LOG_BACKUP_COUNT,
) -> None:
    """ルートロガーを設定する。

    - ファイル: file_level（デフォルトINFO）以上を詳細な書式で記録
    - コンソール（標準出力）: console_level（デフォルトWARNING）以上を
      簡潔な書式（タイムスタンプ・ロガー名なし）で表示

    同一プロセス内で複数回呼ばれても二重にハンドラが増えないよう、
    最初の呼び出し以降は何もしない。

    Args:
        log_file: ログファイルの出力先パス
        console_level: コンソールに表示する最低ログレベル
        file_level: ファイルに記録する最低ログレベル
        max_bytes: ログファイル1つの上限サイズ（バイト）。超えると古いファイルとして切り替える
        backup_count: 残す古いログファイルの数
    """
    global _configured
    if _configured:
        return

    # 全体のレベルは詳しい方に合わせ、ハンドラごとに絞り込む
    root = logging.getLogger()
    root.setLevel(min(console_level, file_level))

    # basicConfig 等で既に追加されている可能性のあるハンドラを除去してから設定し直す
    for h in list(root.handlers):
        root.removeHandler(h)

    # サイズが max_bytes を超えたら photo_organizer.log.1, .2 ... に切り替え、
    # backup_count 個より古いものは削除する（ログファイルが際限なく大きくならないように）
    file_handler = _SafeRotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(constants.LOG_FORMAT, datefmt=constants.LOG_DATE_FORMAT))
    root.addHandler(file_handler)

    console_handler = _ConsoleHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(_ConsoleFormatter(constants.CONSOLE_LOG_FORMAT))
    root.addHandler(console_handler)

    _configured = True
