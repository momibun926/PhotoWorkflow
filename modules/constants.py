"""アプリケーション全体で使用する定数の集約モジュール。"""

from typing import Dict, Set

# ===== ファイル拡張子 =====
VALID_IMAGE_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".nef", ".arw", ".dng"}
JPEG_EXTENSIONS: Set[str] = {".jpg", ".jpeg"}
RAW_EXTENSIONS: Set[str] = {".nef", ".arw", ".dng"}
GPX_EXTENSIONS: Set[str] = {".gpx"}

# ===== ExifTool関連 =====
EXIF_DATE_FORMAT: str = "%Y%m%d"
EXIF_TAGS_BATCH: Dict[str, str] = {
    "model": "EXIF:Model",
    "lens": "EXIF:LensModel",
    "focal_length": "EXIF:FocalLength",
    "iso": "EXIF:ISO",
    "f_number": "EXIF:FNumber",
    "exposure_time": "EXIF:ExposureTime",
    "exposure_compensation": "EXIF:ExposureCompensation",
    "exposure_program": "EXIF:ExposureProgram",
    "picture_control": "MakerNotes:PictureControlName",
    "white_balance": "EXIF:WhiteBalance",
    "color_temperature": "EXIF:ColorTemperature",
    "orientation": "Orientation",
    "gps_latitude": "GPSLatitude",
    "gps_longitude": "GPSLongitude",
}

# ===== 機材名フォーマット =====
CAMERA_NAME_REPLACEMENTS: Dict[str, str] = {
    "Z50_2": "Z50II",
    "Z50 2": "Z50II",
    "Z50ii": "Z50II",
    "_3": "III",
    "_2": "II",
    "_4": "IV",
    "_5": "V",
}

CAMERA_NAME_REMOVES: list = [
    "NIKON", "Nikon", "nikon",
    "NIKKOR", "Nikkor", "nikkor"
]

CAMERA_NAME_BRAND_REPLACEMENTS: Dict[str, str] = {
    "SONY": "Sony",
    "CANON": "Canon",
    "FUJIFILM": "Fujifilm",
}

# ===== 露出モード =====
EXPOSURE_MODE_MAP: Dict[int, str] = {
    0: "AUTO",
    1: "M",
    2: "P",
    3: "A",
    4: "S",
}

# ===== GUI色設定（16進数） =====
GUI_COLORS: Dict[str, str] = {
    "dark_bg": "#2D2D2D",
    "primary": "#0078D4",
    "text_light": "#DCDCDC",
    "black": "#000000",
}

# ===== ロゴ設定 =====
LOGO_FILENAME: str = "nikon_logo.png"
LOGO_SIZE_RATIO: float = 2.3

# ===== レイアウト設定（デフォルト） =====
DEFAULT_TOP_PADDING_RATIO: float = 0.20
DEFAULT_Y_OFFSET_PX: int = 0
DEFAULT_LINE_SPACING_PX: int = 40
DEFAULT_LOGO_LEFT_OFFSET: int = 70
DEFAULT_LOGO_TOP_OFFSET: int = 70

# ===== 処理設定 =====
THUMBNAIL_SIZE: int = 200
GPS_SYNC_TIMEZONE: str = "+09:00"
JPEG_QUALITY: int = 95
JPEG_SUBSAMPLING: int = 0

# ===== ロギング設定 =====
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL_FILE: str = "INFO"
LOG_LEVEL_CONSOLE: str = "WARNING"

# ===== プログレス表示 =====
PROGRESS_FORMAT: str = "[{:3d}%] ({}/{})"
PROGRESS_BAR_WIDTH: int = 50

# ===== エラーメッセージ =====
ERROR_MESSAGES: Dict[str, str] = {
    "config_not_found": "設定ファイルが見つかりません: {}",
    "directory_not_found": "ディレクトリが見つかりません: {}",
    "exiftool_error": "ExifTool 実行エラー: {}",
    "font_not_found": "指定されたフォントが見つかりません: {}",
    "image_process_error": "画像処理エラー: {}",
    "gps_write_error": "GPS情報の書き込みエラー: {}",
}

# ===== 確認プロンプトメッセージ =====
PROMPT_MESSAGES: Dict[str, str] = {
    "next_step": "次のステップ [{}] に進みますか？",
    "manual_gps": "手動GPS付与ツールを起動しますか？",
    "invalid_input": "[!] 無効な入力です。'{}' のいずれかを入力してください。",
}
