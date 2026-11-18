"""画面表示（console）: 写真1枚ごとの進み具合と、ログ表示との調整。"""

import io
import logging
from unittest import mock

import pytest

from modules import console, logging_config


class _Screen:
    """画面への出力（pytest の capsys）を、何度でも読めるようにためておく。"""

    def __init__(self, capsys):
        self.capsys = capsys
        self.text = ""

    def getvalue(self) -> str:
        self.text += self.capsys.readouterr().out
        return self.text

    def write(self, data: str) -> None:  # ログのハンドラの出力先としても使う
        import sys
        sys.stdout.write(data)

    def flush(self) -> None:
        pass


@pytest.fixture
def screen(monkeypatch, capsys):
    """画面（対話端末）に表示したものとして扱い、出力を記録する。"""
    # pytest は出力を横取りしていて対話端末ではないため、上書き表示になるよう per_file_mode を差し替える
    monkeypatch.setattr(console, "per_file_mode", lambda: console._per_file)
    monkeypatch.setattr(console, "_terminal_width", lambda: 100)
    console.configure(per_file=False)
    yield _Screen(capsys)
    console.configure(per_file=False)
    console.Progress.active = None


def final_screen(text: str) -> list:
    """\\r による上書きを反映して、最終的に画面に残る行。"""
    lines = []
    for raw in text.split("\n"):
        lines.append(raw.split("\r")[-1].rstrip() if "\r" in raw else raw.rstrip())
    return [line for line in lines if line]


def test_overwrite_mode_keeps_only_problems_and_finish(screen):
    with console.Progress("JPEG をコピー", 3, ["a.jpg", "b.jpg", "c.jpg"]) as p:
        p.item("a.jpg")
        p.item("b.jpg", "コピーに失敗（ディスクがいっぱい）", problem=True)
        p.item("c.jpg")
    lines = final_screen(screen.getvalue())
    assert lines[0] == "  [警告] b.jpg: コピーに失敗（ディスクがいっぱい）"
    assert lines[1].startswith("  JPEG をコピー")
    assert "[####################] 100%  3/3  完了（0秒、うち要確認 1 件）" in lines[1]
    assert len(lines) == 2


def test_overwrite_line_shows_current_file(screen):
    p = console.Progress("RAW をコピー", 4)
    p.item("DSC_0001.NEF")
    last = screen.getvalue().split("\r")[-1]
    assert "[#####---------------]  25%  1/4" in last and last.rstrip().endswith("DSC_0001.NEF")


def test_overwrite_line_fits_terminal_width(screen, monkeypatch):
    monkeypatch.setattr(console, "_terminal_width", lambda: 50)
    p = console.Progress("フレームを付与", 1)
    p.item("とても長いファイル名" * 5 + ".jpg")
    assert console.display_width(screen.getvalue().split("\r")[-1].rstrip()) <= 49


def test_per_file_mode(screen):
    console.configure(per_file=True)
    with console.Progress("RAW をコピー", 2, ["R1.dng", "LONG_NAME.dng"]) as p:
        p.item("R1.dng", "完了 → 2026/11/20261101")
        p.item("LONG_NAME.dng", "unknown に保存（撮影日なし）", problem=True)
    lines = screen.getvalue().splitlines()
    assert lines[0] == "  [1/2]  50%  R1.dng         RAW をコピー        完了 → 2026/11/20261101"
    assert lines[1] == "  [2/2] 100%  LONG_NAME.dng  RAW をコピー        unknown に保存（撮影日なし）"
    assert lines[2] == "  RAW をコピー: 2/2 件 完了（0秒、うち要確認 1 件）"


def test_not_a_terminal_uses_per_file_lines(monkeypatch):
    """画面でなくファイルに出力しているとき（タスクスケジューラ等）は、上書きせず1枚ごとに1行。"""
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    console.configure(per_file=False)
    with console.Progress("EXIF を読み取り", 1) as p:
        p.item("a.jpg")
    assert "\r" not in out.getvalue()
    assert out.getvalue().splitlines()[0].startswith("  [1/1] 100%  a.jpg")


def test_abort_on_exception(screen):
    with pytest.raises(RuntimeError):
        with console.Progress("ゴミ箱へ移動", 3) as p:
            p.item("a.jpg")
            raise RuntimeError("止まった")
    assert "1/3  中断" in final_screen(screen.getvalue())[-1]
    assert console.Progress.active is None


def test_remaining_time(screen):
    p = console.Progress("フレームを付与", 10)
    p.started = 0
    p.current = 4
    with mock.patch("modules.console.time.monotonic", return_value=5):
        assert p._remaining() == "残り 約8秒"  # 4枚に5秒 → 残り6枚は約7.5秒


@pytest.mark.parametrize("seconds, text", [(0.4, "0秒"), (59, "59秒"), (65, "1分05秒"), (3720, "1時間02分")])
def test_format_duration(seconds, text):
    assert console.format_duration(seconds) == text


def test_display_width():
    assert console.display_width("abc") == 3
    assert console.display_width("写真") == 4


def test_step_end_shows_elapsed(screen):
    started = console.step_start("写真整理・コピー")
    console.step_end("写真整理・コピー", started)
    assert "【終了】写真整理・コピー（所要 0秒）" in screen.getvalue()


def _console_logger(out):
    handler = logging_config._ConsoleHandler(out)
    handler.setFormatter(logging_config._ConsoleFormatter("[%(levelname)s] %(message)s"))
    lg = logging.getLogger("test_console_log")
    lg.handlers = [handler]
    lg.propagate = False
    return lg


def test_log_messages_shown_elsewhere_are_not_repeated(screen):
    lg = _console_logger(screen)
    lg.warning("画面に出す警告")
    lg.warning("進み具合の行で表示済み", extra=logging_config.ON_SCREEN)
    assert "画面に出す警告" in screen.getvalue()
    assert "表示済み" not in screen.getvalue()


def test_console_log_hides_traceback(screen):
    lg = _console_logger(screen)
    try:
        raise ValueError("詳しい原因")
    except ValueError:
        lg.error("失敗しました", exc_info=True)
    assert "失敗しました" in screen.getvalue()
    assert "Traceback" not in screen.getvalue()


def test_log_during_progress_clears_and_redraws(screen):
    lg = _console_logger(screen)
    p = console.Progress("JPEG をコピー", 2)
    p.item("a.jpg")
    lg.warning("途中の警告")
    p.item("b.jpg")
    p.finish()
    lines = final_screen(screen.getvalue())
    assert lines[0] == "[WARNING] 途中の警告"
    assert "2/2  完了" in lines[1]
