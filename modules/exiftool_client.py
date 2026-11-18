"""exiftool呼び出しを一元化する共通クライアントモジュール。

プロジェクト内で分散していた exiftool の呼び出す方式（パス解決、
subprocess/pyexiftool の使い分け、タイムアウト、Windows でのコンソール
非表示設定など）を本モジュールに集約する。他のモジュールは生の
subprocess.run(["exiftool", ...]) を直接書かず、必ず本クライアント
経由で exiftool を呼び出すこと。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SHORT = 10   # 単発の軽い問い合わせ（GPS取得、書き込みなど）
DEFAULT_TIMEOUT_MEDIUM = 30  # フォルダ単位の一括読み取り
DEFAULT_TIMEOUT_LONG = 1800  # 再帰的な一括抽出など重い処理

# -progress 指定時に exiftool が出力する進捗行（例: "======== C:/.../DSC_0001.NEF [12/340]"）
PATTERN_PROGRESS = re.compile(r"^======== (.+) \[(\d+)/(\d+)\]$")


def get_creation_flags() -> int:
    """Windows環境でサブプロセス実行時にコンソール画面を出さないフラグを返す。"""
    # Windows("nt")のときだけ CREATE_NO_WINDOW を付与し、黒いコンソール窓が
    # ポップアップするのを防ぐ。それ以外のOS（mac/Linux）では0（フラグなし）を返す。
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# Windows のコマンドライン長の上限（32,767文字）に対して余裕を持たせた1回あたりの上限
MAX_CMDLINE_CHARS = 24000


def path_key(path: Any) -> str:
    """パス表記の違い（/ と \\、大文字小文字）を吸収した比較用キーを返す。

    exiftool が返す SourceFile と、呼び出し側で組み立てたパスを突き合わせるときは
    必ずこの関数でキーを作る（モジュールごとに正規化方法が違うと一致しなくなるため）。
    """
    return os.path.normcase(os.path.normpath(str(path).replace("\\", "/")))


# 互換用の旧名
_path_key = path_key


def _chunk_by_cmdline_length(items: List[str], fixed_args: List[str], limit: int = MAX_CMDLINE_CHARS) -> List[List[str]]:
    """コマンドラインが limit 文字を超えないよう、引数リストを複数のまとまりに分ける。"""
    # 引数1つあたり、区切りの空白と引用符の分として +3 文字を見込む
    base = sum(len(a) + 3 for a in fixed_args)
    chunks: List[List[str]] = []
    current: List[str] = []
    length = base
    for item in items:
        size = len(item) + 3
        if current and length + size > limit:
            chunks.append(current)
            current, length = [], base
        current.append(item)
        length += size
    if current:
        chunks.append(current)
    return chunks


class ExifToolError(Exception):
    """exiftool呼び出しに関するエラー。"""


class ExifToolClient:
    """exiftoolへのアクセスを一元化するクライアント。

    軽量な単発コマンドは subprocess.run で都度実行し、大量ファイルに対する
    連続問い合わせ（GUIでのサムネイル走査など）は pyexiftool の永続プロセス
    (`with ExifToolClient(...) as et:`) を使うことでプロセス起動コストを削減する。
    """

    def __init__(self, exiftool_path: Optional[str] = None, config: Any = None):
        """初期化。

        Args:
            exiftool_path: exiftool実行ファイルの明示パス。指定があれば最優先。
            config: ConfigManager 等、get_tool_path("exiftool") を持つ設定オブジェクト。
        """
        self.path = exiftool_path or self._resolve_path(config)
        self._helper = None  # pyexiftool の永続プロセス（with文で使用時のみ）
        logger.info("ExifToolClient 初期化: path=%s", self.path)

    @staticmethod
    def _resolve_path(config: Any) -> str:
        """exiftoolパスを解決する。

        優先順位: config の external_tools.exiftool → PATH上の exiftool.exe/exiftool
        → 最終フォールバックとして "exiftool"（PATHに任せる）。
        """
        if config is not None:
            try:
                return str(config.get_tool_path("exiftool"))
            except (KeyError, AttributeError):
                logger.debug("configからexiftoolパスを取得できず。自動検索にフォールバック")

        found = shutil.which("exiftool.exe") or shutil.which("exiftool")
        if found:
            return found

        logger.warning("exiftoolが見つかりません。'exiftool' としてPATH解決に委ねます")
        return "exiftool"

    # ------------------------------------------------------------------
    # 永続プロセス（pyexiftool）を使う場合のコンテキストマネージャ
    # ------------------------------------------------------------------
    def __enter__(self) -> "ExifToolClient":
        # pyexiftoolパッケージは必須依存ではないため、withブロックに入る時点で
        # 初めてimportを試みる（未インストール環境でも単発呼び出し系メソッドは使える）
        try:
            import exiftool
        except ImportError as e:
            raise ExifToolError(
                "exiftool パッケージが未インストールです。pip install PyExifTool を実行してください"
            ) from e

        self._helper = exiftool.ExifToolHelper(executable=self.path)
        self._helper.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._helper is not None:
            self._helper.__exit__(exc_type, exc_val, exc_tb)
            self._helper = None

    # ------------------------------------------------------------------
    # 内部共通ヘルパー
    # ------------------------------------------------------------------
    def _launch_error(self, cmd: List[str], error: OSError) -> "ExifToolError":
        """exiftool の起動失敗（FileNotFoundError）を、原因が分かるメッセージに変換する。

        Windows ではコマンドラインが長すぎる場合（WinError 206）も FileNotFoundError に
        なるため、実行ファイルが実在するかで原因を切り分ける。
        """
        exists = os.path.isfile(self.path) or shutil.which(self.path) is not None
        if not exists:
            return ExifToolError(
                f"exiftoolが見つかりません（path={self.path}）。PATHに追加するかconfigを確認してください"
            )
        length = len(subprocess.list2cmdline(cmd))
        return ExifToolError(
            f"exiftoolを起動できませんでした（path={self.path}, コマンド長={length}文字。"
            f"Windowsのコマンドライン長の上限を超えた可能性があります）: {error}"
        )

    def _run(
        self,
        args: List[str],
        timeout: int = DEFAULT_TIMEOUT_SHORT,
        capture_binary: bool = False,
    ) -> subprocess.CompletedProcess:
        """exiftoolをsubprocessで実行する内部共通処理。

        Args:
            args: exiftoolに渡す引数（実行ファイルパスに含まない）
            timeout: タイムアウト秒数
            capture_binary: True の場合 stdout をバイナリのまま取得（プレビュー画像抽出用）
        """
        cmd = [self.path] + args
        try:
            if capture_binary:
                # バイナリ抽出用: text=Trueを指定せず、stdoutをbytesのまま受け取る
                # （JPEGプレビュー画像などをテキストとしてデコードすると破損するため）
                return subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=get_creation_flags(),
                    timeout=timeout,
                    check=False,  # 非ゼロ終了でも例外にせず、呼び出し元でreturncodeを見て判定させる
                )
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=get_creation_flags(),
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise self._launch_error(cmd, e) from e
        except subprocess.TimeoutExpired as e:
            raise ExifToolError(f"exiftoolの実行がタイムアウトしました（timeout={timeout}s）") from e

    # ------------------------------------------------------------------
    # 読み取り系API
    # ------------------------------------------------------------------
    def get_metadata_batch(self, file_paths: List[Path]) -> List[Dict[str, Any]]:
        """複数ファイルのメタデータを一括取得（pyexiftool永続プロセス使用）。

        with文の中でのみ使用可能。
        """
        if not file_paths:
            return []
        if self._helper is None:
            raise ExifToolError("get_metadata_batch は with ExifToolClient(...) as et: の中で使用してください")

        return self._helper.get_metadata([str(p) for p in file_paths])

    def get_dates_batch(self, file_paths: List[Path], date_format: str = "%Y%m%d") -> Dict[Path, str]:
        """複数ファイルの撮影日を一括取得（単発subprocess）。

        Returns:
            {ファイルパス: 日付文字列}。撮影日（DateTimeOriginal / CreateDate）を
            取得できなかったファイルは含まれない。

        Windows はコマンドライン全体の長さが約32,000文字までのため、ファイルが多い場合は
        複数回に分けて exiftool を呼び出す（以前は全ファイルを1回で渡していたため、
        RAW が千枚程度を超えると起動に失敗し、全ファイルが更新日時にフォールバックしていた）。
        """
        if not file_paths:
            return {}

        date_map: Dict[Path, str] = {}
        # exiftool の SourceFile 表記（区切り文字・大文字小文字）の違いを吸収するための逆引き表
        by_key: Dict[str, Path] = {_path_key(p): p for p in file_paths}

        # -fast2: MakerNotes の解析を省いて速くする（撮影日時は標準 EXIF にあるため影響しない）
        base_args = ["-j", "-fast2", "-DateTimeOriginal", "-CreateDate", "-d", date_format]
        chunks = _chunk_by_cmdline_length([str(p) for p in file_paths], [self.path] + base_args)
        if len(chunks) > 1:
            logger.info("撮影日の一括取得を %d 回に分けて実行します（%d ファイル）", len(chunks), len(file_paths))

        for chunk in chunks:
            try:
                result = self._run(base_args + chunk, timeout=DEFAULT_TIMEOUT_LONG)
                data = json.loads(result.stdout) if result.stdout else []
                for item in data:
                    source_path = by_key.get(_path_key(item.get("SourceFile", "")))
                    if source_path is None:
                        continue
                    date_str = item.get("DateTimeOriginal") or item.get("CreateDate")
                    if date_str and len(str(date_str)) == 8 and str(date_str).isdigit():
                        date_map[source_path] = str(date_str)
            except (ExifToolError, json.JSONDecodeError) as e:
                logger.error("日付一括取得エラー: %s", e)

        # 撮影日を取得できなかったファイルは戻り値に含めない（呼び出し側で「日付不明」として扱う）。
        # ※以前はファイルの更新日時で代用していたが、更新日時は exiftool で GPS を書き込んだ日や
        #   コピーした日に変わることがあり、撮影日と無関係な日付フォルダへ振り分けられていた
        missing = [p for p in file_paths if p not in date_map]
        if missing:
            # 画面には呼び出し側（写真整理・コピー）が写真ごとに表示するため、ここではログファイルにだけ残す
            logger.info("撮影日を取得できなかったファイル: %d 件", len(missing))
            for path in missing:
                logger.info("撮影日取得失敗: %s", path.name)

        return date_map

    def get_gps(self, file_path: Path) -> Optional[Tuple[float, float]]:
        """単一ファイルのGPS座標を取得。"""
        # -n: 数値として出力（度分秒ではなく10進数の緯度経度を得るため）
        args = ["-j", "-n", "-GPSLatitude", "-GPSLongitude", str(file_path)]
        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_SHORT)
            data = json.loads(result.stdout) if result.stdout else []
            if data:
                lat = data[0].get("GPSLatitude")
                lng = data[0].get("GPSLongitude")
                if lat is not None and lng is not None:
                    return (float(lat), float(lng))
        except (ExifToolError, json.JSONDecodeError, ValueError) as e:
            # GPS情報が無い/壊れている等は珍しくないため、エラーではなくdebugログに留める
            logger.debug("GPS取得失敗 (%s): %s", file_path.name, e)
        return None

    def get_gps_orientation_batch(
        self,
        directory: Path,
        extensions: Optional[List[str]] = None,
        timeout: int = DEFAULT_TIMEOUT_LONG,
    ) -> Dict[str, Dict[str, Any]]:
        """ディレクトリ内ファイルのGPS有無・Orientationを一括取得（GUI用）。

        戻り値のキーは「ファイル名を小文字化したもの」（例: "dsc_0001.nef"）。
        exiftool が返す SourceFile のパス表記（区切り文字・大文字小文字など）は
        環境によって呼び出し側の組み立てたパスと一致しないことがあるため、
        対象を単一ディレクトリ（非再帰）に限定したうえでファイル名で突き合わせる。

        Args:
            directory: 対象ディレクトリ（サブフォルダは走査しない）
            extensions: 対象拡張子（例: ["nef", "jpg"]）。Noneなら全ファイル
            timeout: タイムアウト秒数。写真が多いフォルダでも途中で打ち切られないよう長めにとる
        """
        # -fast2: MakerNotes（機種固有の大きなタグ群）の解析を省略して高速化する。
        #         GPS は GPS IFD、Orientation は IFD0 にあるため影響しない。
        args = ["-j", "-n", "-fast2"]
        for ext in extensions or []:
            args += ["-ext", ext.lstrip(".")]
        args += ["-Orientation", "-GPSLatitude", "-GPSLongitude", str(directory)]
        meta_dict: Dict[str, Dict[str, Any]] = {}
        try:
            result = self._run(args, timeout=timeout)
            if result.stdout:
                data = json.loads(result.stdout)
                for item in data:
                    name = os.path.basename(str(item.get("SourceFile", "")).replace("\\", "/")).lower()
                    if not name:
                        continue
                    meta_dict[name] = self._to_gps_orientation(item)
            logger.info("GPS/Orientation一括取得: %d 件 (%s)", len(meta_dict), directory)
        except (ExifToolError, json.JSONDecodeError) as e:
            # 失敗しても空の辞書を返し、呼び出し側でファイル単位の再取得にフォールバックさせる
            logger.error("GPS/Orientation一括取得エラー: %s", e)
        return meta_dict

    def get_gps_orientation(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """単一ファイルのGPS・Orientationを取得。一括取得で漏れたファイルの再取得用。

        Returns:
            {"orientation", "lat", "lng"} の辞書。exiftool自体が失敗した場合は None
        """
        args = ["-j", "-n", "-fast2", "-Orientation", "-GPSLatitude", "-GPSLongitude", str(file_path)]
        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_SHORT)
            data = json.loads(result.stdout) if result.stdout else []
            if data:
                return self._to_gps_orientation(data[0])
        except (ExifToolError, json.JSONDecodeError) as e:
            logger.warning("GPS/Orientation取得失敗 (%s): %s", file_path.name, e)
        return None

    @staticmethod
    def _to_gps_orientation(item: Dict[str, Any]) -> Dict[str, Any]:
        """exiftoolのJSON 1件分を {"orientation", "lat", "lng"} に整形する。"""
        def _num(v: Any) -> Optional[float]:
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        orientation = item.get("Orientation", 1)
        try:
            orientation = int(orientation)
        except (TypeError, ValueError):
            orientation = 1  # 未取得・不正値は「回転なし」とみなす
        return {
            "orientation": orientation,
            "lat": _num(item.get("GPSLatitude")),
            "lng": _num(item.get("GPSLongitude")),
        }

    def get_binary_tag(self, file_path: Path, tag: str, timeout: int = DEFAULT_TIMEOUT_SHORT) -> Optional[bytes]:
        """バイナリタグ（PreviewImage、JpgFromRaw等）を抽出。"""
        # -b: バイナリ形式で出力させる。 -{tag}: 抽出したいタグ名（例: PreviewImage）
        args = ["-b", f"-{tag}", str(file_path)]
        try:
            result = self._run(args, timeout=timeout, capture_binary=True)
            if result.stdout:
                return result.stdout
        except ExifToolError as e:
            logger.debug("バイナリタグ抽出失敗 (%s, %s): %s", file_path.name, tag, e)
        return None

    def get_tags_text(self, file_path: Path, tags: List[str]) -> str:
        """指定タグを '-S' 形式のテキストで取得（EXIF表示パネル用）。"""
        # -S: "タグ名: 値" の短い一行形式で出力するオプション。タグ名は引数ごとに -タグ名 で指定
        args = ["-S"] + [f"-{t}" for t in tags] + [str(file_path)]
        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_SHORT)
            return result.stdout or ""
        except ExifToolError as e:
            logger.error("タグ取得失敗 (%s): %s", file_path.name, e)
            return ""

    def run_raw(self, args: List[str], timeout: int = DEFAULT_TIMEOUT_SHORT) -> str:
        """任意のexiftool引数を実行してテキスト出力を返す（他メソッドで表現しにくい特殊用途向け）。

        args にはファイルパスも含める。極力 get_metadata_batch 等の専用メソッドを
        優先し、これは最後の手段として使うこと。
        """
        try:
            result = self._run(args, timeout=timeout)
            return result.stdout or ""
        except ExifToolError as e:
            logger.error("run_raw エラー: %s", e)
            return ""

    def extract_recursive(
        self,
        directory: Path,
        extensions: List[str],
        group_names: bool = True,
        exclude_dirs: Optional[List[str]] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """ディレクトリを再帰検索してメタデータを一括取得（exif_exporter用）。

        Args:
            extensions: 対象拡張子（"jpg" / ".jpg" どちらでも可）
            exclude_dirs: 走査しないサブフォルダ名（exiftool の -i。例: フレーム付き画像の出力フォルダ）
            on_progress: 1ファイル読むごとに呼ぶ通知先 (何件目, 全体の件数, ファイルのパス)
        """
        args = ["-r"]  # サブフォルダも読む
        for name in exclude_dirs or []:
            args += ["-i", name]
        for ext in extensions:
            args += ["-ext", ext.lstrip(".")]
        args += ["-j"]
        if group_names:
            # -G1: タグ名の前にグループ名（EXIF:, MakerNotes: 等）を付けて出力する
            args += ["-G1"]
        # resolve() で絶対パスに変換してから渡す（相対パス指定時の曖昧さを避けるため）
        args += [str(directory.resolve())]

        try:
            if on_progress is None:
                stdout = self._run(args, timeout=DEFAULT_TIMEOUT_LONG).stdout
            else:
                # -progress の行は、JSON（標準出力）とは別に標準エラーへ出る
                stdout = self._run_json_with_progress(["-progress"] + args, DEFAULT_TIMEOUT_LONG, on_progress)
            if stdout:
                return json.loads(stdout)
        except (ExifToolError, json.JSONDecodeError) as e:
            logger.error("再帰的メタデータ抽出エラー: %s", e)
        return []

    def get_gps_and_date_batch(self, directory: Path, extensions: Optional[List[str]] = None,
                               timeout: int = DEFAULT_TIMEOUT_LONG) -> Dict[str, Dict[str, bool]]:
        """フォルダ直下の写真に、位置情報と撮影日時（DateTimeOriginal）があるかをまとめて調べる。

        Returns:
            {小文字のファイル名: {"has_gps": bool, "has_date": bool}}。exiftool が失敗したら空

        Raises:
            ExifToolError: exiftool を起動できなかった場合
        """
        args = ["-j", "-n", "-fast2"]
        for ext in extensions or []:
            args += ["-ext", ext.lstrip(".")]
        args += ["-GPSLatitude", "-DateTimeOriginal", str(directory)]
        result = self._run(args, timeout=timeout)
        info: Dict[str, Dict[str, bool]] = {}
        try:
            for item in json.loads(result.stdout) if result.stdout else []:
                name = os.path.basename(str(item.get("SourceFile", "")).replace("\\", "/")).lower()
                if name:
                    info[name] = {
                        "has_gps": item.get("GPSLatitude") not in (None, ""),
                        "has_date": item.get("DateTimeOriginal") not in (None, ""),
                    }
        except json.JSONDecodeError as e:
            logger.error("位置情報・撮影日時の一括取得エラー: %s", e)
        return info

    # ------------------------------------------------------------------
    # 書き込み系API
    # ------------------------------------------------------------------
    def write_gps(self, file_path: Path, lat: float, lng: float, timeout: int = DEFAULT_TIMEOUT_SHORT) -> bool:
        """単一ファイルにGPS座標を書き込む。"""
        args = [
            f"-GPSLatitude={lat}",
            f"-GPSLongitude={lng}",
            # 緯度が正など北緯(N)、負など南緯(S)。経度が正など東経(E)、負など西経(W)を明示するRefタグ
            "-GPSLatitudeRef=N" if lat >= 0 else "-GPSLatitudeRef=S",
            "-GPSLongitudeRef=E" if lng >= 0 else "-GPSLongitudeRef=W",
            # 元ファイルへ直接上書き（バックアップ(_original)ファイルを作らない）
            "-overwrite_original",
            # -P: ファイルの更新日時を書き込み前のまま保つ（書き込んだ日に変わらないようにする）
            "-P",
            str(file_path),
        ]
        try:
            result = self._run(args, timeout=timeout)
            if result.returncode == 0:
                return True
            logger.warning("GPS書き込み失敗: %s (exitcode=%d)", file_path.name, result.returncode)
        except ExifToolError as e:
            logger.error("GPS書き込みエラー (%s): %s", file_path.name, e)
        return False

    def geotag_from_gpx(
        self,
        target_dir: Optional[Path],
        gpx_files: List[Path],
        geosync: Optional[str] = None,
        extensions: Optional[List[str]] = None,
        skip_existing: bool = True,
        max_interpolation_secs: Optional[int] = None,
        max_extrapolation_secs: Optional[int] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        timeout: int = DEFAULT_TIMEOUT_LONG,
        files: Optional[List[Path]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> str:
        """GPXファイル群を元に、対象ディレクトリ（または files の写真）へジオタグを一括付与する。

        Args:
            geosync: カメラ時計のずれ補正値（"GPS時刻 − カメラ時刻"、例 "+00:01:30"）。
                None/空なら -geosync を付けない。タイムゾーン補正には使わないこと
                （exiftool は "+09:00" を「+9分」と解釈する）。
            extensions: 対象とする拡張子（例: ["jpg", "nef"]）。Noneならフォルダ内の全ファイル
            skip_existing: True なら既にGPS情報を持つ写真は書き換えない
                （スマホ連携や手動付与した位置を保護する）
            max_interpolation_secs: GPXの記録点の間隔がこの秒数を超える区間では補間しない
                （exiftool の GeoMaxIntSecs。None なら exiftool の既定値 1800秒）
            max_extrapolation_secs: GPXの記録の前後この秒数までは端点の位置を使う
                （exiftool の GeoMaxExtSecs。None なら exiftool の既定値 1800秒）
            on_progress: 進捗通知コールバック (現在の件数, 全体件数, ファイルパス)
            timeout: タイムアウト秒数
            files: 対象の写真を1枚ずつ指定する場合の一覧。指定すると target_dir と extensions は使わない
            on_line: exiftool の出力を1行ずつ受け取る通知先（写真ごとの結果を表示するため）

        Returns:
            exiftool の出力（標準出力と標準エラーを出力順のまま結合したもの）。
            件数集計は呼び出し側で行う。
        """
        # -progress: 1ファイル処理するごとに "======== ファイル [N/全体]" を出力させる（進捗表示用）
        args = ["-progress"]
        args += [f"-geotag={gpx}" for gpx in gpx_files]
        if geosync:
            args.append(f"-geosync={geosync}")
        if max_interpolation_secs is not None:
            args += ["-api", f"GeoMaxIntSecs={int(max_interpolation_secs)}"]
        if max_extrapolation_secs is not None:
            args += ["-api", f"GeoMaxExtSecs={int(max_extrapolation_secs)}"]
        # -overwrite_original: 元ファイルへ直接上書き
        # -P: ファイルの更新日時を書き込み前のまま保つ（書き込んだ日に変わらないようにする）
        args += ["-overwrite_original", "-P"]
        if skip_existing:
            # 既にGPS緯度を持つファイルは条件不一致として書き込み対象から外す
            # （exiftool は "N files failed condition" として件数を報告する）
            args += ["-if", "not $GPSLatitude"]
        if files is not None:
            # 写真の一覧はファイル（-@）で渡す（何百枚あってもコマンドラインの長さの上限を超えないように）
            with _argfile(files) as path:
                return self._run_streaming(args + ["-charset", "filename=utf8", "-@", path],
                                           timeout=timeout, on_progress=on_progress, on_line=on_line)
        for ext in extensions or []:
            # -ext: 指定した拡張子のファイルだけを対象にする（動画など分類対象外のファイルに触れない）
            args += ["-ext", ext.lstrip(".")]
        return self._run_streaming(args + [str(target_dir)], timeout=timeout,
                                   on_progress=on_progress, on_line=on_line)

    def _start_timeout(self, proc: subprocess.Popen, timeout: int) -> Tuple[threading.Timer, threading.Event]:
        """timeout 秒たっても終わらなければ exiftool を止めるタイマー。

        出力を1行ずつ読んでいる間は proc.wait(timeout) が使えないため、タイマーで打ち切る。
        """
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            if os.name == "nt":
                # Windows 版 exiftool.exe は内部で perl を子プロセスとして起動するため、
                # 親だけ止めると出力パイプが閉じずに読み取りが終わらない。プロセスツリーごと終了させる
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=get_creation_flags(),
                    check=False,
                )
            else:
                proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.start()
        return timer, timed_out

    def _popen(self, cmd: List[str], merge_stderr: bool) -> subprocess.Popen:
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=get_creation_flags(),
            )
        except FileNotFoundError as e:
            raise self._launch_error(cmd, e) from e

    @staticmethod
    def _notify_progress(line: str, on_progress: Optional[Callable[[int, int, str], None]]) -> None:
        if on_progress is None:
            return
        m = PATTERN_PROGRESS.match(line)
        if m:
            try:
                on_progress(int(m.group(2)), int(m.group(3)), m.group(1))
            except Exception as e:  # 表示側の不具合で処理全体を止めない
                logger.debug("進捗通知エラー: %s", e)

    def _run_streaming(
        self,
        args: List[str],
        timeout: int,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> str:
        """exiftoolを実行し、出力を1行ずつ読みながら進捗を通知する。

        標準エラーは標準出力にまとめて受け取る（警告と対象ファイルの対応が出力順で
        分かるようにするため、また2本のパイプを同時に読む必要をなくすため）。
        """
        proc = self._popen([self.path] + args, merge_stderr=True)
        timer, timed_out = self._start_timeout(proc, timeout)
        lines: List[str] = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                lines.append(line)
                self._notify_progress(line, on_progress)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception as e:  # 表示側の不具合で処理全体を止めない
                        logger.debug("出力行の通知エラー: %s", e)
            proc.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise ExifToolError(f"exiftoolの実行がタイムアウトしました（timeout={timeout}s）")
        return "\n".join(lines)

    def _run_json_with_progress(self, args: List[str], timeout: int,
                                on_progress: Callable[[int, int, str], None]) -> str:
        """-j（JSON）と -progress を一緒に使って実行する。JSON を返し、進み具合は1件ずつ通知する。

        JSON は標準出力、進み具合の行は標準エラーに出るため、標準出力は別のスレッドで読む。
        """
        proc = self._popen([self.path] + args, merge_stderr=False)
        timer, timed_out = self._start_timeout(proc, timeout)
        chunks: List[str] = []
        reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
        reader.start()
        try:
            assert proc.stderr is not None
            for raw in proc.stderr:
                line = raw.rstrip("\r\n")
                self._notify_progress(line, on_progress)
                if line and not PATTERN_PROGRESS.match(line):
                    logger.info("exiftool: %s", line)
            reader.join()
            proc.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise ExifToolError(f"exiftoolの実行がタイムアウトしました（timeout={timeout}s）")
        return "".join(chunks)


@contextmanager
def _argfile(paths: List[Path]) -> Iterator[str]:
    """exiftool の -@ に渡す、1行に1つずつパスを書いた一時ファイル（UTF-8）。使い終わったら消す。"""
    fd, name = tempfile.mkstemp(prefix="photoworkflow_", suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for p in paths:
                f.write(f"{p}\n")
        yield name
    finally:
        try:
            os.remove(name)
        except OSError:
            pass
