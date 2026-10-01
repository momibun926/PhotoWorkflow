"""手動GPS付与用GUIツール。

PyQt6 を使用した対話的なGPS座標付与インターフェース。
RAW ファイルのサムネイル表示とExif情報の編集が可能。

画面構成は「左：サムネイル一覧＋ステータス」「右：地図（Leaflet）」の2ペイン。
地図上でクリックした位置、または検索した住所の座標を、選択した写真
（NEF/JPEG）のExif GPSタグとして書き込む。読み込み・書き込みはいずれも
UIをブロックしないよう QThread のワーカースレッドで実行する。
"""

import logging
import sys
import os
from typing import Optional, Dict, Any
from pathlib import Path

import rawpy
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QSplitter,
    QAbstractItemView, QFileDialog, QFrame, QProgressBar, QMessageBox,
    QDialog, QMenuBar
)
from PyQt6.QtGui import (
    QPixmap, QImage, QTransform, QIcon, QPainter, QColor, QAction, QFont, QFontMetrics
)
from PyQt6.QtCore import Qt, QUrl, pyqtSlot, QObject, QSize, QTimer, QThread, pyqtSignal
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

try:
    # パッケージとして実行された場合（本来の使われ方）
    from .config_manager import ConfigManager
    from .exiftool_client import ExifToolClient
    from .logging_config import setup_logging
except ImportError:
    # 単独スクリプトとして実行された場合のフォールバック
    from config_manager import ConfigManager
    from exiftool_client import ExifToolClient
    from logging_config import setup_logging

# ロギング設定
logger = logging.getLogger(__name__)

# --- 設定の読み込み（他モジュールと同じ ConfigManager を使用して一元化） ---
def load_app_config() -> Optional[ConfigManager]:
    """アプリケーション設定(config.json)の読み込み。見つからない場合はNone。"""
    try:
        return ConfigManager()
    except (FileNotFoundError, ValueError) as e:
        # config.jsonがない・不正な環境でも起動できるよう、例外にせず警告に留めてNoneを返す
        logger.warning("config.json の読み込みに失敗: %s", e)
        return None

# モジュール読み込み時に一度だけ設定を読み込み、以降はこのグローバル変数を各クラスで共有する
APP_CONFIG = load_app_config()
# 起動時に自動で開くフォルダ。config.jsonが読み込めていればそこに設定された
# 'from_camera' ディレクトリ、読み込めなければ固定のフォールバックパスを使う
INIT_DIR = (
    str(APP_CONFIG.get_directory("from_camera"))
    if APP_CONFIG is not None
    else r"C:\SHARE\Photo\from_camera"
)


def format_technical_terms(text: str) -> str:
    """カメラ・レンズ名の表記を補正。"""
    if not text:
        return ""
    # メソッドチェーンで、Nikon機種名・レンズ略称にありがちな表記ゆれを
    # 見た目の良い表記へ順番に置換していく（例: "Z50_2" → "Z50II"）
    text = (
        text.replace("Z50_2", "Z50II")
        .replace("Z50 2", "Z50II")
        .replace("Z50ii", "Z50II")
        .replace("dx", "DX")
        .replace("vr", "VR")
    )
    return text

# --- GPS状態の表示定義 ---
# 読み込み時に判定したGPSの状態。サムネイル右上のバッジとファイル名の先頭記号に使う。
#   yes     : GPS情報あり（書き込み不要）
#   no      : GPS情報なし（手動で付与が必要）
#   unknown : 読み取りに失敗して判定できなかった
GPS_YES, GPS_NO, GPS_UNKNOWN = "yes", "no", "unknown"
GPS_BADGES = {
    GPS_YES: ("GPS", "#2E9E4F"),        # 緑
    GPS_NO: ("GPSなし", "#D9822B"),     # オレンジ
    GPS_UNKNOWN: ("GPS ?", "#6E6E6E"),  # グレー
}
GPS_NAME_PREFIX = {GPS_YES: "🚩 ", GPS_NO: "", GPS_UNKNOWN: "❓ "}

# QListWidgetItem に持たせるデータのロール
ROLE_PATH = Qt.ItemDataRole.UserRole          # 実ファイルパス
ROLE_THUMB = Qt.ItemDataRole.UserRole + 1     # バッジを描く前のサムネイル（QPixmap / None）
ROLE_GPS = Qt.ItemDataRole.UserRole + 2       # GPS状態（GPS_YES / GPS_NO / GPS_UNKNOWN）
ROLE_COORDS = Qt.ItemDataRole.UserRole + 3    # (lat, lng) または None


# NOTE: 以前ここには Google Maps API キー用の独自 ConfigManager クラスがあったが、
# 地図には Leaflet + OpenStreetMap を使用しており api_key はどこからも参照されて
# いなかった（デッドコード）ため削除。設定管理は config_manager.ConfigManager に
# 一元化する（モジュール冒頭の APP_CONFIG を参照）。

# --- バックグラウンド処理（読み込み）用スレッド ---
class LoadWorker(QThread):
    """ファイルリストを読み込むバックグラウンドスレッド。"""

    # progress: (現在何件目, 全体件数, ファイル名, サムネイルQImageまたはNone,
    #            GPS情報 {"state": GPS_YES/GPS_NO/GPS_UNKNOWN, "lat": float|None, "lng": float|None})
    progress = pyqtSignal(int, int, str, object, object)
    # finished: 読み込みが完了したファイル総数
    finished = pyqtSignal(int)

    def __init__(self, directory: str, thumb_size: int) -> None:
        super().__init__()
        self.directory = directory
        self.thumb_size = thumb_size
        # stop()が呼ばれるとFalseになり、run()内のループを途中で抜けるためのフラグ
        self._is_running = True
        # exiftoolのパス解決・呼び出しは ExifToolClient に一元化する
        self.et = ExifToolClient(config=APP_CONFIG)
        logger.info("LoadWorker 初期化: directory=%s, thumb_size=%d", directory, thumb_size)

    def run(self) -> None:
        """スレッド実行メイン処理。"""
        try:
            # 対象拡張子はNEF（Nikon RAW）とJPEGのみ
            valid_exts = ('.nef', '.jpg', '.jpeg')
            files = sorted([
                f for f in os.listdir(self.directory)
                if f.lower().endswith(valid_exts)
            ])
            total = len(files)

            if total == 0:
                # 対象ファイルが1件もなければ、その旨をログに残しfinished(0)を即座に通知して終了
                logger.warning("有効なファイルが見つかりません: %s", self.directory)
                self.finished.emit(0)
                return

            # フォルダ単位で一括してメタデータを取得（exiftool呼び出しは指定したディレクトリ内全ファイルを一度に処理）
            # ファイルごとにexiftoolを都度起動すると非常に遅いため、
            # ディレクトリ単位でまとめて1回のexiftool呼び出しにすることで高速化している。
            # 戻り値のキーは小文字のファイル名。
            meta_dict = self.et.get_gps_orientation_batch(
                Path(self.directory),
                extensions=[e.lstrip('.') for e in valid_exts],
            )
            fallback_count = 0

            for i, f in enumerate(files):
                if not self._is_running:
                    # UIスレッド側からstop()が呼ばれた場合、ここでループを中断する
                    logger.info("ユーザーが中止")
                    break

                full_path = os.path.join(self.directory, f)

                # 一括取得結果からこのファイル分のメタデータ（GPS座標・向き）を取り出す。
                # 一括取得がタイムアウト等で失敗した／このファイルが漏れた場合は、
                # 「GPSなし」と誤表示しないよう、このファイルだけ個別に取り直す
                meta = meta_dict.get(f.lower())
                if meta is None:
                    fallback_count += 1
                    meta = self.et.get_gps_orientation(Path(full_path))
                gps_info = self._to_gps_info(meta)
                # 向き情報（EXIF Orientation）。取得できなければ1（回転なし）とみなす
                orientation = meta.get('orientation', 1) if meta else 1

                try:
                    # サムネイル画像を読み込み・回転補正した上でUIスレッドへ通知する
                    qimg = self.load_thumbnail(full_path, orientation)
                    self.progress.emit(i + 1, total, f, qimg, gps_info)
                except Exception as e:
                    # 1ファイルの読み込みに失敗しても全体を止めず、そのファイルだけスキップして続行する
                    # （GPSの判定結果はサムネイルの成否と関係ないのでそのまま渡す）
                    logger.error("サムネイル読み込みエラー (%s): %s", f, e)
                    self.progress.emit(i + 1, total, f, None, gps_info)

            if fallback_count:
                logger.warning("一括取得で見つからず個別に再取得したファイル: %d/%d 件", fallback_count, total)

            self.finished.emit(total)
        except Exception as e:
            # ディレクトリ読み取り自体の失敗など、想定外のエラーはログを残し finished(0) で終了を通知
            logger.error("LoadWorker エラー: %s", e, exc_info=True)
            self.finished.emit(0)

    @staticmethod
    def _to_gps_info(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """exiftoolの取得結果をGPS状態の辞書に変換する。"""
        if meta is None:
            # 個別取得でも読めなかった（exiftoolエラー等）→ 判定不能
            return {"state": GPS_UNKNOWN, "lat": None, "lng": None}
        lat, lng = meta.get("lat"), meta.get("lng")
        if lat is not None and lng is not None:
            return {"state": GPS_YES, "lat": lat, "lng": lng}
        return {"state": GPS_NO, "lat": None, "lng": None}

    def stop(self) -> None:
        """スレッドを停止。"""
        # run()のforループが次の反復でこのフラグを見て自発的に終了する（強制終了ではない）
        self._is_running = False
        logger.debug("LoadWorker 停止要求")

    def load_thumbnail(self, path: str, orientation: int) -> Optional[QImage]:
        """ファイルのサムネイルを読み込み。"""
        is_nef = path.lower().endswith('.nef')
        qimg: Optional[QImage] = None

        try:
            if is_nef:
                # NEF（RAW）はQImageで直接開けないため、exiftoolでプレビュー画像を抽出する
                qimg = self.extract_thumb_fast(path)
            else:
                # JPEGはQt標準機能でそのまま読み込める
                qimg = QImage(path)

            if qimg and not qimg.isNull():
                # EXIFのOrientation情報に応じて回転・反転を適用してから、指定サイズにリサイズする
                qimg = self.apply_rotation(qimg, orientation)
                return qimg.scaled(
                    self.thumb_size, self.thumb_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )

            if is_nef:
                # exiftoolでのプレビュー抽出に失敗した場合、rawpyによる抽出にフォールバックする
                return self.extract_thumb_fallback(path)
        except Exception as e:
            logger.error("サムネイル読み込みエラー (%s): %s", os.path.basename(path), e)

        return None

    def apply_rotation(self, qimg, orientation):
        # EXIF Orientation値（1〜8）に応じた回転・反転を QTransform で表現する。
        # 1（そのまま）や未指定（0など）はそのまま返す。
        if orientation <= 1: return qimg
        transform = QTransform()
        if orientation == 3: transform.rotate(180)          # 180度回転（天地逆）
        elif orientation == 6: transform.rotate(90)          # 時計回りに90度回転
        elif orientation == 8: transform.rotate(270)         # 反時計回りに90度回転
        elif orientation == 2: transform.scale(-1, 1)        # 左右反転
        elif orientation == 4: transform.scale(1, -1)        # 上下反転
        elif orientation == 5: transform.rotate(90).scale(-1, 1)   # 90度回転＋左右反転
        elif orientation == 7: transform.rotate(270).scale(-1, 1)  # 270度回転＋左右反転
        return qimg.transformed(transform, Qt.TransformationMode.SmoothTransformation)

    def extract_thumb_fast(self, path: str) -> Optional[QImage]:
        """ExifTool でプレビュー画像を抽出。"""
        try:
            # NEFファイル内に埋め込まれたJPEGプレビュー（PreviewImageタグ）を
            # バイナリのまま取得し、QImageに変換する（RAW自体をデコードするより高速）
            data = self.et.get_binary_tag(Path(path), "PreviewImage", timeout=5)
            if data:
                qimg = QImage.fromData(data)
                if not qimg.isNull():
                    return qimg
        except Exception as e:
            logger.debug("サムネイル抽出失敗 (Fast): %s", e)
        return None

    def extract_thumb_fallback(self, path: str) -> Optional[QImage]:
        """rawpy でサムネイルを抽出（フォールバック）。"""
        try:
            # exiftoolでのプレビュー抽出がうまくいかない機種・ファイルのために、
            # rawpy（libraw）で直接RAWファイルからサムネイルを取り出す
            with rawpy.imread(path) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    return QImage.fromData(thumb.data)
                # サムネイルがJPEG形式でない場合（ビットマップ形式等）は非対応として扱う
        except Exception as e:
            logger.debug("サムネイル抽出失敗 (Fallback): %s", e)
        return None

# --- バックグラウンド処理（書き込み）用スレッド ---
class WriteWorker(QThread):
    """GPS座標をファイルに書き込むバックグラウンドスレッド。"""

    # progress: (現在何件目, 全体件数, ファイル名)
    progress = pyqtSignal(int, int, str)
    # file_done: (ファイルパス, 書き込み成功ならTrue)。一覧のバッジを1件ずつ更新するために使う
    file_done = pyqtSignal(str, bool)
    # finished: (成功件数, 全体件数)
    finished = pyqtSignal(int, int)

    def __init__(self, items_data, lat: float, lng: float) -> None:
        super().__init__()
        # items_data: [(ファイルパス, 対応するQListWidgetItem), ...] のリスト
        self.items_data = items_data
        self.lat = lat
        self.lng = lng
        self.et = ExifToolClient(config=APP_CONFIG)
        logger.info("WriteWorker 初期化: %d ファイル, lat=%.6f, lng=%.6f",
                   len(items_data), lat, lng)

    def run(self) -> None:
        """スレッド実行メイン処理。"""
        total = len(self.items_data)
        success_count = 0

        # 選択された全ファイルに同一の緯度・経度を書き込んでいく（1件ずつexiftoolを呼び出す）
        for i, (path, _) in enumerate(self.items_data):
            current = i + 1
            filename = os.path.basename(path)
            self.progress.emit(current, total, filename)

            ok = self.et.write_gps(Path(path), self.lat, self.lng, timeout=10)
            if ok:
                success_count += 1
                logger.debug("GPS書込成功: %s", filename)
            else:
                # 書き込みに失敗した場合も処理は継続し、後続ファイルの書き込みを試みる
                logger.warning("GPS書込失敗: %s", filename)
            self.file_done.emit(path, ok)

        logger.info("GPS書込完了: %d/%d ファイル", success_count, total)
        self.finished.emit(success_count, total)

# --- プレビュー表示用ダイアログ ---
class PreviewDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(f"詳細プレビュー: {os.path.basename(path)}")
        # 左：拡大画像、右：EXIF情報パネルの横並びレイアウト
        self.main_layout = QHBoxLayout(self)
        self.image_label = QLabel("読み込み中...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: #000; border-radius: 4px;")
        # 画像側をEXIF情報パネルより広く取るためstretch比率を4:1程度にする
        self.main_layout.addWidget(self.image_label, stretch=4)
        self.info_panel = QFrame()
        self.info_panel.setFixedWidth(300)
        self.info_panel.setStyleSheet("background-color: #2D2D2D; border-radius: 4px; padding: 10px;")
        self.info_layout = QVBoxLayout(self.info_panel)
        title_label = QLabel("📋 撮影情報 (EXIF)")
        title_label.setStyleSheet("font-weight: bold; color: #0078D4; font-size: 16px; margin-bottom: 10px;")
        self.info_layout.addWidget(title_label)
        self.exif_label = QLabel("取得中...")
        self.exif_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.exif_label.setStyleSheet("font-family: 'Consolas', 'Yu Gothic'; font-size: 13px; color: #DCDCDC;")
        self.exif_label.setWordWrap(True)
        self.info_layout.addWidget(self.exif_label)
        self.info_layout.addStretch()
        self.main_layout.addWidget(self.info_panel)
        self.showMaximized()
        # ダイアログ表示直後だと描画がまだ確定していないため、少し遅延させてから読み込みを開始する
        QTimer.singleShot(100, self.load_content)

    def load_content(self):
        # 画像本体とEXIF情報テキストをそれぞれ読み込む（どちらも同期処理だがexiftool呼び出しのみで軽量）
        self.load_image(self.path)
        self.load_exif(self.path)

    def load_exif(self, path):
        try:
            # プレビューで表示したい代表的なタグのみを指定して取得する
            tags = ['Model', 'LensID', 'LensModel', 'ExposureTime', 'FNumber',
                    'ISO', 'FocalLength', 'DateTimeOriginal', 'ImageSize']
            et = ExifToolClient(config=APP_CONFIG)
            res = et.get_tags_text(Path(path), tags)
            lines = res.strip().split('\n')
            formatted_text = ""
            for line in lines:
                if ':' in line:
                    # exiftoolの "-S" 相当の "キー: 値" 形式の行をパースしてHTMLの箇条書き風に整形する
                    key, val = line.split(':', 1)
                    val = format_technical_terms(val.strip())
                    formatted_text += f"<b style='color:#888;'>{key.strip()}:</b><br>{val}<br><br>"
            if not formatted_text:
                formatted_text = "EXIF情報が見つかりませんでした。"
            self.exif_label.setText(formatted_text)
            # HTMLタグ（<b>や<br>）を解釈させるためRichTextモードを明示する
            self.exif_label.setTextFormat(Qt.TextFormat.RichText)
        except Exception as e:
            self.exif_label.setText(f"EXIF取得エラー: {str(e)}")

    def load_image(self, path):
        try:
            et = ExifToolClient(config=APP_CONFIG)
            # "-n" で数値のまま、"-S" で短縮タグ名形式の出力にして "Orientation: 6" のような1行を得る
            res_orient = et.run_raw(["-Orientation", "-n", "-S", path])
            orientation = 1
            if 'Orientation:' in res_orient:
                orientation = int(res_orient.split(': ')[1])
            if path.lower().endswith('.nef'):
                # NEFはQPixmapで直接開けないため、埋め込みJPEG（フルサイズ優先→プレビューの順）を取得する
                img_data = et.get_binary_tag(Path(path), "JpgFromRaw") or et.get_binary_tag(Path(path), "PreviewImage")
                pix = QPixmap.fromImage(QImage.fromData(img_data)) if img_data else QPixmap()
            else:
                pix = QPixmap(path)
            if pix and not pix.isNull():
                if orientation > 1:
                    # サムネイル一覧側と同様に、代表的な回転パターン（180/90/270度）のみ簡易対応
                    transform = QTransform()
                    if orientation == 3: transform.rotate(180)
                    elif orientation == 6: transform.rotate(90)
                    elif orientation == 8: transform.rotate(270)
                    pix = pix.transformed(transform, Qt.TransformationMode.SmoothTransformation)
                # ラベルの表示領域に収まるようアスペクト比を保ったまま縮小する
                pix = pix.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.image_label.setPixmap(pix)
            else: raise Exception()
        except: self.image_label.setText("プレビューに失敗しました。")

class MapBridge(QObject):
    """QWebChannel経由でJavaScript側から呼び出されるブリッジオブジェクト。

    地図（Leaflet）上でクリック・検索した座標をPython側（PyQt6側）へ
    伝えるための橋渡し役。JavaScriptの updateMarker() から
    bridge.updateCoords(lat, lng) が呼ばれると、Qtのシグナルとして中継する。
    """
    coords_changed = pyqtSignal(float, float)
    @pyqtSlot(float, float)
    def updateCoords(self, lat, lng): self.coords_changed.emit(lat, lng)

# --- メインウィンドウ ---
class NefGpsTool(QMainWindow):
    def __init__(self):
        super().__init__()
        # exiftool呼び出しは全て ExifToolClient に一元化（configはモジュール冒頭のAPP_CONFIG）
        self.et = ExifToolClient(config=APP_CONFIG)
        # 地図上で最後に選択された座標。(0.0, 0.0)は「未選択」を表す番兵値として扱う
        self.selected_coords = (0.0, 0.0)
        self.thumb_size = 180
        self.target_sidebar_width = 450
        self.current_dir = ""
        self.worker = None
        self.writer = None
        # ファイルパス → 一覧のアイテム（書き込み結果をバッジに反映するために使う）
        self.items_by_path: Dict[str, QListWidgetItem] = {}
        self.setWindowTitle("Photo GPS Writer (NEF & JPEG)")
        self.set_app_icon()
        self.create_menu_bar()
        self.init_ui()
        self.apply_dark_theme()
        self.setWindowState(Qt.WindowState.WindowMaximized)
        # 設定から得た初期フォルダが実在すればそれを、なければ固定パスを使い、
        # そのパスが存在する場合のみ自動的に読み込みを開始する
        init_path = INIT_DIR if INIT_DIR and os.path.exists(INIT_DIR) else r"C:\SHARE\Photo\from_camera"
        if os.path.exists(init_path): self.start_loading(init_path)

    def set_app_icon(self):
        # 同ディレクトリに app_icon.png があればそれを使い、なければ簡単な円形アイコンを描画して代用する
        icon_path = os.path.join(os.path.dirname(__file__), "app_icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            pixmap = QPixmap(64, 64)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setBrush(QColor("#0078D4"))
            painter.drawEllipse(10, 10, 44, 44)
            painter.end()
            self.setWindowIcon(QIcon(pixmap))

    def create_menu_bar(self):
        # メニューバー「ファイル」に「フォルダを開く」「終了」を追加
        menubar = self.menuBar()
        file_menu = menubar.addMenu("ファイル(&F)")
        open_act = QAction("フォルダを開く...", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self.select_folder)
        file_menu.addAction(open_act)
        file_menu.addSeparator()
        exit_act = QAction("終了(&X)", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

    def init_ui(self):
        # 以下、中央ウィジェットの構築。左サイドバー（パス表示・進捗・サムネイル一覧）と
        # 右側の地図ビューをQSplitterで左右に並べ、下部に座標表示と書き込みボタンを配置する。
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QVBoxLayout(cw)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        left_panel = QFrame()
        left_vbox = QVBoxLayout(left_panel)
        left_panel.setMinimumWidth(self.target_sidebar_width)
        # 現在選択中のフォルダパスを表示するラベル
        self.path_lbl = QLabel("フォルダを選択してください")
        self.path_lbl.setStyleSheet("background: #252525; padding: 8px; border-radius: 4px; color: #BBB; border: 1px solid #333;")
        left_vbox.addWidget(self.path_lbl)
        # 読み込み・書き込みの進捗状況を表示するステータス欄
        self.status_frame = QFrame()
        self.status_frame.setStyleSheet("background: #1A1A1A; border: 1px solid #333; border-radius: 4px; margin-top: 5px;")
        status_vbox = QVBoxLayout(self.status_frame)
        self.status_lbl = QLabel("待機中...")
        self.status_lbl.setStyleSheet("color: #888; font-size: 11px;")
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(8)
        self.pbar.setTextVisible(False)
        status_vbox.addWidget(self.status_lbl)
        status_vbox.addWidget(self.pbar)
        left_vbox.addWidget(self.status_frame)
        # サムネイル一覧（アイコンモードのリストウィジェット、複数選択可）
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(self.thumb_size, self.thumb_size))
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setSpacing(10)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.itemSelectionChanged.connect(self.on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        left_vbox.addWidget(self.list_widget)
        # 地図表示用のWebエンジンビューと、JS↔Python間のブリッジ（QWebChannel）を設定
        self.browser = QWebEngineView()
        self.bridge = MapBridge()
        self.bridge.coords_changed.connect(self.on_map_clicked)
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.browser.page().setWebChannel(self.channel)
        self.browser.setHtml(self.get_html())
        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(self.browser)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter)
        # 下部：現在の選択座標表示と、GPS書き込みを実行するボタン
        bottom = QHBoxLayout()
        self.coord_lbl = QLabel("緯度: --, 経度: --")
        self.coord_lbl.setStyleSheet("color: #FFE000; font-family: 'Consolas'; font-size: 15px; font-weight: bold;")
        self.btn_save = QPushButton(" 選択した画像にGPSを書き込む")
        self.btn_save.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DialogSaveButton))
        self.btn_save.setObjectName("save_btn")
        self.btn_save.setFixedHeight(50)
        bottom.addStretch()
        bottom.addWidget(self.coord_lbl)
        bottom.addStretch()
        bottom.addWidget(self.btn_save)
        layout.addLayout(bottom)
        self.btn_save.clicked.connect(self.start_writing)
        # ウィンドウ描画が落ち着いてからサイドバー幅を調整（初期表示直後だとwidth()が未確定なため遅延実行）
        QTimer.singleShot(500, self.adjust_splitter)

    def adjust_splitter(self):
        # サイドバーを固定幅に、残りを地図側に割り当てる
        self.splitter.setSizes([self.target_sidebar_width, self.width()-self.target_sidebar_width])

    def select_folder(self):
        # フォルダ選択ダイアログを開き、選択されればそのフォルダの読み込みを開始する
        path = QFileDialog.getExistingDirectory(self, "フォルダを選択")
        if path: self.start_loading(path)

    def start_loading(self, path):
        # 既存のLoadWorkerが動作中であれば、まず安全に停止させてから新しい読み込みを始める
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        self.list_widget.clear()
        self.items_by_path = {}
        self.path_lbl.setText(path)
        self.current_dir = path
        self.pbar.setValue(0)
        self.worker = LoadWorker(path, self.thumb_size)
        # progress/finishedシグナルをUI更新用のスロットに接続してからスレッドを開始する
        self.worker.progress.connect(self.add_item_to_list)
        self.worker.finished.connect(self.on_load_finished)
        self.worker.start()

    def add_item_to_list(self, current, total, filename, qimg, gps_info):
        # LoadWorker.progress シグナルのハンドラ。1ファイル分の結果をリストに追加する
        self.pbar.setMaximum(total)
        self.pbar.setValue(current)
        self.status_lbl.setText(f"読込中 ({current}/{total}): {filename}")

        path = os.path.join(self.current_dir, filename)
        item = QListWidgetItem(filename)
        # アイテムに実ファイルパスをユーザーデータとして保持させておく（後で選択時に参照するため）
        item.setData(ROLE_PATH, path)
        # バッジを描き直せるよう、バッジなしのサムネイルも保持しておく
        pix = QPixmap.fromImage(qimg) if qimg is not None and not qimg.isNull() else None
        item.setData(ROLE_THUMB, pix)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)

        # GPS状態に応じてバッジ・ファイル名の記号・ツールチップを設定する
        coords = (gps_info["lat"], gps_info["lng"]) if gps_info["state"] == GPS_YES else None
        self.apply_gps_state(item, gps_info["state"], coords)

        self.items_by_path[path] = item
        self.list_widget.addItem(item)

    def on_load_finished(self, count):
        # 読み込み完了時に、GPSあり／なしの件数を表示して手動付与が必要な枚数を分かるようにする
        states = [self.list_widget.item(i).data(ROLE_GPS) for i in range(self.list_widget.count())]
        n_yes, n_no, n_unknown = (states.count(s) for s in (GPS_YES, GPS_NO, GPS_UNKNOWN))
        text = f"完了: {count}枚（GPSあり {n_yes} / GPSなし {n_no}"
        text += f" / 判定不可 {n_unknown}）" if n_unknown else "）"
        self.status_lbl.setText(text)

    def apply_gps_state(self, item: QListWidgetItem, state: str, coords=None) -> None:
        """アイテムのGPS状態を更新し、バッジ付きアイコン・ファイル名・ツールチップに反映する。"""
        item.setData(ROLE_GPS, state)
        item.setData(ROLE_COORDS, coords)
        filename = os.path.basename(item.data(ROLE_PATH))
        item.setText(f"{GPS_NAME_PREFIX[state]}{filename}")
        item.setIcon(self.make_thumb_icon(item.data(ROLE_THUMB), state))
        if state == GPS_YES and coords:
            tip = f"GPSあり\n緯度: {coords[0]:.6f}\n経度: {coords[1]:.6f}"
        elif state == GPS_NO:
            tip = "GPSなし（手動で付与が必要）"
        else:
            tip = "GPS情報を読み取れませんでした（ログを確認してください）"
        item.setToolTip(f"{filename}\n{tip}")

    def make_thumb_icon(self, pix: Optional[QPixmap], state: str) -> QIcon:
        """サムネイルを正方形キャンバスの中央に置き、右上にGPS状態のバッジを描いたアイコンを作る。"""
        size = self.thumb_size
        # サムネイルのアスペクト比が正方形でない場合も見た目を揃えるため、
        # 固定サイズの透明キャンバスの中央にpixを描画してアイコン化する
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if pix is not None and not pix.isNull():
            x, y = (size - pix.width()) // 2, (size - pix.height()) // 2
            painter.drawPixmap(x, y, pix)
        else:
            # サムネイルが取れなかったファイルもバッジは見えるよう、暗い下地を描いておく
            painter.fillRect(8, 8, size - 16, size - 16, QColor("#2A2A2A"))
            painter.setPen(QColor("#777777"))
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter.value, "No preview")

        # --- 右上のバッジ（角丸の色付きラベル） ---
        label, color = GPS_BADGES[state]
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(11, size // 13))
        painter.setFont(font)
        fm = QFontMetrics(font)
        pad_x, pad_y, margin = 7, 3, 6
        bw = fm.horizontalAdvance(label) + pad_x * 2
        bh = fm.height() + pad_y * 2
        bx, by = size - bw - margin, margin
        painter.setPen(QColor(255, 255, 255, 200))   # 暗い写真・明るい写真どちらでも目立つよう白い縁取り
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(bx, by, bw, bh, bh / 2, bh / 2)
        painter.setPen(QColor("#FFFFFF"))
        # PyQt6 の drawText はフラグを int で受け取るため .value を渡す
        painter.drawText(bx, by, bw, bh, Qt.AlignmentFlag.AlignCenter.value, label)
        painter.end()
        return QIcon(canvas)

    def start_writing(self):
        # 選択がない、または地図上でまだ座標が選ばれていなければ何もしない
        selected = self.list_widget.selectedItems()
        if not selected or self.selected_coords == (0.0, 0.0): return
        # 誤操作防止のため書き込み前に確認ダイアログを出す
        reply = QMessageBox.question(self, '確認', f'{len(selected)}枚に書き込みますか？',
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        self.pbar.setMaximum(len(selected))
        self.pbar.setValue(0)
        # 選択中の各アイテムから (ファイルパス, アイテム) のタプルを作りWriteWorkerへ渡す
        items_data = [(i.data(ROLE_PATH), i) for i in selected]
        self.writer = WriteWorker(items_data, *self.selected_coords)
        self.writer.progress.connect(self.on_write_progress)
        self.writer.file_done.connect(self.on_write_file_done)
        self.writer.finished.connect(self.on_write_finished)
        # 書き込み中の二重実行を防ぐためボタンを無効化する
        self.btn_save.setEnabled(False)
        self.writer.start()

    def on_write_progress(self, current, total, name):
        self.pbar.setValue(current)
        self.status_lbl.setText(f"書込中 ({current}/{total}): {name}")

    def on_write_file_done(self, path, ok):
        # 書き込みに成功したファイルだけ「GPSあり」に更新する。
        # （以前は完了時点で選択中のアイテムに一律で🚩を付けていたため、
        #   書き込み中に選択を変えたり、失敗したファイルがあると表示がずれていた）
        if not ok:
            return
        item = self.items_by_path.get(path)
        if item is not None:
            written_coords = (self.writer.lat, self.writer.lng) if self.writer else None
            self.apply_gps_state(item, GPS_YES, written_coords)

    def on_write_finished(self, success, total):
        failed = total - success
        self.btn_save.setEnabled(True)
        if failed:
            self.status_lbl.setText(f"書き込み完了: 成功 {success} / 失敗 {failed}")
            QMessageBox.warning(
                self, "一部失敗",
                f"{total}枚中 {failed}枚の書き込みに失敗しました。\n"
                "失敗したファイルは「GPSなし」のままです。詳細はログを確認してください。"
            )
        else:
            self.status_lbl.setText(f"書き込み完了: {success}枚")
            QMessageBox.information(self, "完了", f"{success}枚に書き込みました。")

    def on_selection_changed(self):
        # 一覧の選択が変わるたびに、その写真の既存GPS情報を地図上に反映する
        sel = self.list_widget.selectedItems()
        if not sel:
            # 選択が空になったらマーカーを消す
            self.browser.page().runJavaScript("clearMarker();")
            return
        item = sel[0]
        state = item.data(ROLE_GPS)
        if state == GPS_YES and item.data(ROLE_COORDS):
            # 読み込み時（または書き込み時）に取得済みの座標を使い、exiftoolの再実行を省く
            lat, lng = item.data(ROLE_COORDS)
        elif state == GPS_NO:
            lat, lng = None, None
        else:
            lat, lng = self.get_gps_fast(item.data(ROLE_PATH))
        if lat is not None:
            # 既存のGPS座標があれば、それを選択座標として扱い地図上にマーカー表示する
            # （第3引数falseはJS側からのnotifyを抑制し、Python→JSの一方向更新にするため）
            self.on_map_clicked(lat, lng)
            self.browser.page().runJavaScript(f"updateMarker({lat}, {lng}, false);")
        else:
            self.coord_lbl.setText("GPS未設定")
            self.browser.page().runJavaScript("clearMarker();")

    def on_item_double_clicked(self, item):
        # ダブルクリックで詳細プレビューダイアログをモーダル表示する
        path = item.data(ROLE_PATH)
        PreviewDialog(path, self).exec()

    def on_map_clicked(self, lat, lng):
        # MapBridge.coords_changed シグナルのハンドラ。地図クリック・検索結果の座標を保持し表示を更新する
        self.selected_coords = (lat, lng)
        self.coord_lbl.setText(f"緯度: {lat:.6f}, 経度: {lng:.6f}")

    def get_gps_fast(self, path):
        result = self.et.get_gps(Path(path))
        # ExifToolClient.get_gps がNoneを返す場合は明示的に (None, None) のタプルに揃える
        return result if result is not None else (None, None)

    def closeEvent(self, event):
        # 書き込み処理中にウィンドウを閉じようとした場合は警告を出し、キャンセルできるようにする
        if self.writer and self.writer.isRunning():
            reply = QMessageBox.warning(self, "注意", "書き込み中です。強制終了しますか？",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        # 読み込みスレッドが動作中であれば、終了前に安全に停止させる
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        event.accept()

    def get_html(self):
        # QWebEngineViewに読み込ませる地図画面のHTML/JavaScript一式。
        # Leaflet + OpenStreetMapタイル + 住所検索（Geocoder）を使用し、
        # クリックまたは検索結果の座標をQWebChannel経由でPython側（MapBridge）へ通知する。
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                #map { height: 100vh; width: 100%; margin: 0; }
                body { margin: 0; overflow: hidden; }
                .leaflet-control-geocoder {
                    font-family: 'Yu Gothic', sans-serif;
                    font-size: 14px;
                }
            </style>
            <!-- Leaflet CSS & JS -->
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <!-- Leaflet Control Geocoder (Search) CSS & JS -->
            <link rel="stylesheet" href="https://unpkg.com/leaflet-control-geocoder/dist/Control.Geocoder.css" />
            <script src="https://unpkg.com/leaflet-control-geocoder/dist/Control.Geocoder.js"></script>
            <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
        </head>
        <body>
            <div id="map"></div>
            <script>
                let bridge, map, marker;
                // QWebChannel初期化：Python側のMapBridgeオブジェクトをJS変数 bridge として受け取る
                new QWebChannel(qt.webChannelTransport, (c) => { bridge = c.objects.bridge; });

                document.addEventListener("DOMContentLoaded", function() {
                    // マップの初期化（初期位置：東京駅周辺）
                    map = L.map('map').setView([35.6812, 139.7671], 15);

                    // OpenStreetMapのタイルレイヤーを追加
                    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                        maxZoom: 19,
                        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
                    }).addTo(map);

                    // 検索コントロール（Geocoding）の追加
                    const geocoder = L.Control.geocoder({
                        defaultMarkGeocode: false,
                        placeholder: "住所を検索...",
                        errorMessage: "住所が見つかりませんでした。"
                    }).addTo(map);

                    // 検索結果が選ばれたら、その位置にマーカーを立てて地図を移動する
                    geocoder.on('markgeocode', function(e) {
                        const center = e.geocode.center;
                        updateMarker(center.lat, center.lng, true);
                        map.setView(center, 16);
                    });

                    // マップクリック時のイベント（クリック位置にマーカーを立ててPython側に通知）
                    map.on('click', function(e) {
                        updateMarker(e.latlng.lat, e.latlng.lng, true);
                    });
                });

                // マーカーの作成／移動を行う共通関数。notify=trueのときのみPython側へ座標を通知する
                function updateMarker(lat, lng, notify) {
                    const ll = [lat, lng];
                    if (marker) {
                        marker.setLatLng(ll);
                    } else {
                        marker = L.marker(ll).addTo(map);
                    }
                    map.panTo(ll);
                    if (notify && bridge) {
                        bridge.updateCoords(lat, lng);
                    }
                }

                // 選択が空になったとき等にマーカーを取り除く
                function clearMarker() {
                    if (marker) {
                        map.removeLayer(marker);
                        marker = null;
                    }
                }
            </script>
        </body>
        </html>
        """

    def apply_dark_theme(self):
        # アプリ全体のダークテーマ用スタイルシートを一括適用する（機械的なQSS定義のため簡潔なコメントのみ）
        self.setStyleSheet("""
            QMainWindow, QDialog { background: #1E1E1E; }
            QMenuBar { background: #2D2D2D; color: #DCDCDC; border-bottom: 1px solid #333; }
            QMenuBar::item:selected { background: #3D3D3D; }
            QMenu { background: #2D2D2D; color: #DCDCDC; border: 1px solid #444; }
            QMenu::item:selected { background: #0078D4; }
            QListWidget { background: #1A1A1A; color: #DCDCDC; border: 1px solid #333; border-radius: 4px; }
            QListWidget::item:selected { background: #2D2D2D; border: 1px solid #0078D4; }
            QProgressBar { background: #333; border-radius: 4px; border: none; }
            QProgressBar::chunk { background: #0078D4; border-radius: 4px; }
            QPushButton { background: #333; color: white; border: 1px solid #555; padding: 5px 15px; border-radius: 4px; }
            QPushButton#save_btn { background: #0078D4; border: none; font-weight: bold; font-size: 14px; }
            QPushButton#save_btn:hover { background: #1E90FF; }
            QPushButton#save_btn:disabled { background: #444; color: #888; }
            QLabel { color: #AAA; }
        """)

if __name__ == "__main__":
    try:
        # main.py と同じ共通ロギング設定を使う（画面には WARNING 以上のみ、
        # 詳細は同じ photo_organizer.log に集約）
        setup_logging()
        logger.info("=" * 50)
        logger.info("手動GPS付与ツールを開始します")
        logger.info("=" * 50)

        app = QApplication(sys.argv)
        ex = NefGpsTool()
        ex.show()
        exit_code = app.exec()

        logger.info("=" * 50)
        logger.info("手動GPS付与ツールを終了します")
        logger.info("=" * 50)
        sys.exit(exit_code)

    except Exception as e:
        # 起動処理全体を包んでおき、想定外の例外でも必ずログに残してから終了コード1で抜ける
        logger.critical("予期しないエラーが発生しました", exc_info=True)
        print(f"\n[エラー] {e}")
        sys.exit(1)
