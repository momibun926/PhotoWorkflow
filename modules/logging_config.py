"""ロギング設定をプロジェクト全体で一元化するモジュール。

main.py は他モジュールを同一プロセス内でimportして呼び出すが、
manual_gps_tagger.py と exif_exporter.py は subprocess として
別プロセスで起動される（GUIのため／別プロセスに隔離するため）。
これらが各々 logging.basicConfig() を呼んで独自にログ設定すると、

  - INFOレベルの詳細ログが（本来ファイルだけに書くはずが）
    標準エラー出力にも流れてユーザー画面を埋め尽くす
  - ログの書き先が photo_organizer.log 以外に分散する
  - stdout（print()によるUI表示）とstderr（ログ）のバッファリング差で
    表示順が前後する

という問題が起きる。本モジュールの setup_logging() を全エントリー
ポイント（main.py / manual_gps_tagger.py / exif_exporter.py /
frame_processor.py の __main__ ブロック）から呼ぶことで、
「詳細はファイルに、画面にはWARNING以上を簡潔に」という方針を統一する。
"""

import logging  # 標準ロギングモジュール。ルートロガーに対してハンドラを設定する
import sys  # コンソール出力先(sys.stdout)を明示的に指定するために使用
from pathlib import Path  # ログファイルの出力先パスをOS非依存に扱うために使用

try:
    # パッケージとして実行された場合（本来の使われ方）
    # 同じmodulesパッケージ内のconstantsモジュールから定数（ログ書式等）をインポート
    from . import constants
except ImportError:
    # 単独スクリプトとして実行されたモジュール（exif_exporter.py等）から
    # フォールバック経由でimportされた場合
    # 相対importが失敗するケース（パッケージ外から単体実行された場合）に備えた通常importでの再試行
    import constants

# プロジェクトルート直下の共通ログファイル。
# 本ファイル（modules/logging_config.py）から見て一つ上の階層が
# main.py と同じプロジェクトルートになるため、呼び出し元がどこで
# 実行されても同じログファイルに集約される。
DEFAULT_LOG_FILE: Path = Path(__file__).resolve().parent.parent / "photo_organizer.log"

# setup_logging() が既に一度実行されたかどうかを記録するモジュールレベルのフラグ。
# 同一プロセス内で複数回呼び出されてもハンドラが多重登録されないようにするためのガード。
_configured = False


def setup_logging(
    log_file: Path = DEFAULT_LOG_FILE,
    console_level: int = logging.WARNING,
    file_level: int = logging.INFO,
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
    """
    global _configured
    # 既に設定済みなら何もせず即座に戻る（二重設定によるハンドラ重複を防ぐ）
    if _configured:
        return

    # ルート（無名）ロガーを取得し、全体のログレベルはファイル/コンソールのうち
    # より詳細な（数値の小さい）方に合わせておく。個々のハンドラ側で再度絞り込む。
    root = logging.getLogger()
    root.setLevel(min(console_level, file_level))

    # basicConfig 等で既に追加されている可能性のあるハンドラを除去してから設定し直す
    for h in list(root.handlers):
        root.removeHandler(h)

    # ファイル出力用ハンドラ: 詳細な書式（時刻・レベル・ロガー名付き）でUTF-8で書き出す
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(constants.LOG_FORMAT, datefmt=constants.LOG_DATE_FORMAT))
    root.addHandler(file_handler)

    # コンソール出力用ハンドラ: 標準出力へ簡潔な書式（レベルとメッセージのみ）で表示する
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(constants.CONSOLE_LOG_FORMAT))
    root.addHandler(console_handler)

    # 設定完了フラグを立て、以降の呼び出しを早期リターンさせる
    _configured = True
