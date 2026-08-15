"""アプリケーション全体の設定管理を統一するモジュール。"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from types import SimpleNamespace
import sys

logger = logging.getLogger(__name__)


class ConfigManager:
    """JSON形式の設定ファイルを管理するクラス。"""

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
        required_dirs = ["base", "from_camera", "to_amazon_jpeg", "to_note"]
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

    def __getattr__(self, name: str) -> Any:
        """属性アクセスのサポート（namespace経由）。"""
        return getattr(self.namespace, name)


class YamlConfigManager:
    """YAML形式の設定ファイルを管理するクラス。"""

    def __init__(self, config_path: Path):
        """初期化。
        
        Args:
            config_path: YAML設定ファイルのパス
            
        Raises:
            FileNotFoundError: ファイルが存在しない場合
            ValueError: YAML形式が不正な場合
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

        self.namespace = self._dict_to_namespace(self.config)

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
