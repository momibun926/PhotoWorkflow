"""exiftool呼び出しを一元化する共通クライアントモジュール。

プロジェクト内で分散していた exiftool の呼び出し方式（パス解決、
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
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# サブプロセスのデフォルトタイムアウト（秒）
DEFAULT_TIMEOUT_SHORT = 10   # 単発の軽い問い合わせ（GPS取得、書き込みなど）
DEFAULT_TIMEOUT_MEDIUM = 30  # フォルダ単位の一括読み取り
DEFAULT_TIMEOUT_LONG = 1800  # 再帰的な一括抽出など重い処理


def get_creation_flags() -> int:
    """Windows環境でサブプロセス実行時にコンソール画面を出さないフラグを返す。"""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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
    def _run(
        self,
        args: List[str],
        timeout: int = DEFAULT_TIMEOUT_SHORT,
        capture_binary: bool = False,
    ) -> subprocess.CompletedProcess:
        """exiftoolをsubprocessで実行する内部共通処理。

        Args:
            args: exiftoolに渡す引数（実行ファイルパスは含まない）
            timeout: タイムアウト秒数
            capture_binary: True の場合 stdout をバイナリのまま取得（プレビュー画像抽出用）
        """
        cmd = [self.path] + args
        try:
            if capture_binary:
                return subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=get_creation_flags(),
                    timeout=timeout,
                    check=False,
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
            raise ExifToolError(
                f"exiftoolが見つかりません（path={self.path}）。PATHに追加するかconfigを確認してください"
            ) from e
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
        """複数ファイルの撮影日を一括取得（単発subprocess、フォールバックあり）。"""
        if not file_paths:
            return {}

        date_map: Dict[Path, str] = {}
        args = [
            "-j",
            "-DateTimeOriginal",
            "-CreateDate",
            "-d",
            date_format,
        ] + [str(p) for p in file_paths]

        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_MEDIUM)
            data = json.loads(result.stdout) if result.stdout else []
            for item in data:
                source_path = Path(item.get("SourceFile"))
                date_str = item.get("DateTimeOriginal") or item.get("CreateDate")
                if date_str and len(str(date_str)) == 8 and str(date_str).isdigit():
                    date_map[source_path] = str(date_str)
        except (ExifToolError, json.JSONDecodeError) as e:
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
            logger.debug("GPS取得失敗 (%s): %s", file_path.name, e)
        return None

    def get_gps_orientation_batch(self, directory: Path) -> Dict[str, Dict[str, Any]]:
        """ディレクトリ内ファイルのGPS有無・Orientationを一括取得（GUI用）。"""
        args = ["-j", "-n", "-Orientation", "-GPSLatitude", "-GPSLongitude", str(directory)]
        meta_dict: Dict[str, Dict[str, Any]] = {}
        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_MEDIUM)
            if result.stdout:
                data = json.loads(result.stdout)
                for item in data:
                    path = os.path.normpath(item.get("SourceFile", ""))
                    meta_dict[path] = {
                        "orientation": item.get("Orientation", 1),
                        "lat": item.get("GPSLatitude"),
                        "lng": item.get("GPSLongitude"),
                    }
        except (ExifToolError, json.JSONDecodeError) as e:
            logger.error("GPS/Orientation一括取得エラー: %s", e)
        return meta_dict

    def get_binary_tag(self, file_path: Path, tag: str, timeout: int = DEFAULT_TIMEOUT_SHORT) -> Optional[bytes]:
        """バイナリタグ（PreviewImage、JpgFromRaw等）を抽出。"""
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
        args = ["-S"] + [f"-{t}" for t in tags] + [str(file_path)]
        try:
            result = self._run(args, timeout=DEFAULT_TIMEOUT_SHORT)
            return result.stdout or ""
        except ExifToolError as e:
            logger.error("タグ取得エラー (%s): %s", file_path.name, e)
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
        args = ["-r"]
        for ext in extensions:
            args += ["-ext", ext]
        args += ["-j"]
        if group_names:
            args += ["-G1"]
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
            "-GPSLatitudeRef=N" if lat >= 0 else "-GPSLatitudeRef=S",
            "-GPSLongitudeRef=E" if lng >= 0 else "-GPSLongitudeRef=W",
            "-overwrite_original",
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
        target_dir: Path,
        gpx_files: List[Path],
        geosync: str,
        timeout: int = DEFAULT_TIMEOUT_LONG,
    ) -> Tuple[str, str]:
        """GPXファイル群を元に対象ディレクトリへジオタグを一括付与する。

        Returns:
            (stdout, stderr) のタプル。件数集計は呼び出し側で行う。
        """
        args = [f"-geotag={gpx}" for gpx in gpx_files]
        args += [f"-geosync={geosync}", "-overwrite_original", str(target_dir)]
        result = self._run(args, timeout=timeout)
        return result.stdout or "", result.stderr or ""
