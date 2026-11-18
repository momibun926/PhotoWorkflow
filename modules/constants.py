"""アプリケーション全体で使う既定値。

config.json / YAML に書かれていない項目はここの値を使う。
設定ファイル側に同じ内容を書き写す必要はない（書いた項目だけが上書きされる）。
"""

from typing import Dict, List, Set

# ===== 対象ファイルの拡張子（config.json の file_types で上書きできる） =====
JPEG_EXTENSIONS: Set[str] = {".jpg", ".jpeg"}
RAW_EXTENSIONS: Set[str] = {".nef", ".arw", ".dng"}
GPX_EXTENSIONS: Set[str] = {".gpx"}
# フレーム付与で JPEG に加えて対象にする拡張子。RAW は Pillow で正しく開けないため含めない
FRAME_EXTRA_EXTENSIONS: Set[str] = {".png"}

# ===== EXIF =====
# 撮影日フォルダ（年/月/年月日）を作るための日付書式。exiftool の -d に渡す
EXIF_DATE_FORMAT: str = "%Y%m%d"

# EXIF抽出・書き出しで個別の .exif ファイルに書き出すタグと、その見出し（書いた順に出力される）。
# config.json の exif_export.target_tags / tag_labels で上書きできる
EXIF_EXPORT_TAGS: Dict[str, str] = {
    "IFD0:Model": "カメラ",
    "Composite:LensID": "レンズ",
    "ExifIFD:FocalLength": "焦点距離",
    "ExifIFD:FocalLengthIn35mmFormat": "換算",
    "ExifIFD:FNumber": "F",
    "ExifIFD:ExposureTime": "SS",
    "ExifIFD:ISO": "ISO",
    "ExifIFD:ExposureCompensation": "露出補正",
    "Nikon:AFAreaMode": "AFエリアモード",
    "Nikon:FocusMode": "フォーカスモード",
    "Nikon:PictureControlName": "ピクチャーコントロール",
    "Nikon:ShutterMode": "シャッターモード",
    "Nikon:WhiteBalance": "ホワイトバランス",
    "Composite:HyperfocalDistance": "過焦点距離",
}

# ===== カメラ・レンズ名の表記ゆれ補正（config.json の camera_name_rules で上書きできる） =====
# replacements → removes → brand_replacements の順に適用する。
# Nikon は Model が "NIKON Z 6_3" のように "_3" で世代を表すため、ローマ数字に直す
CAMERA_NAME_REPLACEMENTS: Dict[str, str] = {
    "Z50_2": "Z50II",
    "Z50 2": "Z50II",
    "Z50ii": "Z50II",
    "_3": "III",
    "_2": "II",
    "_4": "IV",
    "_5": "V",
}
# フレームにはメーカーロゴを入れるため、名前からメーカー名を除く
CAMERA_NAME_REMOVES: List[str] = [
    "NIKON", "Nikon", "nikon",
    "NIKKOR", "Nikkor", "nikkor",
]
# 先頭が大文字のメーカー名（"SONY ..."）を通常の表記にそろえる
CAMERA_NAME_BRAND_REPLACEMENTS: Dict[str, str] = {
    "SONY": "Sony",
    "CANON": "Canon",
    "FUJIFILM": "Fujifilm",
}

# exiftool の ExposureProgram（数値）をフレームに表示する記号
EXPOSURE_MODE_MAP: Dict[int, str] = {
    0: "AUTO",
    1: "M",
    2: "P",
    3: "A",
    4: "S",
}

# ===== GPS =====
# カメラ時計のずれ補正（exiftool の -geosync、"GPS時刻 − カメラ時刻"）の既定値。空なら補正なし。
# タイムゾーンではない点に注意。以前 "+09:00" を指定していたが、exiftool はこれを
# 「+9分」と解釈するため、全写真の位置が9分ずれていた
CAMERA_CLOCK_OFFSET: str = ""

# ===== フレーム付与。YAML に書かれていない場合の既定値 =====
LOGO_FILENAME: str = "nikon_logo.png"
LOGO_SIZE_RATIO: float = 2.3            # ロゴの高さ = 1行目の文字サイズ × この倍率
DEFAULT_TOP_PADDING_RATIO: float = 0.20  # 文字エリアの上端から1行目までの距離（文字エリアの高さに対する比率）
DEFAULT_LINE_SPACING_PX: int = 40        # 行の上端から次の行の上端までの距離
DEFAULT_LOGO_LEFT_OFFSET: int = 70       # ロゴの横位置の補正（px）
DEFAULT_LOGO_TOP_OFFSET: int = 70        # ロゴの縦位置の補正（px）
JPEG_QUALITY: int = 95
JPEG_SUBSAMPLING: int = 0  # 4:4:4。文字の縁に色にじみが出ないよう色情報を間引かない

# ===== ログ =====
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
CONSOLE_LOG_FORMAT: str = "[%(levelname)s] %(message)s"  # 画面には時刻やロガー名を出さず簡潔に
LOG_MAX_BYTES: int = 5 * 1024 * 1024  # これを超えたら photo_organizer.log.1 に切り替える
LOG_BACKUP_COUNT: int = 5             # 残す古いログの数（.1 〜 .5）

# ===== 画面表示 =====
PROGRESS_FORMAT: str = "[{:3d}%] ({}/{})"

ERROR_MESSAGES: Dict[str, str] = {
    "config_not_found": "設定ファイルが見つかりません: {}",
    "directory_not_found": "ディレクトリが見つかりません: {}",
}
