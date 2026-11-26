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


# ------------------------------------------------------------------
# 文字の色（display.color）
# ------------------------------------------------------------------
@pytest.fixture
def color(monkeypatch):
    """色の設定を切り替える。テストの後は既定（auto）に戻す。"""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(console, "_enable_windows_vt", lambda: True)

    def _set(mode):
        console.configure(per_file=console._per_file, color=mode)

    yield _set
    console.configure(per_file=False, color="auto")


def test_color_auto_is_off_when_not_a_terminal(color):
    color("auto")  # pytest は出力を横取りしていて対話端末ではない
    assert console.paint("失敗", "error") == "失敗"


def test_color_auto_on_terminal(color, monkeypatch):
    monkeypatch.setattr(console.sys.stdout, "isatty", lambda: True, raising=False)
    color("auto")
    assert console.paint("失敗", "error") == "\033[31m失敗\033[0m"


def test_color_auto_respects_no_color(color, monkeypatch):
    monkeypatch.setattr(console.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    color("auto")
    assert console.paint("失敗", "error") == "失敗"


def test_color_always_and_never(color):
    color("always")
    assert console.paint("完了", "success") == "\033[32m完了\033[0m"
    color("never")
    assert console.paint("完了", "success") == "完了"


def test_color_off_when_windows_console_cannot_enable(color, monkeypatch):
    monkeypatch.setattr(console, "_enable_windows_vt", lambda: False)
    color("always")
    assert console.paint("失敗", "error") == "失敗"


def test_configure_without_color_keeps_color_setting(color):
    color("always")
    console.configure(per_file=True)  # EXIF抽出の単独実行などは per_file だけを設定し直す
    assert console.color_enabled()


def test_unknown_style_and_empty_text_are_left_as_is(color):
    color("always")
    assert console.paint("文字", "nothing") == "文字"
    assert console.paint("", "error") == ""


def test_display_width_ignores_color_codes(color):
    color("always")
    assert console.display_width(console.paint("完了", "success")) == 4
    assert console.strip_color(console.paint("完了", "success")) == "完了"


def test_messages_are_colored(screen, color):
    color("always")
    console.error("書き込めません")
    console.warning("撮影日なし")
    console.step_start("写真整理・コピー")
    out = screen.getvalue()
    assert "\033[31m -> [エラー] 書き込めません\033[0m" in out
    assert "\033[33m -> [警告] 撮影日なし\033[0m" in out
    assert "\033[1;36m 【開始】写真整理・コピー\033[0m" in out


def test_progress_bar_colors_and_width(screen, color):
    color("always")
    p = console.Progress("JPEG をコピー", 2)
    p.item("a.jpg")
    # 色のコードの分だけ行の幅を多く数えない（上書きで消す幅が正しい）
    drawn_line = console.strip_color(screen.getvalue().split("\r")[-1])
    assert p._drawn == console.display_width(drawn_line.rstrip())
    p.item("b.jpg", "失敗", problem=True)
    p.finish()
    out = screen.getvalue()
    assert "\033[36m[##########----------]\033[0m" in out          # 実行中はシアン
    assert "\033[33m  [警告] b.jpg: 失敗\033[0m" in out              # 問題のあった写真は黄
    assert "\033[33m[####################]\033[0m" in out           # 要確認ありで終わったバーは黄
    lines = [console.strip_color(line) for line in final_screen(out)]
    assert lines[-1].startswith("  JPEG をコピー")
    assert "2/2  完了（0秒、うち要確認 1 件）" in lines[-1]


def test_progress_without_problems_finishes_green(screen, color):
    color("always")
    with console.Progress("JPEG をコピー", 1) as p:
        p.item("a.jpg")
    out = screen.getvalue()
    assert "\033[32m[####################]\033[0m" in out
    assert "\033[32m完了（0秒）\033[0m" in out


def test_progress_abort_is_red(screen, color):
    color("always")
    with pytest.raises(RuntimeError):
        with console.Progress("JPEG をコピー", 2) as p:
            p.item("a.jpg")
            raise RuntimeError
    assert "\033[31m中断（0秒）\033[0m" in screen.getvalue()


def test_per_file_problem_line_is_yellow(screen, color):
    color("always")
    console.configure(per_file=True)
    with console.Progress("RAW をコピー", 1, names=["a.nef"]) as p:
        p.item("a.nef", "unknown に保存（撮影日なし）", problem=True)
    out = screen.getvalue()
    assert "\033[33m  [1/1] 100%  a.nef" in out
    assert "\033[33m  RAW をコピー: 1/1 件 完了" in out


def test_console_log_colors_by_level(screen, color):
    color("always")
    lg = _console_logger(screen)
    lg.warning("警告です")
    lg.error("エラーです")
    out = screen.getvalue()
    assert "\033[33m[WARNING] 警告です\033[0m" in out
    assert "\033[31m[ERROR] エラーです\033[0m" in out


def test_no_color_codes_without_color(screen, color):
    color("never")
    console.error("書き込めません")
    with console.Progress("JPEG をコピー", 1) as p:
        p.item("a.jpg")
    assert "\033[" not in screen.getvalue()
