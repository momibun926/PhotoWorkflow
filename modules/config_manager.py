"""設定ファイルの読み込み。

  - ConfigManager: config.json（ディレクトリ・外部ツール・各種ルール）。全ステップが共有する
  - YamlConfigManager: フレームのレイアウト設定（standard.yaml など）。フレーム付与で使う

どちらも共通の基底クラス _ConfigFile を持ち、"directories.base" のようなドット区切りの
キーでの取得（get）と、config.directories.base のような属性でのアクセスができる。
PyQt6 には依存しない（GUI を使わないステップからも読み込めるようにするため）。
"""

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, Dict, List, Optional, Set

from . import constants

logger = logging.getLogger(__name__)

#: アプリのフォルダ（main.py のある場所 = modules フォルダの1つ上）。
#: 相対パスはカレントディレクトリではなくここを基準に解決する
#: （タスクスケジューラ等で別のフォルダから起動しても同じファイルを使うため）
APP_DIR: Path = Path(__file__).resolve().parent.parent


def resolve_relative_path(value: Any, base_dirs: List[Path]) -> Path:
    """相対パスを base_dirs の順に探して解決する（絶対パスはそのまま返す）。

    どの基準フォルダにも存在しない場合は、先頭の基準フォルダからのパスを返す
    （エラーメッセージに分かりやすいフルパスが出るようにするため）。
    """
    p = Path(str(value))
    if p.is_absolute():
        return p
    for base in base_dirs:
        candidate = base / p
        if candidate.exists():
            return candidate
    return base_dirs[0] / p if base_dirs else p


def _to_namespace(data: Any) -> Any:
    """辞書を SimpleNamespace に再帰的に変換する（リストの中の辞書も変換する）。"""
    if isinstance(data, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in data.items()})
    if isinstance(data, list):
        return [_to_namespace(v) for v in data]
    return data


class _ConfigFile:
    """設定ファイル1つを読み込む共通処理。

    サブクラスは _parse（ファイル形式ごとの読み込み）と _validate_config（必須キーの検証）を実装する。
    """

    #: ログやエラーメッセージに出すファイルの種類
    FORMAT_NAME = "設定"

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.namespace: SimpleNamespace = SimpleNamespace()
        self.load()

    def _parse(self, f: IO[str]) -> Any:
        raise NotImplementedError

    def _validate_config(self) -> None:
        raise NotImplementedError

    def load(self) -> None:
        """ファイルを読み込み、検証してから属性アクセス用の namespace を作る。

        Raises:
            FileNotFoundError: ファイルが無い場合
            ValueError: 書式が不正、または必須キーが足りない場合
        """
        if not self.config_path.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {self.config_path}")

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = self._parse(f)
        except ValueError:
            raise
        except Exception as e:
            logger.error("設定ファイル読み込みエラー (%s): %s", self.config_path, e)
            raise

        # 空ファイルは空の設定として扱い、必須キーの不足として検証で知らせる
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise ValueError(f"{self.config_path.name} の一番外側は {{ }}（キーと値の組）で書いてください")
        self.config = data
        logger.info("%sファイルを読み込みました: %s", self.FORMAT_NAME, self.config_path)

        self._validate_config()
        self.namespace = _to_namespace(self.config)

    def get(self, key: str, default: Any = None) -> Any:
        """ドット区切りのキー（例: 'directories.base'）で値を取得する。無ければ default。"""
        value: Any = self.config
        try:
            for k in key.split("."):
                value = value[k]
            return value
        except (KeyError, TypeError):
            logger.debug("設定キーが見つからないため既定値を使用: %s", key)
            return default

    def __getattr__(self, name: str) -> Any:
        """config.directories.base のような属性アクセスを namespace に委譲する。"""
        # namespace 自体がまだ無い（__init__ の途中など）ときに、ここで無限に再帰しないようにする
        if name == "namespace":
            raise AttributeError(name)
        return getattr(self.namespace, name)


class ConfigManager(_ConfigFile):
    """config.json を管理するクラス。"""

    FORMAT_NAME = "設定"

    #: カメラ時計のずれ補正値として受け付ける書式（[+|-]時:分:秒）。
    #: exiftool は "09:00" を「9分」と解釈するため、曖昧さを避けて時:分:秒に限定する。
    _CLOCK_OFFSET_PATTERN = re.compile(r"^[+-]?\d{1,2}:\d{2}:\d{2}$")

    def __init__(self, config_path: Optional[Path] = None):
        """
        Args:
            config_path: 設定ファイルのパス。None ならアプリのフォルダ → カレントディレクトリの順に探す。
                指定したパスが存在しない場合は FileNotFoundError（他の場所は探さない）
        """
        super().__init__(self._resolve_config_path(config_path))

    @staticmethod
    def _resolve_config_path(config_path: Optional[Path]) -> Path:
        """設定ファイルのパスを決める。

        明示的に指定された場合はそのファイルだけを使う。存在しなくても別の config.json に
        切り替えない（指定ミスに気付かないまま別の設定で動いてしまうのを防ぐため）。
        """
        if config_path is not None:
            path = Path(config_path)
            if not path.is_file():
                raise FileNotFoundError(f"指定された設定ファイルが見つかりません: {path.resolve()}")
            return path

        # アプリのフォルダを優先し、どこから起動しても同じ config.json を使うようにする
        search_paths = [APP_DIR / "config.json", Path.cwd() / "config.json"]
        for path in search_paths:
            if path.is_file():
                logger.info("設定ファイルを検出: %s", path)
                return path

        raise FileNotFoundError(
            "config.json が見つかりません。config.example.json を config.json という名前でコピーし、"
            "パスを自分の環境に合わせて書き換えてください。置き場所は次のいずれかです:\n"
            + "\n".join(str(p) for p in search_paths)
        )

    def _parse(self, f: IO[str]) -> Any:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            logger.error("JSON形式エラー: %s", e)
            raise ValueError(f"設定ファイルのJSON形式が不正です: {self.config_path}（{e}）") from e

    def _validate_config(self) -> None:
        """起動時に必須キーの不足に気付けるよう検証する。"""
        for key in ("directories", "external_tools"):
            if key not in self.config:
                raise ValueError(f"設定ファイルに必須キー '{key}' がありません")

        # JPEG の追加コピー先は copy_targets（または旧形式の directories.to_amazon_jpeg）で
        # 指定するため、ここでは必須にしない
        dirs = self.config.get("directories") or {}
        for key in ("base", "from_camera", "to_note"):
            if key not in dirs:
                raise ValueError(f"directories に必須キー '{key}' がありません")

        tools = self.config.get("external_tools") or {}
        if "exiftool" not in tools:
            raise ValueError("external_tools に必須キー 'exiftool' がありません")

    def resolve_path(self, value: Any) -> Path:
        """設定値に書かれたパスを解決する。

        相対パスはカレントディレクトリではなく、config.json のあるフォルダ →
        アプリのフォルダ（main.py のある場所）の順に探す。
        """
        return resolve_relative_path(value, [self.config_path.resolve().parent, APP_DIR])

    def get_directory(self, dir_key: str) -> Path:
        """directories.<dir_key> を Path で返す。

        Raises:
            KeyError: キーが無い場合
        """
        try:
            return Path(self.config["directories"][dir_key])
        except KeyError as e:
            logger.error("ディレクトリ設定が見つかりません: %s", dir_key)
            raise KeyError(f"ディレクトリ設定が見つかりません: {dir_key}") from e

    def get_tool_path(self, tool_key: str) -> Path:
        """external_tools.<tool_key> を Path で返す。

        exiftool のパスは ExifToolClient がこのメソッドで取得する（パス解決を1か所にまとめるため）。

        Raises:
            KeyError: キーが無い場合
        """
        try:
            return Path(self.config["external_tools"][tool_key])
        except KeyError as e:
            logger.error("外部ツール設定が見つかりません: %s", tool_key)
            raise KeyError(f"外部ツール設定が見つかりません: {tool_key}") from e

    def get_copy_targets(self) -> List[Dict[str, str]]:
        """JPEG の追加コピー先（[{"name": 表示名, "path": パス}, ...]）を返す。0件もあり得る。

        copy_targets が無ければ、旧形式の directories.to_amazon_jpeg から1件だけ組み立てる。
        """
        targets = self.config.get("copy_targets")
        if targets:
            return targets

        dirs = self.config.get("directories", {})
        if "to_amazon_jpeg" in dirs:
            return [{"name": "to_amazon_jpeg", "path": dirs["to_amazon_jpeg"]}]
        return []

    def get_file_extensions(self) -> Dict[str, Set[str]]:
        """対象の拡張子 {"jpeg", "raw", "gpx"}（小文字）。file_types に無い種類は constants の既定値。"""
        file_types = self.config.get("file_types", {})

        def _to_ext_set(key: str, default: Set[str]) -> Set[str]:
            values = file_types.get(key)
            if not values:
                return default
            return {str(v).lower() for v in values}

        return {
            "jpeg": _to_ext_set("jpeg_extensions", constants.JPEG_EXTENSIONS),
            "raw": _to_ext_set("raw_extensions", constants.RAW_EXTENSIONS),
            "gpx": _to_ext_set("gpx_extensions", constants.GPX_EXTENSIONS),
        }

    def get_camera_name_rules(self) -> Dict[str, Any]:
        """カメラ・レンズ名の補正ルール。camera_name_rules に無い項目は constants の既定値。"""
        rules = self.config.get("camera_name_rules", {})
        return {
            "replacements": rules.get("replacements", constants.CAMERA_NAME_REPLACEMENTS),
            "removes": rules.get("removes", constants.CAMERA_NAME_REMOVES),
            "brand_replacements": rules.get("brand_replacements", constants.CAMERA_NAME_BRAND_REPLACEMENTS),
        }

    def get_camera_clock_offset(self) -> Optional[str]:
        """カメラ時計のずれ補正値（exiftool の -geosync に渡す値）。

        gps.camera_clock_offset に「GPS時刻 − カメラ時刻」を "[+|-]時:分:秒" で書く
        （例: カメラ時計が1分30秒遅れているなら "+00:01:30"）。
        タイムゾーンの補正ではない。タイムゾーンは exiftool が写真の OffsetTimeOriginal、
        なければPCのタイムゾーン設定から扱う。

        Returns:
            補正値。未指定・空・書式不正なら None（補正なし）
        """
        gps = self.config.get("gps", {})
        if "sync_timezone" in gps:
            # 旧設定。"+09:00" が「+9分」と解釈され位置が9分ずれていたため廃止し、読み込まない
            logger.warning(
                "config.json の gps.sync_timezone は廃止されました（値 %r は使用しません）。"
                "カメラ時計のずれを補正する場合は gps.camera_clock_offset を \"+00:01:30\" の形式で指定してください",
                gps["sync_timezone"],
            )

        value = str(gps.get("camera_clock_offset", constants.CAMERA_CLOCK_OFFSET) or "").strip()
        if not value:
            return None
        if not self._CLOCK_OFFSET_PATTERN.match(value):
            logger.warning(
                "gps.camera_clock_offset の書式が不正なため補正しません: %r（\"+00:01:30\" のように時:分:秒で指定）",
                value,
            )
            return None
        return value

    def get_gps_options(self) -> Dict[str, Any]:
        """GPS位置情報の書き込みの動作設定（gps セクション）。

          - skip_existing_gps (既定 True): 既に位置情報がある写真は書き換えない
            （スマホ連携や手動で付けた位置を守るため）
          - max_interpolation_seconds (既定 null): GPX の記録点の間隔がこれを超える区間では補間しない
          - max_extrapolation_seconds (既定 null): GPX の記録の前後この秒数までは端の位置を使う
          null なら exiftool の既定値（1800秒）。
        """
        gps = self.config.get("gps", {})

        def _seconds(key: str) -> Optional[int]:
            value = gps.get(key)
            if value in (None, ""):
                return None
            try:
                seconds = int(value)
                if seconds < 0:
                    raise ValueError
                return seconds
            except (TypeError, ValueError):
                logger.warning("gps.%s の値が不正なため既定値を使います: %r", key, value)
                return None

        return {
            "skip_existing": bool(gps.get("skip_existing_gps", True)),
            "max_interpolation_secs": _seconds("max_interpolation_seconds"),
            "max_extrapolation_secs": _seconds("max_extrapolation_seconds"),
        }

    def get_exif_export_tags(self) -> Dict[str, str]:
        """EXIF抽出・書き出しで個別 .exif に書き出すタグと見出し {タグ名: 見出し}（書き出す順）。

        既定値は constants.EXIF_EXPORT_TAGS。config.json の exif_export で上書きできる:
          - target_tags: 書き出すタグの一覧（順序もこのとおりになる）
          - tag_labels: 見出しの上書き（書いたタグだけ。無いタグは既定の見出し、既定にも無ければタグ名）
        """
        exif_export = self.config.get("exif_export", {})
        labels = {**constants.EXIF_EXPORT_TAGS, **(exif_export.get("tag_labels") or {})}
        tags = exif_export.get("target_tags") or list(constants.EXIF_EXPORT_TAGS)
        return {tag: labels.get(tag, tag) for tag in tags}

    def get_enabled_steps(self) -> Dict[str, bool]:
        """ステップごとの有効/無効。steps に書かれていないステップは有効。"""
        enabled = {"gps_tag": True, "manual_gps": True, "copy": True, "exif_export": True, "frame": True}
        enabled.update({k: v for k, v in self.config.get("steps", {}).items() if not k.startswith("_")})
        return enabled


class YamlConfigManager(_ConfigFile):
    """フレームのレイアウト設定（YAML）を管理するクラス。"""

    FORMAT_NAME = "YAML設定"

    #: 必須キー。frame_processor.py の実行中に AttributeError で止まらないよう、読み込み時に検証する
    REQUIRED_TOP_KEYS = ("fonts", "colors", "layout", "output")
    REQUIRED_KEYS = {
        "colors": ("bg",),
        "output": ("prefix", "dir_name"),
    }
    #: layout.lines を使わない旧形式（top/bottom の2行）でだけ必須になるキー
    LEGACY_LINE_KEYS = {
        "fonts": ("main_size", "sub_size", "bold", "regular"),
        "colors": ("main", "sub"),
        "layout": ("top", "bottom"),
    }
    #: 選択肢が決まっている設定値
    CHOICES = {
        ("layout", "align"): ("left", "center", "right"),
        ("layout", "align_to"): ("image", "canvas"),
        ("layout", "text_area"): ("bottom", "top"),
        ("layout", "vertical_align"): ("padding", "center"),
        ("canvas", "aspect_fill"): ("center", "bottom", "top"),
        ("logo", "position"): ("left", "center", "right", "text_left", "text_right"),
        ("logo", "vertical"): ("bottom", "center", "top", "text"),
        ("scaling", "basis"): ("long_side", "display"),
        ("focal_length", "show_equivalent"): ("crop", "always", "never"),
    }

    def _parse(self, f: IO[str]) -> Any:
        # PyYAML はフレーム処理でしか使わないため、ここで読み込む
        try:
            import yaml
        except ImportError as e:
            logger.error("PyYAML がインストールされていません。pip install pyyaml を実行してください")
            raise ImportError("PyYAML is required for YAML configuration files") from e
        try:
            return yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error("YAML形式エラー: %s", e)
            raise ValueError(f"YAML形式が不正です: {self.config_path}") from e

    def _validate_config(self) -> None:
        name = self.config_path.name

        def require(section_name: str, keys) -> None:
            section = self.config.get(section_name) or {}
            for sub_key in keys:
                if sub_key not in section:
                    raise ValueError(f"{name} の '{section_name}' に必須キー '{sub_key}' がありません")

        for top_key in self.REQUIRED_TOP_KEYS:
            if top_key not in self.config:
                raise ValueError(f"{name} に必須キー '{top_key}' がありません")
        for top_key, sub_keys in self.REQUIRED_KEYS.items():
            require(top_key, sub_keys)

        layout = self.config.get("layout") or {}
        lines = layout.get("lines")
        if lines is None:
            for top_key, sub_keys in self.LEGACY_LINE_KEYS.items():
                require(top_key, sub_keys)
        else:
            if not isinstance(lines, list) or not lines:
                raise ValueError(f"{name} の 'layout.lines' は1行以上のリストで指定してください")
            for i, line in enumerate(lines, 1):
                if not isinstance(line, dict) or "text" not in line:
                    raise ValueError(f"{name} の 'layout.lines' の{i}行目に 'text' がありません")
                if line.get("align") not in (None, "left", "center", "right"):
                    raise ValueError(
                        f"{name} の 'layout.lines' の{i}行目の align は left / center / right のいずれかです"
                    )

        # 余白は ratios（旧形式）か canvas.margins のどちらかで指定する
        canvas = self.config.get("canvas") or {}
        if "ratios" not in self.config and "margins" not in canvas:
            raise ValueError(f"{name} に 'ratios' または 'canvas.margins' のどちらかが必要です")
        if "ratios" in self.config and "margins" not in canvas:
            require("ratios", ("bottom_margin", "side_margin"))

        for (section_name, key), choices in self.CHOICES.items():
            value = (self.config.get(section_name) or {}).get(key)
            if value is not None and value not in choices:
                raise ValueError(
                    f"{name} の '{section_name}.{key}' は {' / '.join(choices)} のいずれかです（指定値: {value}）"
                )

        # logo.makers: {"nikon": "nikon.png"} か {"google": {"file": "google.png", "height_font_ratio": 1.8}}
        logo = self.config.get("logo") or {}
        makers = logo.get("makers")
        if makers is not None:
            if not isinstance(makers, dict):
                raise ValueError(f"{name} の 'logo.makers' は「メーカー名: ファイル名」の形で指定してください")
            allowed = {"file", *constants.LOGO_MAKER_OVERRIDE_KEYS}
            for key, entry in makers.items():
                if isinstance(entry, dict):
                    unknown = set(entry) - allowed
                    if unknown:
                        raise ValueError(
                            f"{name} の 'logo.makers.{key}' に使えない項目があります: {', '.join(sorted(unknown))}"
                            f"（使えるのは {' / '.join(sorted(allowed))}）"
                        )
                elif not isinstance(entry, str) or not entry:
                    raise ValueError(f"{name} の 'logo.makers.{key}' にはロゴのファイル名を指定してください")
            if not logo.get("dir"):
                raise ValueError(f"{name} の 'logo.makers' を使うときは 'logo.dir'（ロゴのフォルダ）も指定してください")

        focal = self.config.get("focal_length") or {}
        equiv = focal.get("equivalent_35mm")
        if equiv is not None and not (isinstance(equiv, list) and all(isinstance(m, str) and m for m in equiv)):
            raise ValueError(f"{name} の 'focal_length.equivalent_35mm' はメーカー名のリスト（例: [\"google\"]）で指定してください")

        # アスペクト比は "4:5" か 1.25 のような正の数
        scaling = self.config.get("scaling") or {}
        for section_name, section, key in (("canvas", canvas, "aspect_ratio"),
                                           ("canvas", canvas, "aspect_ratio_portrait"),
                                           ("scaling", scaling, "display_box")):
            value = section.get(key)
            if value in (None, "", "none", "None", False):
                continue
            try:
                text = str(value)
                if ":" in text:
                    a, b = text.split(":", 1)
                    ok = float(a) > 0 and float(b) > 0
                else:
                    ok = float(text) > 0
            except ValueError:
                ok = False
            if not ok:
                raise ValueError(
                    f"{name} の '{section_name}.{key}' は \"4:5\" のような形式で指定してください（指定値: {value}）"
                )
