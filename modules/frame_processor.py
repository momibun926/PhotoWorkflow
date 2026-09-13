"""STEP 3: フレーム付与処理モジュール (modules/frame_processor.py)."""

# 標準ライブラリのインポート
import logging       # ログ出力用
import sys            # コマンドライン引数・終了コード制御用
import os              # パス正規化(normpath)に使用
from pathlib import Path       # ファイル・ディレクトリパス操作
from dataclasses import asdict  # dataclass(PhotoMetadata)を辞書に変換するために使用
from typing import List

# 画像処理ライブラリ Pillow から必要なモジュールをインポート
# Image: 画像の読み込み・生成・保存, ImageDraw: 図形/文字の描画,
# ImageFont: フォント読み込み, ImageOps: EXIF Orientationに基づく自動回転(exif_transpose)
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    # パッケージとして実行された場合（本来の使われ方）
    # 相対importなので、`modules`パッケージの一部としてimportされたときに成功する
    from .exif_utils import PhotoMetadata, ExifReader
    from .config_manager import YamlConfigManager
    from .logging_config import setup_logging
    from . import constants
except ImportError:
    # python frame_processor.py として単独実行された場合のフォールバック
    # （manual_gps_tagger.py / exif_exporter.py と同じパターン）
    # 相対importが使えない（パッケージの外から直接実行された）場合は、
    # 同じディレクトリにあるモジュールとして絶対importし直す
    from exif_utils import PhotoMetadata, ExifReader
    from config_manager import YamlConfigManager
    from logging_config import setup_logging
    import constants

# このモジュール用のロガーを取得（main.py側でsetup_logging()済みならその設定を引き継ぐ）
logger = logging.getLogger(__name__)


class ImageProcessor:
    """画像にフレームとEXIF情報を付与するクラス。"""

    def __init__(self, config_path: Path, app_config=None) -> None:
        """初期化。

        Args:
            config_path: YAML形式の設定ファイルパス（フレームのレイアウト設定）
            app_config: ConfigManager（config.json）。exiftoolのパス解決に使用
        """
        # YAML設定ファイルのパスを保持し、存在チェック
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            # 存在しない場合はconstants.pyの定型メッセージを使ってエラーを送出
            raise FileNotFoundError(constants.ERROR_MESSAGES["config_not_found"].format(config_path))

        # YAML設定（フォント/色/レイアウト/出力設定など）を読み込む
        self.config = YamlConfigManager(self.config_path)
        # config.json由来のConfigManager（exiftoolパス解決などに使用、Noneの場合もある）
        self.app_config = app_config
        # EXIF読み取り用のヘルパー（メタデータのバッチ取得に使用）
        self.exif_reader = ExifReader(config=app_config)
        # ロゴ画像ファイルのパス（constants.pyで定義されたファイル名）
        self.logo_path = Path(constants.LOGO_FILENAME)
        logger.info("ImageProcessor 初期化完了: config=%s", config_path)

    def _calculate_font_size(self, long_side: int, config_val: float) -> int:
        """フォントサイズを計算。"""
        # size_typeが"px"ならconfig_valをそのままピクセル数として使う
        if self.config.fonts.size_type == "px":
            return int(config_val)
        # それ以外（比率指定）の場合は、画像の長辺に対する割合でフォントサイズを算出
        return int(long_side * config_val)

    def _get_font(self, font_list: List[str], size: int) -> ImageFont.FreeTypeFont:
        """フォントを取得。"""
        # 候補のフォント名を順番に試し、最初に読み込めたものを返す（フォールバック方式）
        for font_name in font_list:
            try:
                return ImageFont.truetype(font_name, size)
            except OSError as e:
                # そのフォントが見つからない/読み込めない場合は次の候補を試す
                logger.debug("フォント読み込み失敗: %s", font_name)
                continue
        # すべての候補が失敗した場合はエラーとして送出
        error_msg = f"指定されたフォントが見つかりません: {font_list}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    def _draw_logo(self, canvas: Image.Image, draw: ImageDraw.ImageDraw,
                  main_fs: int, side_m: int) -> None:
        """ロゴを画像に描画。"""
        # ロゴファイルが存在しなければ何もせず終了（ロゴなしでも処理は継続できる）
        if not self.logo_path.exists():
            return
        try:
            # ロゴ画像を開き、透過情報を保持したRGBAモードに変換
            logo = Image.open(self.logo_path).convert("RGBA")
            # ロゴの高さをメインフォントサイズに対する比率(LOGO_SIZE_RATIO)で決定
            logo_h = int(main_fs * constants.LOGO_SIZE_RATIO)
            # 元画像のアスペクト比を保ったまま幅を計算
            logo_w = int(logo.width * (logo_h / logo.height))
            # 高品質なリサイズ（LANCZOS法）でロゴを縮小/拡大
            logo = logo.resize((logo_w, logo_h), Image.Resampling.LANCZOS)
            # ロゴのX座標（左マージンからのオフセット）
            logo_x = side_m + constants.DEFAULT_LOGO_LEFT_OFFSET
            # ロゴを左下に配置（キャンバス下部）
            # Y座標はキャンバス下端からロゴの高さとオフセット分だけ上にずらして計算
            logo_y = canvas.height - logo_h - constants.DEFAULT_LOGO_TOP_OFFSET
            # アルファチャンネル(logo自身)をマスクとして使い、透過を保ったまま貼り付け
            canvas.paste(logo, (logo_x, logo_y), logo)
            logger.debug("ロゴ描画完了（左下）")
        except Exception as e:
            # ロゴ描画に失敗しても致命的ではないため、警告のみでフレーム処理自体は継続する
            logger.warning("ロゴ描画失敗: %s", e)

    def process_image(self, path: Path, meta: PhotoMetadata, current: int, total: int) -> bool:
        """画像を処理してフレーム付与。"""
        try:
            # 進捗表示用の文字列を作成（[ XX%] (現在/合計) ファイル名）
            progress_pct = int((current / total) * 100)
            progress_str = f"[{progress_pct:3d}%] ({current}/{total}) {path.name}"

            # 画像を開き、EXIFのOrientation情報に基づいて向きを正しく補正した上でRGBに変換
            # (with構文でファイルハンドルはブロックを抜けると自動的に閉じられる)
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")

            # PhotoMetadata（dataclass）を文字列フォーマット用に辞書化
            meta_dict = asdict(meta)
            w, h = img.size
            # 長辺（縦横どちらか大きい方）を基準にフォントサイズやマージンを計算する
            long_side = max(w, h)

            # メイン文字列・サブ文字列それぞれのフォントサイズを算出
            main_fs = self._calculate_font_size(long_side, self.config.fonts.main_size)
            sub_fs = self._calculate_font_size(long_side, self.config.fonts.sub_size)
            # 太字フォント（メイン用）・通常フォント（サブ用）を取得
            f_main = self._get_font(self.config.fonts.bold, main_fs)
            f_sub = self._get_font(self.config.fonts.regular, sub_fs)

            # 左右マージン・下部マージンを長辺に対する比率から算出
            side_m = int(long_side * self.config.ratios.side_margin)
            bott_m = int(long_side * self.config.ratios.bottom_margin)
            # フレーム全体（余白込み）のキャンバスサイズを計算
            canvas_w = w + side_m * 2
            canvas_h = h + side_m + bott_m

            # 背景色で塗りつぶした新しいキャンバスを作成し、元画像を中央上部に貼り付け
            canvas = Image.new("RGB", (canvas_w, canvas_h), self.config.colors.bg)
            canvas.paste(img, (side_m, side_m))
            draw = ImageDraw.Draw(canvas)

            # YAML設定のレイアウト文字列テンプレートにEXIF情報を埋め込んでテキストを生成
            # （例: "{camera} / {lens}" のような文字列に meta_dict の値を差し込む）
            text_top = self.config.layout.top.format(**meta_dict)
            text_bottom = self.config.layout.bottom.format(**meta_dict)

            # 下部余白エリアの開始Y座標
            margin_area_y = h + side_m
            # 任意設定項目（未設定ならデフォルト値を使用）
            top_padding_ratio = getattr(self.config.layout, "top_padding_ratio", 0.20)
            y_offset_px = getattr(self.config.layout, "y_offset_px", 0)
            line_spacing_px = getattr(self.config.layout, "line_spacing_px", 40)

            # 1行目（メイン文字列）と2行目（サブ文字列）のY座標を計算
            y_top_line = margin_area_y + int(bott_m * top_padding_ratio) + y_offset_px
            y_bottom_line = y_top_line + line_spacing_px

            # ロゴを描画（メイン文字列の描画より先に行うことで文字が前面に来る）
            self._draw_logo(canvas, draw, main_fs, side_m)

            # メイン文字列の描画：textbboxで描画時の幅を取得し、中央揃えになるようX座標を計算
            tw_top = draw.textbbox((0, 0), text_top, font=f_main)[2]
            text_top_x = (canvas_w - tw_top) // 2
            draw.text((text_top_x, y_top_line), text_top, fill=self.config.colors.main, font=f_main)

            # サブ文字列も同様に中央揃えで描画
            tw_bottom = draw.textbbox((0, 0), text_bottom, font=f_sub)[2]
            text_bottom_x = (canvas_w - tw_bottom) // 2
            draw.text((text_bottom_x, y_bottom_line), text_bottom, fill=self.config.colors.sub, font=f_sub)

            # 出力先ディレクトリ（元画像と同じ場所の下にoutput.dir_nameで指定されたフォルダ）を作成
            out_dir = path.parent / self.config.output.dir_name
            out_dir.mkdir(exist_ok=True, parents=True)
            # 出力ファイル名は prefix + 元のファイル名(拡張子なし) + .jpg
            out_path = out_dir / f"{self.config.output.prefix}{path.stem}.jpg"

            # 出力品質はYAMLの output.quality を優先し、未指定ならconstants.pyのデフォルトを使用。
            # ※以前はここが常に constants.JPEG_QUALITY 固定になっており、
            #   standard.yaml/black.yaml の quality: 100 設定が反映されないバグがあった。
            quality = getattr(self.config.output, "quality", constants.JPEG_QUALITY)
            subsampling = getattr(self.config.output, "subsampling", constants.JPEG_SUBSAMPLING)

            # JPEGとして保存（品質・クロマサブサンプリング設定を反映）
            canvas.save(out_path, "JPEG", quality=quality, subsampling=subsampling)

            print(progress_str)
            logger.info("画像処理完了: %s -> %s", path.name, out_path)
            return True

        except Exception as e:
            # 1枚の処理失敗が全体を止めないよう、例外はここで捕捉してFalseを返す
            logger.error("画像処理エラー (%s): %s", path.name, e, exc_info=True)
            print(f" [エラー] {path.name}: {e}")
            return False

    def run(self, target: Path) -> int:
        """対象ディレクトリの画像をすべて処理。"""
        # 対象パスが存在しなければエラー
        if not target.exists():
            raise FileNotFoundError(constants.ERROR_MESSAGES["directory_not_found"].format(target))

        # targetがファイル単体ならそれだけを、ディレクトリならその直下のファイル一覧を対象にする
        if target.is_file():
            files = [target]
        else:
            files = [f for f in target.iterdir() if f.is_file()]

        # 有効な画像拡張子の一覧。app_configがあればconfig.jsonの設定を優先して使う
        valid_extensions = constants.VALID_IMAGE_EXTENSIONS
        if self.app_config is not None:
            try:
                # JPEG系・RAW系の拡張子に加えてPNGも許可する
                ext_map = self.app_config.get_file_extensions()
                valid_extensions = ext_map["jpeg"] | ext_map["raw"] | {".png"}
            except AttributeError:
                # app_configにget_file_extensionsメソッドが無い（想定外のオブジェクト）場合は
                # constants.py側のデフォルト拡張子セットにフォールバック
                logger.debug("app_configにget_file_extensionsが無いため、デフォルト拡張子を使用")

        # 拡張子でフィルタして処理対象ファイルを絞り込む
        valid_files = [f for f in files if f.suffix.lower() in valid_extensions]

        if not valid_files:
            logger.warning("有効な画像ファイルが見つかりません: %s", target)
            print("有効な画像ファイルが見つかりません。")
            return 0

        # EXIFメタデータを1枚ずつではなく一括で取得（exiftool呼び出し回数を減らし高速化するため）
        logger.info("メタデータ一括取得開始: %d ファイル", len(valid_files))
        print(" -> メタデータを一括取得中...")
        metadata_map = self.exif_reader.get_photo_metadata_batch(valid_files)

        success_count = 0
        total_files = len(valid_files)
        for i, file_path in enumerate(valid_files, 1):
            # metadata_mapのキーはos.path.normpathで正規化されたパス文字列なので、
            # 検索時も同じ正規化を行って一致させる
            norm_path = os.path.normpath(str(file_path))
            meta = metadata_map.get(norm_path)

            if not meta:
                # メタデータが取得できなかったファイルはスキップ（フレーム描画に必要な情報がないため）
                logger.warning("メタデータが取得できません: %s", file_path.name)
                print(f" [警告] {file_path.name}: メタデータが取得できませんでした")
                continue

            # 1枚ずつフレーム付与処理を実行し、成功した枚数をカウント
            if self.process_image(file_path, meta, i, total_files):
                success_count += 1

        logger.info("画像処理完了: %d/%d ファイル", success_count, total_files)
        return success_count


def run_frame_processing(script_path_or_unused: Path, target_dir: Path, yaml_config: str, app_config=None) -> bool:
    """フレーム付与処理を実行するエントリーポイント。

    Args:
        app_config: ConfigManager（config.json）。exiftoolのパス解決に使用
    """
    print("==================================================")
    print(" 【開始】STEP 3: フレーム付与処理")
    print("==================================================")

    # YAML設定ファイルの絶対パスを解決
    conf_path = Path(yaml_config).resolve()

    try:
        # ImageProcessorを生成し、対象ディレクトリ配下の画像をまとめて処理
        processor = ImageProcessor(conf_path, app_config=app_config)
        success_count = processor.run(target_dir)

        # 処理結果のサマリーを表示
        print("\n")
        print(" 【処理のサマリー】")
        print(f"    ・フレーム付与完了: {success_count} ファイル")
        print("\n")
        print("==================================================")
        print(" 【終了】STEP 3: フレーム付与処理 終了")
        print("==================================================")
        # 1件以上成功していればTrue（呼び出し元main.pyのワークフロー継続判定に使われる）
        return success_count > 0

    except FileNotFoundError as e:
        # 設定ファイルや対象ディレクトリが見つからない場合のエラー
        logger.error("ファイル不見エラー: %s", e)
        print(f" -> [エラー] {e}")
        return False
    except Exception as e:
        # 想定外の例外はここでまとめて捕捉し、ワークフロー全体を落とさずFalseを返す
        logger.error("フレーム処理エラー: %s", e, exc_info=True)
        print(f"\n -> [エラー] フレーム処理中に予期しないエラーが発生しました: {e}")
        return False


# このファイルを単独で実行した場合のエントリーポイント
# （通常はmain.pyから run_frame_processing() として呼び出されるが、
#   このモジュール単体でも動作確認できるようにしてある）
if __name__ == "__main__":
    setup_logging()

    # 引数チェック：画像パス（ファイルまたはディレクトリ）は必須
    if len(sys.argv) < 2:
        print("使用法: python frame_processor.py <image_path_or_dir> [config_path]")
        sys.exit(1)

    # 第1引数：処理対象の画像ファイルまたはディレクトリ
    input_path = Path(sys.argv[1]).resolve()
    # 第2引数（省略可）：YAML設定ファイルパス。省略時はカレントディレクトリのstandard.yamlを使用
    conf_path = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) > 2
        else Path("standard.yaml").resolve()
    )

    try:
        processor = ImageProcessor(conf_path)
        processor.run(input_path)
    except Exception as e:
        logger.error("エラー: %s", e, exc_info=True)
        print(f"エラー: {e}")
        sys.exit(1)
