"""画面（コンソール）への表示をまとめたモジュール。

処理を行う関数は画面に直接 print せず、各ステップの入口（apply_gps_tags など）が
ここの関数を使って表示する。表示の形（区切り線・見出し・進み具合・まとめ）を全ステップでそろえるため、
また将来メニューを専用のウィンドウにする場合に、処理の関数をそのまま使えるようにするため。

写真1枚ごとの進み具合は Progress で表示する。表示の形は2通り:
  - 1行を上書き（既定）: 進み具合のバー・件数・残り時間・今のファイル名を1行で更新する。
    警告やエラーはその場で1行ずつ残す。
  - 1枚ごとに1行: 処理した写真を1行ずつ表示する（config.json の display.per_file を true）。
画面ではなくファイルなどに出力しているとき（タスクスケジューラ等）は、上書きができないため
自動で「1枚ごとに1行」にする。

文字の色（config.json の display.color）:
  - auto（既定）: 画面（対話端末）に表示しているときだけ色を付ける。ファイルなどに出力しているとき、
    環境変数 NO_COLOR があるとき、TERM=dumb のときは付けない
  - always: いつも付ける / never: 付けない
色は ANSI のエスケープコードで付ける（追加のパッケージは不要）。Windows では Windows 10 以降の
コンソールの色表示（仮想ターミナル処理）を最初に色を使うときに有効にし、有効にできなければ色なしにする。
"""

import os
import re
import shutil
import sys
import time
import unicodedata
from typing import Iterable, Optional

RULE = "=" * 50
#: 一覧に表示するファイル名の最大件数（多すぎると画面が流れてしまうため。全件はログに残す）
MAX_LIST = 20
BAR_WIDTH = 20
#: 1枚ごとに1行の表示で、ファイル名の欄の最大幅（これより長い名前は欄からはみ出して表示する）
MAX_NAME_WIDTH = 30
#: 処理の名前（"JPEG をコピー" など）の欄の幅。段階が変わっても、バーや件数の位置をそろえるため
LABEL_WIDTH = 18

_per_file = False

#: display.color に書ける値
COLOR_MODES = ("auto", "always", "never")
_color_mode = "auto"
#: 色を付けるかどうかの判定結果（最初に色を使うときに決める。configure で色の設定を変えると決め直す）
_color_enabled: Optional[bool] = None

# 役割ごとの色（ANSI の SGR コード）。黒背景・白背景のどちらでも読めるよう、標準の8色だけを使う
_STYLES = {
    "error": "31",        # 赤: エラー・失敗・中断
    "warning": "33",      # 黄: 警告・要確認・無効な入力
    "success": "32",      # 緑: 完了
    "heading": "1;36",    # シアン（太字）: 見出し
    "key": "1;36",        # シアン（太字）: メニューの番号
    "muted": "90",        # 灰色: 区切り線・注記・実行できない項目
    "bar": "36",          # シアン: 実行中の進み具合のバー
}
_RESET = "\033[0m"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def configure(per_file: bool = False, color: Optional[str] = None) -> None:
    """表示の形を決める。per_file=True なら1枚ごとに1行。

    color は "auto" / "always" / "never"（display.color）。None なら色の設定は変えない
    （EXIF抽出を単独で実行したときなど、per_file だけを設定し直す呼び出しがあるため）。
    知らない値は "auto" として扱う（設定の検証は config_manager で行う）。
    """
    global _per_file, _color_mode, _color_enabled
    _per_file = bool(per_file)
    if color is not None:
        mode = str(color).strip().lower()
        _color_mode = mode if mode in COLOR_MODES else "auto"
        _color_enabled = None


def color_enabled() -> bool:
    """文字に色を付けるか。"""
    global _color_enabled
    if _color_enabled is None:
        _color_enabled = _decide_color()
    return _color_enabled


def _decide_color() -> bool:
    if _color_mode == "never":
        return False
    if _color_mode == "auto":
        if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
            return False
        try:
            if not sys.stdout.isatty():
                return False
        except (AttributeError, ValueError):
            return False
    return _enable_windows_vt()


def _enable_windows_vt() -> bool:
    """Windows のコンソールで ANSI のエスケープコードを使えるようにする。Windows 以外は何もしない。

    Windows Terminal はもともと使えるが、従来のコンソール（conhost）では
    ENABLE_VIRTUAL_TERMINAL_PROCESSING を立てる必要がある。立てられなければ False（色なし）。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # コンソールではない（always でファイルに出しているときなど）。色のコードはそのまま出す
            return _color_mode == "always"
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def paint(text: str, style: str) -> str:
    """text に役割（"error" / "warning" / "success" / "heading" / "key" / "muted" / "bar"）の色を付ける。

    色を付けない設定・環境では text をそのまま返す。
    """
    code = _STYLES.get(style)
    if not text or code is None or not color_enabled():
        return text
    return f"\033[{code}m{text}{_RESET}"


def strip_color(text: str) -> str:
    """色のコードを取り除く。"""
    return _ANSI_RE.sub("", text)


def per_file_mode() -> bool:
    """1枚ごとに1行で表示するか。画面（対話端末）でなければ常に True。"""
    try:
        is_tty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        is_tty = False
    return _per_file or not is_tty


# ------------------------------------------------------------------
# 見出し・まとめ
# ------------------------------------------------------------------
def step_start(title: str, blank_line_before: bool = True) -> float:
    """ステップの開始の見出し。終了の見出しでかかった時間を出すため、開始時刻を返す。"""
    print(("\n" if blank_line_before else "") + paint(RULE, "muted"))
    print(paint(f" 【開始】{title}", "heading"))
    print(paint(RULE, "muted"))
    return time.monotonic()


def step_end(title: str, started: Optional[float] = None, blank_line_before: bool = True) -> None:
    """ステップの終了の見出し。started（step_start の戻り値）があれば、かかった時間を付ける。"""
    if blank_line_before:
        print("\n")
    print(paint(RULE, "muted"))
    suffix = f"（所要 {format_duration(time.monotonic() - started)}）" if started is not None else ""
    print(paint(f" 【終了】{title}{suffix}", "heading"))
    print(paint(RULE, "muted"))


def summary_start() -> None:
    print("\n")
    print(paint(" 【処理のサマリー】", "heading"))


def item(text: str) -> None:
    """サマリーの1項目（・で始まる行）。"""
    print(f"    ・{text}")


def sub_item(text: str) -> None:
    """サマリーの項目の内訳（* で始まる行）。"""
    print(f"       * {text}")


def file_names(names: Iterable[str], limit: int = MAX_LIST) -> None:
    """ファイル名の一覧。limit 件を超える分は件数だけ表示する。"""
    names = list(names)
    for name in names[:limit]:
        print(f"         - {name}")
    if len(names) > limit:
        print(paint(f"         ...ほか {len(names) - limit} 件（全件はログに記録）", "muted"))


def message(text: str) -> None:
    """処理中のお知らせ（" -> " で始まる行）。"""
    _print_line(f" -> {text}")


def error(text: str, blank_line_before: bool = False) -> None:
    _print_line(("\n" if blank_line_before else "") + paint(f" -> [エラー] {text}", "error"))


def warning(text: str) -> None:
    _print_line(paint(f" -> [警告] {text}", "warning"))


def format_duration(seconds: float) -> str:
    """経過時間を "12秒" / "1分05秒" / "1時間02分" の形にする。"""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分"


# ------------------------------------------------------------------
# 写真1枚ごとの進み具合
# ------------------------------------------------------------------
def display_width(text: str) -> int:
    """画面上の幅（全角文字は2として数える。色のコードは数えない）。"""
    text = strip_color(text)
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _fit(text: str, width: int) -> str:
    """画面上の幅が width を超えないよう末尾を切る。"""
    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


class Progress:
    """写真1枚ごとの進み具合を表示する。

        with Progress("JPEG をコピー", total=len(files), names=[f.name for f in files]) as progress:
            for f in files:
                ...
                progress.item(f.name)                                       # 正常に終わった
                progress.item(f.name, "失敗（ディスクがいっぱい）", problem=True)  # 問題があった
        # with を抜けると完了の行を表示する（途中で例外が起きたら「中断」と表示する）

    problem=True の写真は、1行を上書きする表示でも警告としてその場に1行残す。
    """

    #: 表示中の Progress（ログの表示で上書き中の行を消すため。logging_config から参照する）
    active: Optional["Progress"] = None

    def __init__(self, label: str, total: int, names: Iterable[str] = ()) -> None:
        self.label = label
        self.total = total
        self.current = 0
        self.problems = 0
        self.started = time.monotonic()
        self.per_file = per_file_mode()
        widest = max((display_width(n) for n in names), default=0)
        self.name_width = min(widest, MAX_NAME_WIDTH)
        self._drawn = 0         # 上書き表示で、今の行に書いた幅
        self._last_name = ""
        Progress.active = self

    # --- 呼び出し側が使うメソッド ---
    def item(self, name: str, status: str = "完了", problem: bool = False) -> None:
        """1枚の処理が終わった。"""
        self.current += 1
        self._last_name = name
        if problem:
            self.problems += 1
        if self.per_file:
            line = self._per_file_line(name, status)
            print(paint(line, "warning") if problem else line, flush=True)
            return
        if problem:
            self.note(paint(f"  [警告] {name}: {status}", "warning"))
        self._draw()

    def note(self, text: str) -> None:
        """写真1枚に結びつかないお知らせを、その場に1行残す。"""
        self.clear()
        print(text, flush=True)
        if not self.per_file and self.current:
            self._draw()

    def finish(self) -> None:
        """完了の行を表示する。"""
        elapsed = format_duration(time.monotonic() - self.started)
        done = f"{self.current}/{self.total}"
        # 問題（失敗・スキップなど）があった写真の数も書き、「全部うまくいった」と誤解されないようにする
        detail = f"{elapsed}、うち要確認 {self.problems} 件" if self.problems else elapsed
        # 要確認の写真があれば黄、なければ緑
        style = "warning" if self.problems else "success"
        if self.per_file:
            print(paint(f"  {self.label}: {done} 件 完了（{detail}）", style), flush=True)
        else:
            self._draw(final=f"完了（{detail}）", final_style=style)
            print(flush=True)
            self._drawn = 0
        if Progress.active is self:
            Progress.active = None

    def abort(self) -> None:
        """途中で止まったことを表示する（例外や Ctrl+C のとき）。"""
        elapsed = format_duration(time.monotonic() - self.started)
        if self.per_file:
            print(paint(f"  {self.label}: {self.current}/{self.total} 件で中断（{elapsed}）", "error"), flush=True)
        else:
            self._draw(final=f"中断（{elapsed}）", final_style="error")
            print(flush=True)
            self._drawn = 0
        if Progress.active is self:
            Progress.active = None

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.abort()

    # --- ログの表示との調整（logging_config から呼ぶ） ---
    def clear(self) -> None:
        """上書き中の行を消す（その後に別の行を表示するため）。"""
        if not self.per_file and self._drawn:
            print("\r" + " " * self._drawn + "\r", end="", flush=True)
            self._drawn = 0

    def redraw(self) -> None:
        if not self.per_file and self.current:
            self._draw()

    # --- 内部 ---
    def _percent(self) -> int:
        return int(self.current * 100 / self.total) if self.total else 100

    def _per_file_line(self, name: str, status: str) -> str:
        width = len(str(self.total))
        return (f"  [{self.current:>{width}}/{self.total}] {self._percent():3d}%  "
                f"{_pad(name, self.name_width)}  {_pad(self.label, LABEL_WIDTH)}  {status}")

    def _remaining(self) -> str:
        elapsed = time.monotonic() - self.started
        if self.current == 0 or self.current >= self.total or elapsed < 1:
            return ""
        return f"残り 約{format_duration(elapsed / self.current * (self.total - self.current))}"

    def _draw(self, final: str = "", final_style: str = "success") -> None:
        filled = int(BAR_WIDTH * self._percent() / 100)
        bar = "#" * filled + "-" * (BAR_WIDTH - filled)
        width = len(str(self.total))
        head = f"  {_pad(self.label, LABEL_WIDTH)}  "
        parts = [f"{head}[{bar}] {self._percent():3d}%  {self.current:>{width}}/{self.total}"]
        if final:
            parts.append(final)
        else:
            remaining = self._remaining()
            if remaining:
                parts.append(remaining)
            parts.append(self._last_name)
        # 幅は色を付ける前の文字で合わせ、はみ出す分を切ってから色を付ける
        line = _fit("  ".join(parts), _terminal_width() - 1)
        shown = _pad(line, self._drawn)
        if color_enabled():
            shown = self._colorize(shown, head, bar, final, final_style)
        print("\r" + shown, end="", flush=True)
        self._drawn = max(display_width(line), self._drawn)

    @staticmethod
    def _colorize(line: str, head: str, bar: str, final: str, final_style: str) -> str:
        """進み具合の行に色を付ける。バーは実行中はシアン、終わったら完了・中断の色。"""
        bar_text = f"[{bar}]"
        if line.startswith(head + bar_text):
            line = head + paint(bar_text, final_style if final else "bar") + line[len(head + bar_text):]
        if final and final in line:
            line = line.replace(final, paint(final, final_style), 1)
        return line


def _terminal_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _print_line(text: str) -> None:
    """上書き中の進み具合の行があれば消してから1行表示する。"""
    active = Progress.active
    if active is not None:
        active.clear()
    print(text, flush=True)
    if active is not None:
        active.redraw()
