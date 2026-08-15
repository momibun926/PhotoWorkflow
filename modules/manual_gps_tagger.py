import sys
import os
import json
import rawpy
import configparser
import subprocess
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, 
                             QLabel, QSplitter, QAbstractItemView, QFileDialog, QFrame,
                             QProgressBar, QMessageBox, QDialog, QMenuBar)
from PyQt6.QtGui import QPixmap, QImage, QTransform, QIcon, QPainter, QColor, QAction
from PyQt6.QtCore import Qt, QUrl, pyqtSlot, QObject, QSize, QTimer, QThread, pyqtSignal
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

# --- JSON設定の読み込みと書式フォーマット関数 ---
def load_app_config():
    path = "config.json"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

APP_CONFIG = load_app_config()
EXIFTOOL = APP_CONFIG.get("external_tools", {}).get("exiftool", "exiftool")
INIT_DIR = APP_CONFIG.get("directories", {}).get("from_camera", r"C:\SHARE\Photo\from_camera")

def format_technical_terms(text: str) -> str:
    """カメラ・レンズ名の表記を補正する"""
    if not text: return ""
    text = text.replace("Z50_2", "Z50II").replace("Z50 2", "Z50II").replace("Z50ii", "Z50II")
    text = text.replace("dx", "DX").replace("vr", "VR")
    return text

def get_creation_flags():
    """Windows環境でサブプロセス実行時にコンソール画面を出さないフラグ"""
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

# --- 設定管理クラス ---
class ConfigManager:
    def __init__(self):
        self.api_key = ""
        self.load()

    def load(self):
        config = configparser.ConfigParser()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')
        if os.path.exists(path):
            config.read(path, encoding='utf-8')
            self.api_key = config.get('GOOGLE_MAPS', 'API_KEY', fallback="")

# --- バックグラウンド処理（読み込み）用スレッド ---
class LoadWorker(QThread):
    progress = pyqtSignal(int, int, str, object, bool) 

    finished = pyqtSignal(int)

    def __init__(self, directory, thumb_size):
        super().__init__()
        self.directory = directory
        self.thumb_size = thumb_size
        self._is_running = True

    def run(self):
        valid_exts = ('.nef', '.jpg', '.jpeg')
        files = sorted([f for f in os.listdir(self.directory) if f.lower().endswith(valid_exts)])
        total = len(files)
        
        # フォルダ単位で一括してメタデータを取得（ボトルネックの解消）
        meta_dict = self.batch_read_exif(self.directory)
        
        for i, f in enumerate(files):
            if not self._is_running: break
            full_path = os.path.join(self.directory, f)
            norm_path = os.path.normpath(full_path)
            
            meta = meta_dict.get(norm_path, {})
            lat = meta.get('lat')
            lng = meta.get('lng')
            orientation = meta.get('orientation', 1)
            
            # スレッドセーフ確保のため QImage で読み込み
            qimg = self.load_thumbnail(full_path, orientation)
            self.progress.emit(i + 1, total, f, qimg, lat is not None)
            
        self.finished.emit(total)

    def stop(self):
        self._is_running = False

    def batch_read_exif(self, directory):
        cmd = [EXIFTOOL, '-j', '-n', '-Orientation', '-GPSLatitude', '-GPSLongitude', directory]
        res = subprocess.run(cmd, capture_output=True, text=True, creationflags=get_creation_flags())
        meta_dict = {}
        if res.stdout:
            try:
                data = json.loads(res.stdout)
                for item in data:
                    path = os.path.normpath(item.get('SourceFile', ''))
                    meta_dict[path] = {
                        'orientation': item.get('Orientation', 1),
                        'lat': item.get('GPSLatitude'),
                        'lng': item.get('GPSLongitude')
                    }
            except: pass
        return meta_dict

    def load_thumbnail(self, path, orientation):
        is_nef = path.lower().endswith('.nef')
        qimg = None
        if is_nef:
            qimg = self.extract_thumb_fast(path)
        else:
            qimg = QImage(path)

        if qimg and not qimg.isNull():
            qimg = self.apply_rotation(qimg, orientation)
            return qimg.scaled(self.thumb_size, self.thumb_size, 
                            Qt.AspectRatioMode.KeepAspectRatio, 
                            Qt.TransformationMode.SmoothTransformation)
        if is_nef:
            return self.extract_thumb_fallback(path)
        return None

    def apply_rotation(self, qimg, orientation):
        if orientation <= 1: return qimg
        transform = QTransform()
        if orientation == 3: transform.rotate(180)
        elif orientation == 6: transform.rotate(90)
        elif orientation == 8: transform.rotate(270)
        elif orientation == 2: transform.scale(-1, 1)
        elif orientation == 4: transform.scale(1, -1)
        elif orientation == 5: transform.rotate(90).scale(-1, 1)
        elif orientation == 7: transform.rotate(270).scale(-1, 1)
        return qimg.transformed(transform, Qt.TransformationMode.SmoothTransformation)

    def extract_thumb_fast(self, path):
        try:
            cmd = [EXIFTOOL, '-b', '-PreviewImage', path]
            res = subprocess.run(cmd, capture_output=True, creationflags=get_creation_flags())
            if res.stdout:
                qimg = QImage.fromData(res.stdout)
                if not qimg.isNull():
                    return qimg
        except: pass
        return None

    def extract_thumb_fallback(self, path):
        try:
            with rawpy.imread(path) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    return QImage.fromData(thumb.data)
        except: return None

# --- バックグラウンド処理（書き込み）用スレッド ---
class WriteWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal()

    def __init__(self, items_data, lat, lng):
        super().__init__()
        self.items_data = items_data 
        self.lat, self.lng = lat, lng

    def run(self):
        total = len(self.items_data)
        for i, (path, _) in enumerate(self.items_data):
            current = i + 1
            filename = os.path.basename(path)
            self.progress.emit(current, total, filename)
            cmd = [EXIFTOOL, f"-GPSLatitude={self.lat}", f"-GPSLongitude={self.lng}",
                   "-GPSLatitudeRef=N" if self.lat >= 0 else "-GPSLatitudeRef=S",
                   "-GPSLongitudeRef=E" if self.lng >= 0 else "-GPSLongitudeRef=W",
                   "-overwrite_original", path]
            subprocess.run(cmd, capture_output=True, creationflags=get_creation_flags())
        self.finished.emit()

# --- プレビュー表示用ダイアログ ---
class PreviewDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(f"詳細プレビュー: {os.path.basename(path)}")
        self.main_layout = QHBoxLayout(self)
        self.image_label = QLabel("読み込み中...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: #000; border-radius: 4px;")
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
        QTimer.singleShot(100, self.load_content)

    def load_content(self):
        self.load_image(self.path)
        self.load_exif(self.path)

    def load_exif(self, path):
        try:
            tags = ['-Model', '-LensID', '-LensModel', '-ExposureTime', '-FNumber', 
                    '-ISO', '-FocalLength', '-DateTimeOriginal', '-ImageSize']
            cmd = [EXIFTOOL, '-S'] + tags + [path]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', creationflags=get_creation_flags()).stdout
            lines = res.strip().split('\n')
            formatted_text = ""
            for line in lines:
                if ':' in line:
                    key, val = line.split(':', 1)
                    val = format_technical_terms(val.strip())
                    formatted_text += f"<b style='color:#888;'>{key.strip()}:</b><br>{val}<br><br>"
            if not formatted_text:
                formatted_text = "EXIF情報が見つかりませんでした。"
            self.exif_label.setText(formatted_text)
            self.exif_label.setTextFormat(Qt.TextFormat.RichText)
        except Exception as e:
            self.exif_label.setText(f"EXIF取得エラー: {str(e)}")

    def load_image(self, path):
        try:
            cmd_orient = [EXIFTOOL, '-Orientation', '-n', '-S', path]
            res_orient = subprocess.run(cmd_orient, capture_output=True, text=True, creationflags=get_creation_flags()).stdout
            orientation = 1
            if 'Orientation:' in res_orient:
                orientation = int(res_orient.split(': ')[1])
            if path.lower().endswith('.nef'):
                cmd_img = [EXIFTOOL, '-b', '-JpgFromRaw', path]
                res_img = subprocess.run(cmd_img, capture_output=True, creationflags=get_creation_flags())
                if not res_img.stdout:
                    cmd_img = [EXIFTOOL, '-b', '-PreviewImage', path]
                    res_img = subprocess.run(cmd_img, capture_output=True, creationflags=get_creation_flags())
                pix = QPixmap.fromImage(QImage.fromData(res_img.stdout))
            else:
                pix = QPixmap(path)
            if pix and not pix.isNull():
                if orientation > 1:
                    transform = QTransform()
                    if orientation == 3: transform.rotate(180)
                    elif orientation == 6: transform.rotate(90)
                    elif orientation == 8: transform.rotate(270)
                    pix = pix.transformed(transform, Qt.TransformationMode.SmoothTransformation)
                pix = pix.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.image_label.setPixmap(pix)
            else: raise Exception()
        except: self.image_label.setText("プレビューに失敗しました。")

class MapBridge(QObject):
    coords_changed = pyqtSignal(float, float)
    @pyqtSlot(float, float)
    def updateCoords(self, lat, lng): self.coords_changed.emit(lat, lng)

# --- メインウィンドウ ---
class NefGpsTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager()
        self.selected_coords = (0.0, 0.0)
        self.thumb_size = 180
        self.target_sidebar_width = 450
        self.current_dir = ""
        self.worker = None
        self.writer = None
        self.setWindowTitle("Photo GPS Writer (NEF & JPEG)")
        self.set_app_icon()
        self.create_menu_bar()
        self.init_ui()
        self.apply_dark_theme()
        self.setWindowState(Qt.WindowState.WindowMaximized)
        init_path = INIT_DIR if INIT_DIR and os.path.exists(INIT_DIR) else r"C:\SHARE\Photo\from_camera"
        if os.path.exists(init_path): self.start_loading(init_path)

    def set_app_icon(self):
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
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QVBoxLayout(cw)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        left_panel = QFrame()
        left_vbox = QVBoxLayout(left_panel)
        left_panel.setMinimumWidth(self.target_sidebar_width)
        self.path_lbl = QLabel("フォルダを選択してください")
        self.path_lbl.setStyleSheet("background: #252525; padding: 8px; border-radius: 4px; color: #BBB; border: 1px solid #333;")
        left_vbox.addWidget(self.path_lbl)
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
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(self.thumb_size, self.thumb_size))
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setSpacing(10)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.itemSelectionChanged.connect(self.on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        left_vbox.addWidget(self.list_widget)
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
        QTimer.singleShot(500, self.adjust_splitter)

    def adjust_splitter(self):
        self.splitter.setSizes([self.target_sidebar_width, self.width()-self.target_sidebar_width])

    def select_folder(self):
        path = QFileDialog.getExistingDirectory(self, "フォルダを選択")
        if path: self.start_loading(path)

    def start_loading(self, path):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        self.list_widget.clear()
        self.path_lbl.setText(path)
        self.current_dir = path
        self.pbar.setValue(0)
        self.worker = LoadWorker(path, self.thumb_size)
        self.worker.progress.connect(self.add_item_to_list)
        self.worker.finished.connect(lambda count: self.status_lbl.setText(f"完了: {count}枚"))
        self.worker.start()

    def add_item_to_list(self, current, total, filename, qimg, has_gps):
        self.pbar.setMaximum(total)
        self.pbar.setValue(current)
        self.status_lbl.setText(f"読込中 ({current}/{total}): {filename}")
        display_name = f"🚩 {filename}" if has_gps else filename
        item = QListWidgetItem(display_name)
        item.setData(Qt.ItemDataRole.UserRole, os.path.join(self.current_dir, filename))
        
        # メインスレッドで QImage から QPixmap を生成
        if qimg and not qimg.isNull():
            pix = QPixmap.fromImage(qimg)
            canvas = QPixmap(self.thumb_size, self.thumb_size)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            x, y = (self.thumb_size-pix.width())//2, (self.thumb_size-pix.height())//2
            painter.drawPixmap(x, y, pix)
            painter.end()
            item.setIcon(QIcon(canvas))
            
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self.list_widget.addItem(item)

    def start_writing(self):
        selected = self.list_widget.selectedItems()
        if not selected or self.selected_coords == (0.0, 0.0): return
        reply = QMessageBox.question(self, '確認', f'{len(selected)}枚に書き込みますか？',
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        self.pbar.setMaximum(len(selected))
        self.pbar.setValue(0)
        items_data = [(i.data(Qt.ItemDataRole.UserRole), i) for i in selected]
        self.writer = WriteWorker(items_data, *self.selected_coords)
        self.writer.progress.connect(self.on_write_progress)
        self.writer.finished.connect(self.on_write_finished)
        self.btn_save.setEnabled(False)
        self.writer.start()

    def on_write_progress(self, current, total, name):
        self.pbar.setValue(current)
        self.status_lbl.setText(f"書込中 ({current}/{total}): {name}")

    def on_write_finished(self):
        for item in self.list_widget.selectedItems():
            fname = os.path.basename(item.data(Qt.ItemDataRole.UserRole))
            item.setText(f"🚩 {fname}")
        self.status_lbl.setText("書き込み完了")
        self.btn_save.setEnabled(True)
        QMessageBox.information(self, "完了", "書き込み完了。")

    def on_selection_changed(self):
        sel = self.list_widget.selectedItems()
        if not sel:
            self.browser.page().runJavaScript("clearMarker();")
            return
        path = sel[0].data(Qt.ItemDataRole.UserRole)
        lat, lng = self.get_gps_fast(path)
        if lat is not None:
            self.on_map_clicked(lat, lng)
            self.browser.page().runJavaScript(f"updateMarker({lat}, {lng}, false);")
        else:
            self.coord_lbl.setText("GPS未設定")
            self.browser.page().runJavaScript("clearMarker();")

    def on_item_double_clicked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        PreviewDialog(path, self).exec()

    def on_map_clicked(self, lat, lng):
        self.selected_coords = (lat, lng)
        self.coord_lbl.setText(f"緯度: {lat:.6f}, 経度: {lng:.6f}")

    def get_gps_fast(self, path):
        try:
            cmd = [EXIFTOOL, '-GPSLatitude', '-GPSLongitude', '-n', '-S', '-f', path]
            out = subprocess.run(cmd, capture_output=True, text=True, creationflags=get_creation_flags()).stdout
            lat = lng = None
            for l in out.splitlines():
                if 'GPSLatitude:' in l:
                    v = l.split(': ')[1]
                    if v != '-': lat = float(v)
                if 'GPSLongitude:' in l:
                    v = l.split(': ')[1]
                    if v != '-': lng = float(v)
            return lat, lng
        except: return None, None

    def closeEvent(self, event):
        if self.writer and self.writer.isRunning():
            reply = QMessageBox.warning(self, "注意", "書き込み中です。強制終了しますか？",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        event.accept()

    def get_html(self):
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
                        placeholder: "場所を検索...",
                        errorMessage: "場所が見つかりませんでした。"
                    }).addTo(map);

                    geocoder.on('markgeocode', function(e) {
                        const center = e.geocode.center;
                        updateMarker(center.lat, center.lng, true);
                        map.setView(center, 16);
                    });

                    // マップクリック時のイベント
                    map.on('click', function(e) {
                        updateMarker(e.latlng.lat, e.latlng.lng, true);
                    });
                });

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
    app = QApplication(sys.argv)
    ex = NefGpsTool()
    ex.show()
    sys.exit(app.exec())