# -*- coding: utf-8 -*-
"""屏幕区域实时文字朗读工具 - 主程序入口

运行方式:
    python main.py          # 前台运行（带日志输出）
    pythonw main.py         # 后台静默运行
"""
import ctypes
import logging
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from modules.capture import ScreenCapture
from modules.config_manager import ConfigManager
from modules.frame_filter import phash, hamming
from modules.hotkeys import register as register_hotkeys
from modules.ocr_engine import OCRReader
from modules.qplayer import QtPlayer
from modules.reader import Reader
from modules.region_selector import RegionSelector, RegionBorder, AdjustDialog, SettingsDialog
from modules.speech import Speech
from modules.text_processor import TextProcessor
from modules.tray_control import create_tray

APP_TITLE = "屏幕区域实时朗读器"


def setup_logger(level="INFO"):
    logger = logging.getLogger("screen_reader")
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_reader.log"),
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def set_dpi_awareness():
    """声明 Per-Monitor V2 DPI 感知，保证 Qt 坐标与 dxcam 物理像素一致。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


class Controller(QObject):
    """核心控制器：串联各模块，负责跨线程调度与生命周期管理。"""

    # Qt 信号：托盘/热键回调运行在后台线程，经信号投递到主线程操作 GUI
    sig_open_selector = Signal()
    sig_open_adjust = Signal()
    sig_open_settings = Signal()
    sig_set_border = Signal(object)  # region tuple 或 None
    sig_quit = Signal()

    def __init__(self, cfg, logger):
        super().__init__()
        self.cfg = cfg
        self.logger = logger
        self._icon = None
        self.selector = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        # 业务模块
        self.ocr = OCRReader(logger)
        self.processor = TextProcessor(cfg, logger)
        self.speech = Speech(cfg, logger)
        # Qt 原生音频播放器（QMediaPlayer，主线程创建），规避 pygame/Qt 崩溃问题
        self.qt_player = QtPlayer(logger)
        self.speech.set_qt_player(self.qt_player)
        self.reader = Reader(self.speech, cfg, logger, processor=self.processor)

        # 监听区域边框（主线程创建）
        self.border = RegionBorder(cfg)
        self.border.hide()

        self.sig_open_selector.connect(self._on_open_selector)
        self.sig_open_adjust.connect(self._on_open_adjust)
        self.sig_open_settings.connect(self._on_open_settings)
        self.sig_set_border.connect(self._on_set_border)
        self.sig_quit.connect(self._on_quit)

    # ---------- 线程安全请求（供托盘/热键调用） ----------
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def request_toggle_monitor(self):
        if self.running:
            self.request_stop_monitor()
        else:
            self.request_start_monitor()

    def request_start_monitor(self):
        with self._lock:
            if self.running:
                return
            if self.cfg.region[2] <= 0 or self.cfg.region[3] <= 0:
                self.logger.warning("尚未设置监听区域，请先按 Ctrl+Alt+S 框选")
                self.request_open_selector()
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True,
                                            name="monitor")
            self._thread.start()
        self.logger.info("开始监听区域 %s", self.cfg.region)

    def request_stop_monitor(self):
        with self._lock:
            if not self.running:
                return
            self._stop_event.set()
        self.logger.info("已停止监听")

    def request_open_selector(self):
        self.sig_open_selector.emit()

    def request_open_adjust(self):
        if self.cfg.region[2] <= 0 or self.cfg.region[3] <= 0:
            self.logger.warning("请先框选监听区域")
            return
        self.sig_open_adjust.emit()

    def request_open_settings(self):
        self.sig_open_settings.emit()

    def request_test(self):
        self.reader.push("语音朗读测试，一切正常")
        self.logger.info("朗读测试已加入队列")

    def request_quit(self):
        self.logger.info("正在退出…")
        self.request_stop_monitor()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
        self.sig_quit.emit()

    # ---------- Qt 主线程槽 ----------
    def _on_open_selector(self):
        if self.selector is not None:
            return
        self.sig_set_border.emit(None)  # 框选时隐藏旧边框
        self.selector = RegionSelector()
        self.selector.region_selected.connect(self._on_region_selected)
        self.selector.show()

    def _on_region_selected(self, region):
        self.cfg.set_region(region)            # 区域仅在本次运行有效，不写入 config.json
        self.selector = None
        self.sig_set_border.emit(region)
        self.logger.info("已更新监听区域: %s", region)

    def _on_open_adjust(self):
        dlg = AdjustDialog(self.cfg.region)
        dlg.region_applied.connect(self._on_region_selected)
        dlg.exec()

    def _on_open_settings(self):
        dlg = SettingsDialog(self.cfg)
        dlg.exec()

    def _on_set_border(self, region):
        if region:
            self.border.set_region(region)
            self.border.show()
        else:
            self.border.hide()

    def _on_quit(self):
        QApplication.instance().quit()

    # ---------- 监听主循环（后台线程） ----------
    def _monitor_loop(self):
        cap = ScreenCapture(self.cfg, self.logger)
        prev_hash = None
        warmup = bool(self.cfg.get("warmup_on_start", True))
        # 强制 OCR 间隔：即使画面静止（pHash 无变化），也周期性强制识别一次，
        # 避免聊天消息出现时恰好未被帧差触发而漏采（新增文本会经去重过滤，不会重复朗读）。
        force_interval = float(self.cfg.get("force_ocr_interval", 2.0))
        last_force = time.time()
        try:
            while not self._stop_event.is_set():
                interval = 1.0 / max(0.5, float(self.cfg.get("fps", 3.0)))
                frame = cap.grab()
                if frame is None:
                    self._stop_event.wait(interval)
                    continue

                cur_hash = phash(frame)
                threshold = int(self.cfg.get("phash_threshold", 10))
                now = time.time()
                # 画面变化超过阈值，或到达强制 OCR 间隔，触发一次识别
                changed = prev_hash is None or hamming(cur_hash, prev_hash) >= threshold
                forced = now - last_force >= force_interval
                if changed or forced:
                    prev_hash = cur_hash
                    last_force = now
                    lines = self.ocr.recognize(frame)
                    if warmup:
                        # 预热：把当前画面已有文本记入历史，不朗读
                        for line in lines:
                            self.processor.seed(line)
                        warmup = False
                        self.logger.info("预热完成，开始增量监听")
                    else:
                        for text in self.processor.process(lines):
                            self.logger.info("[新文本] %s", text)
                            self.reader.push(text)

                self._stop_event.wait(interval)
        except Exception as e:
            self.logger.exception("监听线程异常终止: %s", e)
        finally:
            self.reader.clear_queue()
            self.logger.info("已停止监听")


def main():
    set_dpi_awareness()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = ConfigManager(os.path.join(base_dir, "config.json"))
    cfg.reset_region()                       # 每次启动强制重新框选，区域不跨会话保存
    logger = setup_logger(cfg.get("log_level", "INFO"))

    controller = Controller(cfg, logger)

    # 系统托盘
    try:
        icon = create_tray(controller)
        controller._icon = icon
        icon.run_detached()
    except Exception as e:
        logger.error("托盘启动失败（不影响热键使用）: %s", e)

    # 全局快捷键
    register_hotkeys(controller, cfg, logger)

    if cfg.get("auto_start", False) and cfg.region[2] > 0 and cfg.region[3] > 0:
        controller.request_start_monitor()

    logger.info("%s 已启动。Ctrl+Alt+S 框选区域，Ctrl+Alt+R 开始/停止监听", APP_TITLE)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
