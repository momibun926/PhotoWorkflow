"""アプリケーション全体で使用する定数の集約モジュール。"""

from typing import Dict, Set

# ===== ファイル拡張子 =====
# 各処理（写真整理・EXIF出力・GPSタグ付け等）で扱う対象ファイルの拡張子を
# ここに集約し、判定ロジックが散らばらないようにする。
VALID_IMAGE_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".nef", ".arw", ".dng"}  # ツール全体で「画像」として扱う全拡張子（JPEG/PNG/各社RAW）
JPEG_EXTENSIONS: Set[str] = {".jpg", ".jpeg"}  # JPEGのみを対象にしたい処理（透かし焼き込み等）で使用
RAW_EXTENSIONS: Set[str] = {".nef", ".arw", ".dng"}  # Nikon(NEF)/Sony(ARW)/Adobe DNG等のRAW拡張子
GPX_EXTENSIONS: Set[str] = {".gpx"}  # GPSログファイル（GPXトラック）の拡張子。ジオタグ付け時に検索対象とする

# ===== ExifTool関連 =====
EXIF_DATE_FORMAT: str = "%Y%m%d"  # exiftoolに渡す日付フォーマット（例: 20240101）。ファイル名の日付部などに使用
EXIF_TAGS_BATCH: Dict[str, str] = {
    # exiftoolから一括取得する際に使う「内部キー名 → exiftoolタグ名」の対応表。
    # exif_exporter.py 等が本辞書のキーを使ってEXIF情報を種類ごとに参照する。
    "model": "EXIF:Model",  # カメラ機種名
    "lens": "EXIF:LensModel",  # レンズ名
    "focal_length": "EXIF:FocalLength",  # 焦点距離
    "iso": "EXIF:ISO",  # ISO感度
    "f_number": "EXIF:FNumber",  # F値（絞り）
    "exposure_time": "EXIF:ExposureTime",  # シャッタースピード（露出時間）
    "exposure_compensation": "EXIF:ExposureCompensation",  # 露出補正値
    "exposure_program": "EXIF:ExposureProgram",  # 露出プログラム（AUTO/M/P/A/S等、番号で格納）
    "picture_control": "MakerNotes:PictureControlName",  # Nikon独自のピクチャーコントロール名
    "white_balance": "EXIF:WhiteBalance",  # ホワイトバランス設定
    "color_temperature": "EXIF:ColorTemperature",  # 色温度（K）
    "orientation": "Orientation",  # 画像の回転情報（縦位置/横位置判定に使用）
    "gps_latitude": "GPSLatitude",  # GPS緯度
    "gps_longitude": "GPSLongitude",  # GPS経度
}

# ===== 機材名フォーマット =====
# EXIFから取得した機種名をそのまま表示すると冗長・不揃いになるため、
# 見栄えの良い表記に正規化するための置換ルール群。
CAMERA_NAME_REPLACEMENTS: Dict[str, str] = {
    # Nikon Z50II のような表記ゆれ（アンダースコア区切りやスペース区切り、大文字小文字違い）を統一する
    "Z50_2": "Z50II",
    "Z50 2": "Z50II",
    "Z50ii": "Z50II",
    # 末尾の "_数字" をローマ数字表記に変換する汎用ルール（例: レンズ名の "III型" 等）
    "_3": "III",
    "_2": "II",
    "_4": "IV",
    "_5": "V",
}

# 機種名・レンズ名から取り除く冗長なブランド接頭辞（大文字/先頭大文字/小文字の表記ゆれをすべて列挙）
CAMERA_NAME_REMOVES: list = [
    "NIKON", "Nikon", "nikon",
    "NIKKOR", "Nikkor", "nikkor"
]

# 他社ブランド名の大文字表記を先頭大文字のみの読みやすい表記に置換する
CAMERA_NAME_BRAND_REPLACEMENTS: Dict[str, str] = {
    "SONY": "Sony",
    "CANON": "Canon",
    "FUJIFILM": "Fujifilm",
}

# ===== 露出モード =====
# exiftoolのExposureProgramタグは数値で返るため、画面表示用の短い記号に変換するマップ
EXPOSURE_MODE_MAP: Dict[int, str] = {
    0: "AUTO",  # 自動
    1: "M",     # マニュアル
    2: "P",     # プログラムオート
    3: "A",     # 絞り優先オート
    4: "S",     # シャッター優先オート
}

# ===== GUI色設定（16進数） =====
# ダークテーマのGUIで使う配色をここに集約し、各画面で色がバラバラにならないようにする
GUI_COLORS: Dict[str, str] = {
    "dark_bg": "#2D2D2D",     # ウィンドウ・パネルの背景色
    "primary": "#0078D4",     # 強調色（ボタンやアクティブ状態など）
    "text_light": "#DCDCDC",  # 明るい文字色（暗い背景上のテキスト用）
    "black": "#000000",       # 黒（枠線など）
}

# ===== ロゴ設定 =====
LOGO_FILENAME: str = "nikon_logo.png"  # 透かし焼き込み時に合成するロゴ画像のファイル名
LOGO_SIZE_RATIO: float = 2.3  # ロゴの表示サイズを決める倍率（フォントサイズ等との相対比）

# ===== レイアウト設定（デフォルト） =====
# 透かし（EXIF情報の焼き込み）を画像に配置する際のデフォルトのレイアウト値
DEFAULT_TOP_PADDING_RATIO: float = 0.20  # 画像上部に確保する余白の割合（画像高さに対する比率）
DEFAULT_Y_OFFSET_PX: int = 0  # 文字列描画位置の縦方向オフセット（ピクセル）
DEFAULT_LINE_SPACING_PX: int = 40  # 複数行のテキストを描画する際の行間（ピクセル）
DEFAULT_LOGO_LEFT_OFFSET: int = 70  # ロゴの左端からのオフセット（ピクセル）
DEFAULT_LOGO_TOP_OFFSET: int = 70  # ロゴの上端からのオフセット（ピクセル）

# ===== 処理設定 =====
THUMBNAIL_SIZE: int = 200  # GUI等で表示するサムネイル画像の一辺のサイズ（ピクセル）
GPS_SYNC_TIMEZONE: str = "+09:00"  # GPXログとカメラ内蔵時計の時刻を同期する際に使うタイムゾーン（日本時間）
JPEG_QUALITY: int = 95  # 画像保存時のJPEG品質（0-100、高いほど高画質・大容量）
JPEG_SUBSAMPLING: int = 0  # JPEGのクロマサブサンプリング設定（0=4:4:4、色情報を間引かず高画質を維持）

# ===== ロギング設定 =====
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"  # ログファイルに書き出す際の詳細フォーマット（時刻・レベル・ロガー名・メッセージ）
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"  # 上記フォーマット中の時刻表記
LOG_LEVEL_FILE: str = "INFO"  # ログファイルに記録する最低レベル
LOG_LEVEL_CONSOLE: str = "WARNING"  # コンソール（画面）に表示する最低レベル
# コンソールにはタイムスタンプやロガー名を出さず、簡潔に表示する
CONSOLE_LOG_FORMAT: str = "[%(levelname)s] %(message)s"  # コンソール出力用の簡潔なフォーマット

# ===== プログレス表示 =====
PROGRESS_FORMAT: str = "[{:3d}%] ({}/{})"  # 処理進捗をコンソールに表示する際の書式（パーセント、現在数/総数）
PROGRESS_BAR_WIDTH: int = 50  # テキストベースのプログレスバーの表示幅（文字数）

# ===== エラーメッセージ =====
# 例外発生時やバリデーション失敗時にユーザーへ表示する日本語メッセージのテンプレート集。
# {} の部分は各呼び出し箇所で該当する詳細情報（ファイルパスや例外内容など）に置換される。
ERROR_MESSAGES: Dict[str, str] = {
    "config_not_found": "設定ファイルが見つかりません: {}",
    "directory_not_found": "ディレクトリが見つかりません: {}",
    "exiftool_error": "ExifTool 実行エラー: {}",
    "font_not_found": "指定されたフォントが見つかりません: {}",
    "image_process_error": "画像処理エラー: {}",
    "gps_write_error": "GPS情報の書き込みエラー: {}",
}

# ===== 確認プロンプトメッセージ =====
# CLIでユーザーに次の操作を確認する際に表示するメッセージ集
PROMPT_MESSAGES: Dict[str, str] = {
    "next_step": "次のステップ [{}] に進みますか？",
    "manual_gps": "手動GPS付业とツールを起動しますか？",
    "invalid_input": "[!] 無効な入力です。'{}' のいずれかを入力してください",
}
