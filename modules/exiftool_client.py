"""exiftool呼び出しを一元化する共通クライアントモジュール。

プロジェクト内で分散していた exiftool の呼び出す方式（パス解決、
subprocess/pyexiftool の使い分け、タイムアウト、Windows でのコンソール
非表示設定など）を本モジュールに集約する。他のモジュールは生の
subprocess.run(["exiftool", ...]) を直接書かず、必ず本クライアント
経由で exiftool を呼び出すこと。
"""

from __future__ import annotations  # 型ヒントの前方参照（文字列化）を有効にする。戻り値型に自クラス名を使うために必要

import json  # exiftoolの "-j" オプションによるJSON形式の出力をパースするために使用
import logging  # 動作ログ（デバッグ・警告・エラー）の出力
import os  # OS判定（Windowsかどうか）やパス正規化に使用
import shutil  # shutil.which() でPATH上の実行ファイルを検索するために使用
import subprocess  # exiftoolを外部プロセスとして呼び出すための標準モジュール
from datetime import datetime  # 撮影日が取得できない場合のフォールバック（更新日時）変換に使用
from pathlib import Path  # ファイルパスをOS非依存に扱うため
from typing import Any, Dict, List, Optional, Tuple  # 型ヒント用

logger = logging.getLogger(__name__)  # モジュール単位のロガー。呼び出し元のロギング設定に従って出力される

# サブプロセスのデフォルトタイムアウト（秒）
DEFAULT_TIMEOUT_SHORT = 10   # 単発の軽い問い合わせ（GPS取得、書き込みなど）
DEFAULT_TIMEOUT_MEDIUM = 30  # フォルダ単位の一括読み取り
DEFAULT_TIMEOUT_LONG = 1800  # 再帰的な一括抽出など重い処理


def get_creation_flags() -> int:
    """Windows環境でサブプロセス実行時にコンソール画面を出さないフラグを返す。"""
    # Windows("nt")のときだけ CREATE_NO_WINDOW を付与し、黒いコンソール窓が
    # ポップアップするのを防ぐ。それ以外のOS（mac/Linux）では0（フラグなし）を返す。
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class ExifToolError(Exception):
    """exiftool呼び出しに関するエラー。"""
    # exiftoolの実行失敗・タイムアウト・未インストールなど、本クライアント内で
    # 発生しうる異常を一つの例外型にまとめ、呼び出し側でまとめてcatchできるようにする。


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
        # 明示的なパス指定があればそれを使い、なければ config やPATHから解決する
        self.path = exiftool_path or self._resolve_path(config)
        self._helper = None  # pyexiftool の永続プロセス（with文で使用時のみ）
        logger.info("ExifToolClient 初期化: path=%s", self.path)

    @staticmethod
    def _resolve_path(config: Any) -> str:
        """exiftoolパスを解決する。

        優先順位: config の external_tools.exiftool → PATH上の exiftool.exe/exiftool
        → 最終フォールバックとして "exiftool"（PATHに任せる）。
        """
        # 1. configオブジェクト（ConfigManager等）が渡されていれば、そこに設定された
        #    外部ツールパス（external_tools.exiftool）を最優先で使用する
        if config is not None:
            try:
                return str(config.get_tool_path("exiftool"))
            except (KeyError, AttributeError):
                # configにキーが無い／get_tool_pathメソッドを持たない場合は
                # 例外を握りつぶして自動検索へフォールバックする
                logger.debug("configからexiftoolパスを取得できず。自動検索にフォールバック")

        # 2. PATH環境変数上から exiftool.exe（Windows用実行ファイル）または
        #    exiftool（Unix系）を検索する
        found = shutil.which("exiftool.exe") or shutil.which("exiftool")
        if found:
            return found

        # 3. どちらでも見つからなければ警告を出し、"exiftool" という文字列だけを返して
        #    実行時のPATH解決（OSのシェル的な検索）に委ねる
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

        # pyexiftoolのヘルパーを生成し、その永続プロセス自体のコンテキストにも入る
        self._helper = exiftool.ExifToolHelper(executable=self.path)
        self._helper.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # with文を抜ける際に、開いていた永続プロセスのコンテキストを終了し
        # プロセスを確実に終了させる。既に None（未使用）などの場合は何もしない。
        if self._helper is not None:
            self._helper.__exit__(exc_type, exc_val, exc_tb)
            self._helper = None

    # ------------------------------------------------------------------
    # 内部共通ヘルパー
    # ------------------------------------------------------------------
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
        # 実行ファイルパスと引数を結合して実際のコマンドラインを組み立てる
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
            # 通常のテキスト出力用: UTF-8でデコードし、デコードできない文字は置換する
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
            # exiftool実行ファイル自体が見つからない場合（パス誤り・未インストール）
            raise ExifToolError(
                f"exiftoolが見つかりません（path={self.path}）。PATHに追加するかconfigを確認してください"
            ) from e
        except subprocess.TimeoutExpired as e:
            # 指定タイムアウト内にexiftoolが終了しなかった場合
            raise ExifToolError(f"exiftoolの実行がタイムアウトしました（timeout={timeout}s）") from e

    # ------------------------------------------------------------------
    # 読み取り系API
    # ------------------------------------------------------------------
    def get_metadata_batch(self, file_paths: List[Path]) -> List[Dict[str, Any]]:
        """複数ファイルのメタデータを一括取得（pyexiftool永続プロセス使用）。

        with文の中でのみ使用可能。
        """
        # 空リストなど問い合わせ自体を省略して早期リターン
        if not file_paths:
            return []
        # 永続プロセス（_helper）が起動していない＝with文の外で呼ばれた場合はエラー
        if self._helper is None:
            raise ExifToolError("get_metadata_batch は with ExifToolClient(...) as et: の中で使用してください")

        # pyexiftoolの get_metadata に全パスを渡し、1回のプロセス通信で
        # まとめてメタデータ（辞書のリスト）を取得する
        return self._helper.get_metadata([str(p) for p in file_paths])

    def get_dates_batch(self, file_paths: List[Path], date_format: str = "%Y%m%d") -> Dict[Path, str]:
        """複数ファイルの撮影日を一括取得（単発subprocess、フォールバックあり）。"""
        if not file_paths:
            return {}

        # 最終的に返す「ファイルパス → 日付文字列」の対応表
        date_map: Dict[Path, str] = {}
        # -j: JSON出力, -DateTimeOriginal/-CreateDate: 撮影日時系タグ、
        # -d <format>: 日付の出力書式を指定し、対象ファイル群をまとめて渡す
        args = [
            "-j",
            "-DateTimeOriginal",
            "-CreateDate",
            "-d",
            date_format,
        ] + [str(p) for p in file_paths]

        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_MEDIUM)
            # 出力が空でなければJSONとしてパース。空なら空リスト扱いにする
            data = json.loads(result.stdout) if result.stdout else []
            for item in data:
                source_path = Path(item.get("SourceFile"))
                # DateTimeOriginal（撮影日時）を優先し、無ければCreateDate（作成日時）で代替
                date_str = item.get("DateTimeOriginal") or item.get("CreateDate")
                # 指定フォーマット（例: "%Y%m%d"）で8桁の数字になっているものだけを正常値として採用し、
                # 変換に失敗した曖昧な値（不明日付など）は除外する
                if date_str and len(str(date_str)) == 8 and str(date_str).isdigit():
                    date_map[source_path] = str(date_str)
        except (ExifToolError, json.JSONDecodeError) as e:
            # exiftool実行失敗やJSON崩れが起きても処理全体を止めず、エラーログを出して続行する
            logger.error("日付一括取得エラー: %s", e)

        # 取得できなかったファイルは更新日時(mtime)をフォールバック
        for path in file_paths:
            if path not in date_map:
                mtime = path.stat().st_mtime
                date_map[path] = datetime.fromtimestamp(mtime).strftime(date_format)
                logger.warning("撮影日取得失敗のため更新日時を使用: %s -> %s", path.name, date_map[path])

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
                # 緯度・経度の両方が取得できた場合のみタプルとして返す
                if lat is not None and lng is not None:
                    return (float(lat), float(lng))
        except (ExifToolError, json.JSONDecodeError, ValueError) as e:
            # GPS情報が無い/壊れている等は珍しくないため、エラーではなくdebugログに留める
            logger.debug("GPS取得失敗 (%s): %s", file_path.name, e)
        return None

    def get_gps_orientation_batch(self, directory: Path) -> Dict[str, Dict[str, Any]]:
        """ディレクトリ内ファイルのGPS有無・Orientationを一括取得（GUI用）。"""
        # ディレクトリを対象に、Orientation（回転情報）とGPS座標をまとめて取得する
        args = ["-j", "-n", "-Orientation", "-GPSLatitude", "-GPSLongitude", str(directory)]
        meta_dict: Dict[str, Dict[str, Any]] = {}
        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_MEDIUM)
            if result.stdout:
                data = json.loads(result.stdout)
                for item in data:
                    # OS間でのパス区切り文字の違いを吸収するため正規化しておく
                    path = os.path.normpath(item.get("SourceFile", ""))
                    meta_dict[path] = {
                        "orientation": item.get("Orientation", 1),  # 未取得時は1（回転なし）を既定値とする
                        "lat": item.get("GPSLatitude"),
                        "lng": item.get("GPSLongitude"),
                    }
        except (ExifToolError, json.JSONDecodeError) as e:
            logger.error("GPS/Orientation一括取得エラー: %s", e)
        return meta_dict

    def get_binary_tag(self, file_path: Path, tag: str, timeout: int = DEFAULT_TIMEOUT_SHORT) -> Optional[bytes]:
        """バイナリタグ（PreviewImage、JpgFromRaw等）を抽出。"""
        # -b: バイナリ形式で出力させる。 -{tag}: 抽出したいタグ名（例: PreviewImage）
        args = ["-b", f"-{tag}", str(file_path)]
        try:
            result = self._run(args, timeout=timeout, capture_binary=True)
            if result.stdout:
                # バイナリのままのstdout（bytes）をそのまま返す
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
    ) -> List[Dict[str, Any]]:
        """ディレクトリを再帰検索してメタデータを一括取得（exif_exporter用）。"""
        args = ["-r"]  # -r: サブディレクトリも再帰的に走査する
        # 対象とする拡張子ごとに -ext <拡張子> を追加し、それ以外のファイルは除外する
        for ext in extensions:
            args += ["-ext", ext]
        args += ["-j"]  # JSON形式で出力
        if group_names:
            # -G1: タグ名の前にグループ名（EXIF:, MakerNotes: 等）を付けて出力する
            args += ["-G1"]
        # resolve() で絶対パスに変換してから渡す（相対パス指定時の曖昧さを避けるため）
        args += [str(directory.resolve())]

        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_LONG)
            if result.stdout:
                return json.loads(result.stdout)
        except (ExifToolError, json.JSONDecodeError) as e:
            logger.error("再帰的メタデータ抽出エラー: %s", e)
        return []

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
            str(file_path),
        ]
        try:
            result = self._run(args, timeout=timeout)
            if result.returncode == 0:
                return True
            # 終了コードが非ゼロ＝書き込み失敗として警告ログを出しFalseを返す
            logger.warning("GPS書き込み失敗: %s (exitcode=%d)", file_path.name, result.returncode)
        except ExifToolError as e:
            logger.error("GPS書き込みエラー (%s): %s", file_path.name, e)
        return False

    def geotag_from_gpx(
        self,
        target_dir: Path,
        gpx_files: List[Path],
        geosync: str,
        timeout: int = DEFAULT_TIMEOUT_LONG,
    ) -> Tuple[str, str]:
        """GPXファイル群を元に対象ディレクトリへジオタグを一括付与する。

        Returns:
            (stdout, stderr) のタプル。件数集計は呼び出し側で行う。
        """
        # -geotag=<gpxファイル>: 各GPXトラックログを参照させる（複数指定可）
        args = [f"-geotag={gpx}" for gpx in gpx_files]
        # -geosync=<オフセット>: カメラ時計とGPS時刻のズレを補正する時刻同期設定
        # -overwrite_original: 元ファイルへ直接上書き。対象は個別ファイルではなくディレクトリ全体
        args += [f"-geosync={geosync}", "-overwrite_original", str(target_dir)]
        result = self._run(args, timeout=timeout)
        # 呼び出し元（GUI等）で成功件数・エラー件数の集計を行えるよう、
        # ここでは加工せずexiftoolの生の標準出力・標準エラーをそのまま返す
        return result.stdout or "", result.stderr or ""
