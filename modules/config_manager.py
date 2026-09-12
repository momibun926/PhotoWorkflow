"""アプリケーション全体の設定管理を統一するモジュール。"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from types import SimpleNamespace

logger = logging.getLogger(__name__)


class ConfigManager:
    """JSON形式の設定ファイル（config.json）を管理するクラス。"""

    def __init__(self, config_path: Optional[Path] = None):
        """初期化。
        
        Args:
            config_path: 設定ファイルのパス。Noneの場合はカレントディレクトリを探索
        """
        self.config_path = self._resolve_config_path(config_path)
        self.config: Dict[str, Any] = {}
        self.namespace: SimpleNamespace = SimpleNamespace()
        self.load()

    @staticmethod
    def _resolve_config_path(config_path: Optional[Path]) -> Path:
        """設定ファイルパスを解決。"""
        if config_path and Path(config_path).exists():
            return Path(config_path)

        # デフォルト検索順序
        search_paths = [
            Path("config.json"),
            Path(__file__).parent.parent / "config.json",
            Path.cwd() / "config.json",
        ]

        for path in search_paths:
            if path.exists():
                logger.info("設定ファイルを検出: %s", path)
                return path

        raise FileNotFoundError(
            "config.json が見つかりません。以下のいずれかの場所に配置してください:\n"
            + "\n".join(str(p) for p in search_paths)
        )

    def load(self) -> None:
        """設定ファイルを読み込み。"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {self.config_path}")

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            logger.info("設定ファイルを読み込みました: %s", self.config_path)
        except json.JSONDecodeError as e:
            logger.error("JSON形式エラー: %s", e)
            raise ValueError(f"設定ファイルのJSON形式が不正です: {self.config_path}") from e
        except Exception as e:
            logger.error("設定ファイル読み込みエラー: %s", e)
            raise

        # 設定値の検証
        self._validate_config()

        # 辞書をSimpleNamespaceに変換（ドットアクセスを可能に）
        self.namespace = self._dict_to_namespace(self.config)

    def _validate_config(self) -> None:
        """設定の必須キーを検証。"""
        required_keys = ["directories", "external_tools"]
        for key in required_keys:
            if key not in self.config:
                raise ValueError(f"設定ファイルに必須キー '{key}' がありません")

        # ディレクトリキー
        # 'to_amazon_jpeg' は必須から外し、'copy_targets'（可変長のコピー先リスト）
        # または後方互換の 'directories.to_amazon_jpeg' のどちらかで指定できるようにする。
        required_dirs = ["base", "from_camera", "to_note"]
        dirs = self.config.get("directories", {})
        for key in required_dirs:
            if key not in dirs:
                raise ValueError(f"directories に必須キー '{key}' がありません")

        # 外部ツールキー
        required_tools = ["exiftool"]
        tools = self.config.get("external_tools", {})
        for key in required_tools:
            if key not in tools:
                raise ValueError(f"external_tools に必須キー '{key}' がありません")

    @staticmethod
    def _dict_to_namespace(data: Any) -> Any:
        """辞書をSimpleNamespaceに再帰的に変換。
        
        ドット記法（config.directories.base）でアクセス可能にする。
        """
        if isinstance(data, dict):
            return SimpleNamespace(**{
                k: ConfigManager._dict_to_namespace(v) for k, v in data.items()
            })
        elif isinstance(data, list):
            return [ConfigManager._dict_to_namespace(v) for v in data]
        return data

    def get(self, key: str, default: Any = None) -> Any:
        """設定値をキーで取得（辞書形式）。
        
        Args:
            key: ドット区切りのキー（例：'directories.base'）
            default: キーが存在しない場合のデフォルト値
            
        Returns:
            設定値、またはデフォルト値
        """
        keys = key.split(".")
        value = self.config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            logger.warning("設定キーが見つかりません: %s, デフォルト値を使用", key)
            return default

    def get_directory(self, dir_key: str) -> Path:
        """ディレクトリパスを取得し、Pathオブジェクトとして返す。
        
        Args:
            dir_key: ディレクトリキー（例：'base', 'from_camera'）
            
        Returns:
            Pathオブジェクト
            
        Raises:
            KeyError: ディレクトリキーが存在しない場合
        """
        try:
            dir_path = Path(self.config["directories"][dir_key])
            return dir_path
        except KeyError as e:
            logger.error("ディレクトリ設定が見つかりません: %s", dir_key)
            raise KeyError(f"ディレクトリ設定が見つかりません: {dir_key}") from e

    def get_tool_path(self, tool_key: str) -> Path:
        """外部ツールのパスを取得し、Pathオブジェクトとして返す。

        gps_tagger / exif_utils / manual_gps_tagger / exif_exporter は
        すべて ExifToolClient 経由でこのメソッドを呼び出し、exiftoolの
        パス解決を一元化している。
        
        Args:
            tool_key: ツールキー（例：'exiftool', 'flame_script'）
            
        Returns:
            Pathオブジェクト
            
        Raises:
            KeyError: ツールキーが存在しない場合
        """
        try:
            tool_path = Path(self.config["external_tools"][tool_key])
            return tool_path
        except KeyError as e:
            logger.error("外部ツール設定が見つかりません: %s", tool_key)
            raise KeyError(f"外部ツール設定が見つかりません: {tool_key}") from e

    def get_copy_targets(self) -> List[Dict[str, str]]:
        """JPEGの追加コピー先（Amazon Photos等）のリストを取得。

        config.json に 'copy_targets'（[{"name": ..., "path": ...}, ...]の形）が
        あればそれを使う。指定がなければ後方互換のため、旧来の単一キーだった
        directories.to_amazon_jpeg から1件だけ組み立てる（互換用フォールバック）。
        コピー先を増やしたい場合は copy_targets にオブジェクトを追加するだけでよく、
        photo_copier.py 側のコード変更は不要。

        Returns:
            [{"name": "表示名", "path": "コピー先パス"}, ...] のリスト（0件もあり得る）
        """
        targets = self.config.get("copy_targets")
        if targets:
            return targets

        dirs = self.config.get("directories", {})
        if "to_amazon_jpeg" in dirs:
            return [{"name": "to_amazon_jpeg", "path": dirs["to_amazon_jpeg"]}]
        return []

    def get_file_extensions(self) -> Dict[str, Set[str]]:
        """対応ファイル拡張子（JPEG/RAW/GPX）を取得。

        config.json の 'file_types' で上書きでき、指定がないキーは
        constants.py のデフォルトにフォールバックする。RAWフォーマットが
        異なるカメラ（Fuji の .raf、Canon の .cr3 等）を追加したい場合、
        コードを変更せずここに列挙するだけでよい。
        """
        from . import constants  # 遅延importで循環参照を避ける

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
        """カメラ・レンズ名の表記ゆれ補正ルールを取得。

        config.json の 'camera_name_rules' で機種別の補正ルールを上書きできる。
        Nikon以外のカメラを使う場合や、新機種の略記ルールを追加したい場合に
        constants.py を編集せずに対応できる。
        """
        from . import constants

        rules = self.config.get("camera_name_rules", {})
        return {
            "replacements": rules.get("replacements", constants.CAMERA_NAME_REPLACEMENTS),
            "removes": rules.get("removes", constants.CAMERA_NAME_REMOVES),
            "brand_replacements": rules.get("brand_replacements", constants.CAMERA_NAME_BRAND_REPLACEMENTS),
        }

    def get_gps_sync_timezone(self) -> str:
        """GPXログとの時刻同期に使うタイムゾーンを取得。

        海外旅行など、撮影地のタイムゾーンが日本と異なる場合に
        config.json の 'gps.sync_timezone' で上書きできる。
        """
        from . import constants

        return self.config.get("gps", {}).get("sync_timezone", constants.GPS_SYNC_TIMEZONE)

    def get_exif_export_config(self) -> Dict[str, Optional[Any]]:
        """個別.exifファイルに出力するタグと日本語ラベルの設定を取得。

        config.json の 'exif_export.target_tags' / 'exif_export.tag_labels' で
        上書きできる。指定がなければ None を返すので、呼び出し側
        （exif_exporter.py）は自身のデフォルト値を使う。
        """
        exif_export = self.config.get("exif_export", {})
        return {
            "target_tags": exif_export.get("target_tags"),
            "tag_labels": exif_export.get("tag_labels"),
        }

    def get_enabled_steps(self) -> Dict[str, bool]:
        """有効化されているワークフローステップを取得。

        config.json の 'steps' で特定ステップを恒常的に無効化できる
        （例: 手動GPS付与ツールを常にスキップしたい、Amazon Photosへの
        バックアップ運用をやめてSTEP2だけ使いたい、など）。
        指定がないステップはデフォルトで全て有効。
        """
        default_steps = {
            "gps_tag": True,
            "manual_gps": True,
            "copy": True,
            "exif_export": True,
            "frame": True,
        }
        default_steps.update(self.config.get("steps", {}))
        return default_steps

    def __getattr__(self, name: str) -> Any:
        """属性アクセスのサポート（namespace経由）。"""
        return getattr(self.namespace, name)


class YamlConfigManager:
    """YAML形式の設定ファイル（フレームレイアウト設定）を管理するクラス。"""

    #: frame_processor.py が参照する必須キー。欠落時は起動時に検出できるよう検証する。
    REQUIRED_TOP_KEYS = ("fonts", "colors", "ratios", "layout", "output")
    REQUIRED_KEYS = {
        "fonts": ("size_type", "main_size", "sub_size", "bold", "regular"),
        "colors": ("bg", "main", "sub"),
        "ratios": ("bottom_margin", "side_margin"),
        "layout": ("top", "bottom"),
        "output": ("prefix", "dir_name"),
    }

    def __init__(self, config_path: Path):
        """初期化。
        
        Args:
            config_path: YAML設定ファイルのパス
            
        Raises:
            FileNotFoundError: ファイルが存在しない場合
            ValueError: YAML形式が不正、または必須キーが不足している場合
        """
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.namespace: SimpleNamespace = SimpleNamespace()
        self.load()

    def load(self) -> None:
        """YAMLファイルを読み込み。"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {self.config_path}")

        try:
            import yaml
        except ImportError:
            logger.error("PyYAML がインストールされていません。pip install pyyaml を実行してください")
            raise ImportError("PyYAML is required for YAML configuration files")

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
            logger.info("YAML設定ファイルを読み込みました: %s", self.config_path)
        except yaml.YAMLError as e:
            logger.error("YAML形式エラー: %s", e)
            raise ValueError(f"YAML形式が不正です: {self.config_path}") from e
        except Exception as e:
            logger.error("設定ファイル読み込みエラー: %s", e)
            raise

        self._validate_config()
        self.namespace = self._dict_to_namespace(self.config)

    def _validate_config(self) -> None:
        """必須キーの検証。frame_processor.py実行中のAttributeErrorを未然に防ぐ。"""
        for top_key in self.REQUIRED_TOP_KEYS:
            if top_key not in self.config:
                raise ValueError(f"{self.config_path.name} に必須キー '{top_key}' がありません")

        for top_key, sub_keys in self.REQUIRED_KEYS.items():
            section = self.config.get(top_key, {}) or {}
            for sub_key in sub_keys:
                if sub_key not in section:
                    raise ValueError(
                        f"{self.config_path.name} の '{top_key}' に必須キー '{sub_key}' がありません"
                    )

    @staticmethod
    def _dict_to_namespace(data: Any) -> Any:
        """辞書をSimpleNamespaceに再帰的に変換。"""
        if isinstance(data, dict):
            return SimpleNamespace(**{
                k: YamlConfigManager._dict_to_namespace(v) for k, v in data.items()
            })
        elif isinstance(data, list):
            return [YamlConfigManager._dict_to_namespace(v) for v in data]
        return data

    def get(self, key: str, default: Any = None) -> Any:
        """設定値をキーで取得。
        
        Args:
            key: ドット区切りのキー（例：'layout.top'）
            default: デフォルト値
            
        Returns:
            設定値
        """
        keys = key.split(".")
        value = self.config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            logger.warning("設定キーが見つかりません: %s, デフォルト値を使用", key)
            return default

    def __getattr__(self, name: str) -> Any:
        """属性アクセスのサポート。"""
        return getattr(self.namespace, name)


def get_or_create_config(config_path: Optional[Path] = None) -> ConfigManager:
    """グローバルな設定マネージャーを取得（シングルトン）。
    
    Args:
        config_path: 設定ファイルパス
        
    Returns:
        ConfigManager インスタンス
    """
    if not hasattr(get_or_create_config, "_instance"):
        get_or_create_config._instance = ConfigManager(config_path)
    return get_or_create_config._instance
