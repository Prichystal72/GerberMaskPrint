# Main file of the PySide6 application for copying a PCB Gerber motif onto A4

import sys
import os
import json
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QPushButton, QVBoxLayout, QHBoxLayout,
                               QGridLayout, QFileDialog, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
                               QMessageBox, QLabel, QComboBox, QCheckBox, QGroupBox, QListWidget,
                               QScrollArea, QMenu, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QToolButton, QGraphicsDropShadowEffect, QFrame)
from PySide6.QtGui import QPixmap, QImage, QPainter, QPageSize, QPageLayout, QPen, QColor, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtCore import Qt, QRectF, QRect, QPoint, QPointF, QSize, QTimer
from pygerber.gerberx3.api.v2 import GerberFile, Project, ColorScheme, FileTypeEnum
from pygerber.common.rgba import RGBA
import tempfile

# Supported paper formats
PAPER_FORMATS = {
    "A4": (210, 297),
    "A3": (297, 420),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6)
}

# Size of the printed calibration reference rectangle. Close to a single Eurocard PCB
# (DIN 41494, 100 x 160 mm) but capped at 150 mm on the long side so the whole thing
# still fits within the range of a standard 150 mm digital caliper - a 160 mm reference
# can't actually be measured end-to-end with the caliper most people have.
CALIBRATION_WIDTH_MM = 100
CALIBRATION_HEIGHT_MM = 150

DPI = 1200

STYLESHEET = """
QMainWindow, QWidget, QScrollArea, QScrollArea > QWidget > QWidget {
    background-color: #f5f6f8;
    color: #1a1d23;
    font-size: 13px;
}
QGroupBox {
    font-weight: 600;
    border: 1px solid #d0d5dd;
    border-radius: 8px;
    margin-top: 10px;
    padding: 10px 6px 6px 6px;
    background-color: #ffffff;
    color: #1a1d23;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #1a1d23;
}
QLabel {
    color: #1a1d23;
    background-color: transparent;
}
QPushButton {
    background-color: #eceff2;
    border: 1px solid #c3c9d1;
    border-radius: 6px;
    padding: 8px 12px;
    text-align: left;
    color: #1a1d23;
}
QPushButton:hover {
    background-color: #dfe3e8;
    border-color: #98a2b3;
}
QPushButton:pressed {
    background-color: #cfd4db;
}
QPushButton:disabled {
    color: #98a2b3;
    background-color: #eceff2;
}
QPushButton#primaryButton {
    background-color: #3b82f6;
    border: 1px solid #2563eb;
    color: white;
    font-weight: 600;
}
QPushButton#primaryButton:hover {
    background-color: #2f6fe0;
}
QPushButton#primaryButton:pressed {
    background-color: #2563eb;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #dfe3e8;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #3b82f6;
    width: 16px;
    margin: -6px 0;
    border-radius: 8px;
}
QComboBox, QListWidget {
    border: 1px solid #c3c9d1;
    border-radius: 6px;
    padding: 4px;
    background-color: white;
    color: #1a1d23;
}
QComboBox QAbstractItemView {
    background-color: white;
    color: #1a1d23;
    selection-background-color: #3b82f6;
    selection-color: white;
}
QListWidget::item:selected {
    background-color: #3b82f6;
    color: white;
}
QStatusBar {
    background-color: #eceff2;
    color: #1a1d23;
}
"""

class MotifItem(QGraphicsPixmapItem):
    def __init__(self, pixmap):
        super().__init__(pixmap)
        self.setFlags(QGraphicsPixmapItem.ItemIsMovable | QGraphicsPixmapItem.ItemIsSelectable)
        self.original_pixmap = pixmap
        self.is_mirrored = False

    def mirror_horizontal(self):
        """Mirrors the motif horizontally"""
        if self.is_mirrored:
            # Revert back to the original
            self.setPixmap(self.original_pixmap)
        else:
            # Mirror
            transform = QTransform()
            transform.scale(-1, 1)  # Mirror across the vertical axis
            mirrored = self.original_pixmap.transformed(transform)
            self.setPixmap(mirrored)
        self.is_mirrored = not self.is_mirrored

    def rotate_90(self):
        """Rotates the motif by 90° (independent of the calibration scale in transform())"""
        self.setRotation((self.rotation() + 90) % 360)

class CanvasView(QGraphicsView):
    def __init__(self, scene, main_window):
        super().__init__(scene)
        self.main_window = main_window
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setSceneRect(QRectF(0, 0, 1000, 1000))  # Temporary, will be recalculated
        self.zoom_factor = 1.0

    def wheelEvent(self, event):
        # Zoom via mouse wheel
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor

        if event.angleDelta().y() > 0:
            zoom_factor = zoom_in_factor
            self.zoom_factor *= zoom_factor
        else:
            zoom_factor = zoom_out_factor
            self.zoom_factor *= zoom_factor

        self.scale(zoom_factor, zoom_factor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.main_window.delete_selected()
        else:
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.main_window.update_selection_toolbar()

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if isinstance(item, MotifItem):
            if not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)

            menu = QMenu(self)
            menu.addAction("↔️ Mirror", self.main_window.mirror_selected)
            menu.addAction("🔄 Rotate 90°", self.main_window.rotate_selected_90)
            menu.addSeparator()
            menu.addAction("📄 Duplicate", self.main_window.duplicate_selected)
            menu.addSeparator()
            menu.addAction("⬆️ Bring to Front", self.main_window.bring_to_front)
            menu.addAction("⬇️ Send to Back", self.main_window.send_to_back)
            menu.addSeparator()
            properties_action = menu.addAction("⚙️ Properties...", self.main_window.show_properties_dialog)
            properties_action.setEnabled(len(self.scene().selectedItems()) == 1)
            menu.addSeparator()
            menu.addAction("🗑️ Delete", self.main_window.delete_selected)
            menu.exec(event.globalPos())
        else:
            super().contextMenuEvent(event)

class MotifPropertiesDialog(QDialog):
    """Dialog for manually fine-tuning the position/rotation/mirroring of a single motif.

    The X/Y position is shown and entered in mm purely for user convenience - the
    conversion (mm <-> px) uses the DPI constant only for display and for writing
    the position back. It does NOT touch the scale_factor_x/y calibration or the
    dpmm calculation in render_gerber.
    """
    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item

        # Remember the original state so Cancel/closing the dialog can restore it
        self._initial_pos = item.pos()
        self._initial_rotation = item.rotation()
        self._initial_mirrored = item.is_mirrored

        self.setWindowTitle("Motif Properties")

        layout = QFormLayout()

        pos = item.pos()
        self.x_spin = QDoubleSpinBox()
        self.x_spin.setRange(-1000, 1000)
        self.x_spin.setSuffix(" mm")
        self.x_spin.setDecimals(2)
        self.x_spin.setValue(pos.x() / DPI * 25.4)

        self.y_spin = QDoubleSpinBox()
        self.y_spin.setRange(-1000, 1000)
        self.y_spin.setSuffix(" mm")
        self.y_spin.setDecimals(2)
        self.y_spin.setValue(pos.y() / DPI * 25.4)

        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(0, 359.9)
        self.rotation_spin.setSuffix(" °")
        self.rotation_spin.setDecimals(1)
        self.rotation_spin.setValue(item.rotation())

        self.mirror_checkbox = QCheckBox("Mirrored horizontally")
        self.mirror_checkbox.setChecked(item.is_mirrored)

        layout.addRow("Position X:", self.x_spin)
        layout.addRow("Position Y:", self.y_spin)
        layout.addRow("Rotation:", self.rotation_spin)
        layout.addRow(self.mirror_checkbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.setLayout(layout)

        # Connect signals last so setting initial values above doesn't trigger a live update early
        self.x_spin.valueChanged.connect(self._live_update)
        self.y_spin.valueChanged.connect(self._live_update)
        self.rotation_spin.valueChanged.connect(self._live_update)
        self.mirror_checkbox.toggled.connect(self._live_update)

    def _live_update(self, *_args):
        self.item.setPos(self.x_spin.value() / 25.4 * DPI, self.y_spin.value() / 25.4 * DPI)
        self.item.setRotation(self.rotation_spin.value())
        if self.mirror_checkbox.isChecked() != self.item.is_mirrored:
            self.item.mirror_horizontal()
        self._refresh_selection_toolbar()

    def _refresh_selection_toolbar(self):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "update_selection_toolbar"):
            main_window.update_selection_toolbar()

    def reject(self):
        # Cancel (or closing the dialog) restores the motif to how it was before opening it
        self.item.setPos(self._initial_pos)
        self.item.setRotation(self._initial_rotation)
        if self.item.is_mirrored != self._initial_mirrored:
            self.item.mirror_horizontal()
        self._refresh_selection_toolbar()
        super().reject()


class CalibrationDialog(QDialog):
    """Lets the user enter what they actually measured on a printed calibration sheet
    and computes the refined scale_factor_x/y from it.

    The calibration sheet is always printed WITH the current scale_factor_x/y already
    applied, so the measured result already reflects it - the correction is folded in
    multiplicatively rather than replacing it outright:
    new_scale = current_scale * (nominal_length_mm / measured_length_mm).
    This is an iterative process: print, measure, apply, and repeat - each round
    refines the previous value instead of resetting it, so it converges over a few
    rounds even if the first guess (or the previous saved value) was off.
    """
    def __init__(self, nominal_x_mm, nominal_y_mm, current_scale_x, current_scale_y, parent=None):
        super().__init__(parent)
        self.nominal_x_mm = nominal_x_mm
        self.nominal_y_mm = nominal_y_mm
        self.current_scale_x = current_scale_x
        self.current_scale_y = current_scale_y
        self.setWindowTitle("Apply Calibration")

        layout = QFormLayout()

        info = QLabel(
            f"Enter exactly what you measured on the printed calibration sheet with a ruler or calipers.\n\n"
            f"Nominal horizontal reference: {nominal_x_mm:.1f} mm (printed with X={current_scale_x:.4f})\n"
            f"Nominal vertical reference: {nominal_y_mm:.1f} mm (printed with Y={current_scale_y:.4f})"
        )
        info.setWordWrap(True)
        layout.addRow(info)

        self.measured_x_spin = QDoubleSpinBox()
        self.measured_x_spin.setRange(1.0, 2000.0)
        self.measured_x_spin.setDecimals(2)
        self.measured_x_spin.setSuffix(" mm")
        self.measured_x_spin.setValue(nominal_x_mm)

        self.measured_y_spin = QDoubleSpinBox()
        self.measured_y_spin.setRange(1.0, 2000.0)
        self.measured_y_spin.setDecimals(2)
        self.measured_y_spin.setSuffix(" mm")
        self.measured_y_spin.setValue(nominal_y_mm)

        layout.addRow("Measured horizontal length:", self.measured_x_spin)
        layout.addRow("Measured vertical length:", self.measured_y_spin)

        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addRow(self.result_label)

        self.measured_x_spin.valueChanged.connect(self._update_preview)
        self.measured_y_spin.valueChanged.connect(self._update_preview)
        self._update_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.setLayout(layout)

    @property
    def new_scale_x(self):
        return self.current_scale_x * self.nominal_x_mm / self.measured_x_spin.value()

    @property
    def new_scale_y(self):
        return self.current_scale_y * self.nominal_y_mm / self.measured_y_spin.value()

    def _update_preview(self):
        self.result_label.setText(
            f"New scale_factor_x = {self.new_scale_x:.4f}\nNew scale_factor_y = {self.new_scale_y:.4f}"
        )


class SelectionToolbar(QFrame):
    """Small floating panel with quick actions that appears above the current selection on the canvas."""
    def __init__(self, view, main_window):
        super().__init__(view)
        self.view = view
        self.main_window = main_window
        self.setObjectName("selectionToolbar")
        self.setStyleSheet(
            "#selectionToolbar { background-color: rgba(30, 41, 59, 235); border-radius: 10px; }"
            "QToolButton { color: white; border: none; padding: 5px 8px; font-size: 15px; }"
            "QToolButton:hover { background-color: rgba(255, 255, 255, 40); border-radius: 6px; }"
        )

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 120))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        actions = [
            ("↔️", main_window.mirror_selected, "Mirror"),
            ("🔄", main_window.rotate_selected_90, "Rotate 90°"),
            ("📄", main_window.duplicate_selected, "Duplicate"),
            ("⚙️", main_window.show_properties_dialog, "Properties"),
            ("🗑️", main_window.delete_selected, "Delete"),
        ]
        for label, handler, tooltip in actions:
            btn = QToolButton()
            btn.setText(label)
            btn.setToolTip(tooltip)
            btn.clicked.connect(handler)
            layout.addWidget(btn)

        self.setLayout(layout)
        self.adjustSize()

    def reposition(self, items):
        if not items:
            self.hide()
            return

        combined_rect = None
        for item in items:
            rect = item.sceneBoundingRect()
            combined_rect = rect if combined_rect is None else combined_rect.united(rect)

        top_left_view = self.view.mapFromScene(combined_rect.topLeft())
        top_right_view = self.view.mapFromScene(combined_rect.topRight())

        self.adjustSize()
        x = (top_left_view.x() + top_right_view.x()) / 2 - self.width() / 2
        y = top_left_view.y() - self.height() - 8

        x = max(0, min(x, self.view.viewport().width() - self.width()))
        y = max(0, y)

        self.move(int(x), int(y))
        self.show()
        self.raise_()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GerberMaskPrint")
        self.setGeometry(100, 100, 1200, 800)
        self.motif_pixmap = None
        self.gerber_files = []
        self.layer_stack = None
        self.current_paper_format = "A4"
        self.original_img_path = None
        self.threshold_value = 5
        self.page_border_item = None
        self.threshold_timer = QTimer(self)
        self.threshold_timer.setSingleShot(True)
        self.threshold_timer.timeout.connect(self.apply_threshold_debounced)
        self.init_ui()
        self.update_canvas_size()

    def init_ui(self):
        # Left control panel
        left_panel = QWidget()
        left_layout = QVBoxLayout()

        # Gerber file loading section
        gerber_group = QGroupBox("Gerber Files")
        gerber_layout = QVBoxLayout()

        load_gerber_btn = QPushButton("📁 Load Gerber Files")
        load_gerber_btn.clicked.connect(self.load_gerber_files)

        self.gerber_list = QListWidget()
        self.gerber_list.setMaximumHeight(150)

        render_btn = QPushButton("🖼️ Render Motifs")
        render_btn.setObjectName("primaryButton")
        render_btn.clicked.connect(self.render_gerber)

        # Threshold slider
        threshold_label = QLabel(f"Black/white threshold: {self.threshold_value}")
        self.threshold_label = threshold_label

        from PySide6.QtWidgets import QSlider
        threshold_slider = QSlider(Qt.Horizontal)
        threshold_slider.setMinimum(0)
        threshold_slider.setMaximum(255)
        threshold_slider.setValue(self.threshold_value)
        threshold_slider.valueChanged.connect(self.on_threshold_changed)
        self.threshold_slider = threshold_slider

        gerber_layout.addWidget(load_gerber_btn)
        gerber_layout.addWidget(QLabel("Loaded files:"))
        gerber_layout.addWidget(self.gerber_list)
        gerber_layout.addWidget(render_btn)
        gerber_layout.addWidget(threshold_label)
        gerber_layout.addWidget(threshold_slider)
        gerber_group.setLayout(gerber_layout)

        # Paper format section
        paper_group = QGroupBox("Paper Format")
        paper_layout = QVBoxLayout()

        self.paper_combo = QComboBox()
        self.paper_combo.addItems(list(PAPER_FORMATS.keys()))
        self.paper_combo.currentTextChanged.connect(self.change_paper_format)

        paper_layout.addWidget(QLabel("Format:"))
        paper_layout.addWidget(self.paper_combo)
        paper_group.setLayout(paper_layout)

        # Printer calibration section
        calibration_group = QGroupBox("Printer Calibration")
        calibration_layout = QVBoxLayout()

        calibration_info = QLabel(
            "Your printer's real scale may differ slightly from the nominal one. Print the 100 x 150 mm "
            "reference below (fits a 150 mm caliper), measure it, apply the result, then repeat - each "
            "round refines the calibration until the measured size converges on the nominal one."
        )
        calibration_info.setWordWrap(True)

        print_calibration_btn = QPushButton("📐 Print Calibration Sheet")
        print_calibration_btn.clicked.connect(self.print_calibration_sheet)

        apply_calibration_btn = QPushButton("🎯 Apply Calibration...")
        apply_calibration_btn.clicked.connect(self.apply_calibration)

        self.calibration_status_label = QLabel()
        self.calibration_status_label.setWordWrap(True)
        self._refresh_calibration_status_label()

        calibration_layout.addWidget(calibration_info)
        calibration_layout.addWidget(print_calibration_btn)
        calibration_layout.addWidget(apply_calibration_btn)
        calibration_layout.addWidget(self.calibration_status_label)
        calibration_group.setLayout(calibration_layout)

        # Manipulation buttons
        action_group = QGroupBox("Actions")
        action_layout = QGridLayout()
        action_layout.setSpacing(6)

        add_btn = QPushButton("➕ Add Motif to Page")
        add_btn.setObjectName("primaryButton")
        add_btn.clicked.connect(self.add_motif)

        mirror_btn = QPushButton("↔️ Mirror")
        mirror_btn.clicked.connect(self.mirror_selected)

        rotate_btn = QPushButton("🔄 Rotate 90°")
        rotate_btn.clicked.connect(self.rotate_selected_90)

        duplicate_btn = QPushButton("📄 Duplicate")
        duplicate_btn.clicked.connect(self.duplicate_selected)

        delete_selected_btn = QPushButton("🗑️ Delete")
        delete_selected_btn.clicked.connect(self.delete_selected)

        bring_front_btn = QPushButton("⬆️ To Front")
        bring_front_btn.clicked.connect(self.bring_to_front)

        send_back_btn = QPushButton("⬇️ To Back")
        send_back_btn.clicked.connect(self.send_to_back)

        clear_all_btn = QPushButton("❌ Clear Page")
        clear_all_btn.clicked.connect(self.clear_all)

        zoom_fit_btn = QPushButton("🔍 Fit Page")
        zoom_fit_btn.clicked.connect(self.zoom_fit)

        zoom_in_btn = QPushButton("🔍+ Zoom In")
        zoom_in_btn.clicked.connect(self.zoom_in)

        zoom_out_btn = QPushButton("🔍- Zoom Out")
        zoom_out_btn.clicked.connect(self.zoom_out)

        zoom_reset_btn = QPushButton("🔍= Reset Zoom")
        zoom_reset_btn.clicked.connect(self.zoom_reset)

        action_layout.addWidget(add_btn, 0, 0, 1, 2)
        action_layout.addWidget(mirror_btn, 1, 0)
        action_layout.addWidget(rotate_btn, 1, 1)
        action_layout.addWidget(duplicate_btn, 2, 0)
        action_layout.addWidget(delete_selected_btn, 2, 1)
        action_layout.addWidget(bring_front_btn, 3, 0)
        action_layout.addWidget(send_back_btn, 3, 1)
        action_layout.addWidget(clear_all_btn, 4, 0, 1, 2)
        action_layout.addWidget(zoom_fit_btn, 5, 0, 1, 2)
        action_layout.addWidget(zoom_in_btn, 6, 0)
        action_layout.addWidget(zoom_out_btn, 6, 1)
        action_layout.addWidget(zoom_reset_btn, 7, 0, 1, 2)
        action_group.setLayout(action_layout)

        # Export buttons
        export_group = QGroupBox("Export")
        export_layout = QVBoxLayout()

        export_btn = QPushButton("💾 Export to PDF")
        export_btn.clicked.connect(self.export_pdf)

        export_bmp_btn = QPushButton("🖼️ Export to BMP")
        export_bmp_btn.clicked.connect(self.export_bmp)

        print_btn = QPushButton("🖨️ Print")
        print_btn.setObjectName("primaryButton")
        print_btn.clicked.connect(self.print_page)

        export_layout.addWidget(export_btn)
        export_layout.addWidget(export_bmp_btn)
        export_layout.addWidget(print_btn)
        export_group.setLayout(export_layout)

        # Info label
        self.info_label = QLabel("Load Gerber files from KiCad (F.Mask, Edge.Cuts, etc.)")
        self.info_label.setWordWrap(True)
        self.info_label.setAlignment(Qt.AlignTop)

        # Assemble the left panel
        left_layout.addWidget(gerber_group)
        left_layout.addWidget(paper_group)
        left_layout.addWidget(calibration_group)
        left_layout.addWidget(action_group)
        left_layout.addWidget(export_group)
        left_layout.addWidget(self.info_label)
        left_layout.addStretch()
        left_panel.setLayout(left_layout)
        left_panel.setMaximumWidth(300)

        left_scroll = QScrollArea()
        left_scroll.setWidget(left_panel)
        left_scroll.setWidgetResizable(True)
        left_scroll.setMaximumWidth(320)
        left_scroll.setFrameShape(QFrame.NoFrame)

        # Right panel with the canvas
        self.scene = QGraphicsScene()
        self.scene.selectionChanged.connect(self.update_selection_toolbar)
        self.view = CanvasView(self.scene, self)
        self.view.setBackgroundBrush(Qt.lightGray)

        # Main layout
        main_layout = QHBoxLayout()
        main_layout.addWidget(left_scroll)
        main_layout.addWidget(self.view, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)
        self.statusBar().showMessage("Ready")

        self.selection_toolbar = SelectionToolbar(self.view, self)
        self.selection_toolbar.hide()

    def change_paper_format(self, format_name):
        self.current_paper_format = format_name
        self.update_canvas_size()
        self.info_label.setText(f"Format changed to {format_name}")

    def update_canvas_size(self):
        width_mm, height_mm = PAPER_FORMATS[self.current_paper_format]
        width_px = int(width_mm / 25.4 * DPI)
        height_px = int(height_mm / 25.4 * DPI)

        # Remove and redraw only the page border (motifs already on the canvas are preserved)
        if self.page_border_item is not None:
            self.scene.removeItem(self.page_border_item)
        pen = QPen(QColor(0, 0, 0))
        pen.setWidth(2)
        self.page_border_item = self.scene.addRect(0, 0, width_px, height_px, pen)
        self.page_border_item.setZValue(-1000)
        self.scene.setSceneRect(0, 0, width_px, height_px)
        self.view.setSceneRect(QRectF(0, 0, width_px, height_px))
        self.view.fitInView(0, 0, width_px, height_px, Qt.KeepAspectRatio)

    def zoom_in(self):
        self.view.scale(1.2, 1.2)
        self.view.zoom_factor *= 1.2
        self.update_selection_toolbar()

    def zoom_out(self):
        self.view.scale(1/1.2, 1/1.2)
        self.view.zoom_factor /= 1.2
        self.update_selection_toolbar()

    def zoom_reset(self):
        self.view.resetTransform()
        self.view.zoom_factor = 1.0
        self.update_selection_toolbar()

    def zoom_fit(self):
        width_mm, height_mm = PAPER_FORMATS[self.current_paper_format]
        width_px = int(width_mm / 25.4 * DPI)
        height_px = int(height_mm / 25.4 * DPI)
        self.view.fitInView(0, 0, width_px, height_px, Qt.KeepAspectRatio)
        self.view.zoom_factor = 1.0
        self.info_label.setText(f"View fitted to the full {self.current_paper_format} page")
        self.update_selection_toolbar()

    def load_gerber_files(self):
        # Load the last used directory from JSON
        settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
        last_dir = ""
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                last_dir = data.get("last_gerber_dir", "")
            except Exception:
                last_dir = ""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Gerber Files",
            last_dir,
            "Gerber Files (*.gbr *.gm1 *.gko *.gts *.gbs *.gto *.gbo);;All Files (*.*)"
        )
        if not file_paths:
            return
        # Save the new directory to JSON if it changed
        new_dir = os.path.dirname(file_paths[0])
        if new_dir != last_dir:
            try:
                data = {}
                if os.path.exists(settings_path):
                    with open(settings_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                data["last_gerber_dir"] = new_dir
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except Exception:
                pass
        self.gerber_files = file_paths
        self.gerber_list.clear()
        for path in file_paths:
            self.gerber_list.addItem(os.path.basename(path))
        self.info_label.setText(f"Loaded {len(file_paths)} Gerber files.\nClick 'Render Motifs' to process them.")

    def render_gerber(self):
        if not self.gerber_files:
            QMessageBox.warning(self, "Error", "Please load Gerber files first.")
            return

        try:
            self.info_label.setText("Processing Gerber files with pygerber...")
            QApplication.processEvents()

            print(f"Loading files: {self.gerber_files}")

            # Build a Project from the files
            gerber_files = []
            for file_path in self.gerber_files:
                print(f"Loading: {os.path.basename(file_path)}")
                gerber_files.append(GerberFile.from_file(file_path))

            # Build the project and parse it
            project = Project(gerber_files)
            parsed_project = project.parse()

            print(f"Project parsed, rendering...")

            # Render to PNG at a high DPI
            # IMPORTANT: dpmm must be an int because pygerber uses Decimal internally
            dpmm = round(DPI / 25.4)  # Round to the nearest whole number
            print(f"DEBUG: DPI={DPI}, dpmm={dpmm}, exact dpmm={DPI/25.4:.4f}")

            temp_file = tempfile.mktemp(suffix='.png')
            parsed_project.render_raster(
                temp_file,
                dpmm=dpmm
            )

            print(f"Saved to: {temp_file}")

            # DEBUG: check the dimensions of the rendered image
            from PIL import Image
            test_img = Image.open(temp_file)
            img_width_px, img_height_px = test_img.size
            test_img.close()  # IMPORTANT: close before further use
            img_width_mm = img_width_px / dpmm
            img_height_mm = img_height_px / dpmm
            print(f"DEBUG: Rendered image: {img_width_px}x{img_height_px} px = {img_width_mm:.3f}x{img_height_mm:.3f} mm")

            # Keep a copy of the path to the original image
            self.original_img_path = temp_file + ".original.png"
            import shutil
            shutil.copy(temp_file, self.original_img_path)

            # Apply the threshold
            self.apply_threshold_filter(temp_file)

            # Load as a QPixmap
            if not os.path.exists(temp_file):
                raise Exception(f"PNG file was not created")

            self.motif_pixmap = QPixmap(temp_file)

            if self.motif_pixmap.isNull():
                raise Exception("Failed to load the rendered image")

            self.info_label.setText(
                f"Gerber motifs rendered ({self.motif_pixmap.width()}x{self.motif_pixmap.height()} px)\n"
                f"Loaded {len(gerber_files)} layers. Click 'Add Motif to Page'."
            )
            QMessageBox.information(
                self,
                "Success",
                f"Gerber files were rendered successfully.\n"
                f"Layers: {len(gerber_files)}\n"
                f"Size: {self.motif_pixmap.width()}x{self.motif_pixmap.height()} px\n\n"
                f"Click 'Add Motif to Page' to insert it."
            )

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            print(f"Error while rendering:\n{error_detail}")
            QMessageBox.critical(
                self,
                "Error",
                f"Error while rendering Gerber files:\n\n{str(e)}\n\nSee console for details."
            )
            self.info_label.setText(f"Error: {str(e)}")

    def apply_threshold_filter(self, temp_file):
        """Applies the threshold to the image and updates motif_pixmap"""
        from PIL import Image, ImageOps

        # Load the original image
        img = Image.open(self.original_img_path)

        # Convert to grayscale
        img_gray = img.convert('L')

        # Stretch contrast so the darkest/brightest pixels actually rendered map to 0/255.
        # Different Gerber layer types (mask, silkscreen, copper, ...) get rendered by
        # pygerber in different default colors with very different grayscale luminance
        # (e.g. a silkscreen render can top out around 60/255 instead of 255) - without this,
        # the same threshold value can work for one layer and make another vanish entirely.
        img_gray = ImageOps.autocontrast(img_gray)

        # Apply the threshold - anything above threshold -> white, below -> black
        img_bw = img_gray.point(lambda x: 255 if x >= self.threshold_value else 0)

        # Invert the colors (black -> white, white -> black)
        img_inverted = ImageOps.invert(img_bw)

        # Convert to RGB for a high-quality save
        img_final = img_inverted.convert('RGB')

        # Save at high DPI
        img_final.save(temp_file, dpi=(DPI, DPI), quality=100)

        # Load as a QPixmap
        self.motif_pixmap = QPixmap(temp_file)

        if self.motif_pixmap.isNull():
            raise Exception("Failed to load the rendered image")

        self.info_label.setText(
            f"Gerber motifs rendered ({self.motif_pixmap.width()}x{self.motif_pixmap.height()} px)\n"
            f"Threshold: {self.threshold_value}. Click 'Add Motif to Page'."
        )

    def on_threshold_changed(self, value):
        """Callback for threshold slider changes - the recalculation is debounced so dragging the slider doesn't stutter"""
        self.threshold_value = value
        self.threshold_label.setText(f"Black/white threshold: {value}")
        self.threshold_timer.start(200)

    def apply_threshold_debounced(self):
        """Actually recomputes the threshold filter once the slider has stopped moving"""
        if self.original_img_path and os.path.exists(self.original_img_path):
            QApplication.setOverrideCursor(Qt.BusyCursor)
            try:
                temp_file = self.original_img_path.replace('.original.png', '')
                self.apply_threshold_filter(temp_file)
            except Exception as e:
                print(f"Error applying threshold: {e}")
            finally:
                QApplication.restoreOverrideCursor()

    def add_motif(self):
        if not self.motif_pixmap:
            QMessageBox.warning(self, "Error", "Please load and render Gerber files first.")
            return
        # Load the calibration scale directly from settings.json
        scale_x, scale_y = self._load_scale_factors()
        item = MotifItem(self.motif_pixmap)
        # Apply the calibration scale to the motif
        item.setTransformOriginPoint(self.motif_pixmap.width()/2, self.motif_pixmap.height()/2)
        item.setTransform(QTransform().scale(scale_x, scale_y), True)
        item.setPos(10, 10)
        self.scene.addItem(item)
        self.info_label.setText("Motif added | Drag motifs with the mouse | Ctrl+click to select multiple motifs")

    def mirror_selected(self):
        selected_items = self.scene.selectedItems()
        if not selected_items:
            QMessageBox.information(
                self,
                "Info",
                "First select motifs by clicking them.\n\nTip: Click a motif to select it, Ctrl+click for multiple motifs."
            )
            return

        count = 0
        for item in selected_items:
            if isinstance(item, MotifItem):
                item.mirror_horizontal()
                count += 1

        if count > 0:
            self.info_label.setText(f"Mirrored {count} motifs horizontally")

    def rotate_selected_90(self):
        selected_items = self.scene.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "Info", "First select motifs by clicking them.")
            return

        count = 0
        for item in selected_items:
            if isinstance(item, MotifItem):
                item.rotate_90()
                count += 1

        if count > 0:
            self.info_label.setText(f"Rotated {count} motifs by 90°")

    def duplicate_selected(self):
        selected_items = [item for item in self.scene.selectedItems() if isinstance(item, MotifItem)]
        if not selected_items:
            QMessageBox.information(self, "Info", "First select motifs by clicking them.")
            return

        self.scene.clearSelection()
        for item in selected_items:
            new_item = MotifItem(item.pixmap())
            new_item.original_pixmap = item.original_pixmap
            new_item.is_mirrored = item.is_mirrored
            new_item.setTransformOriginPoint(item.transformOriginPoint())
            new_item.setTransform(item.transform(), False)
            new_item.setRotation(item.rotation())
            new_item.setPos(item.pos() + QPointF(20, 20))
            self.scene.addItem(new_item)
            new_item.setSelected(True)

        self.info_label.setText(f"Duplicated {len(selected_items)} motifs")

    def bring_to_front(self):
        motif_items = [item for item in self.scene.items() if isinstance(item, MotifItem)]
        if not motif_items:
            return
        max_z = max(item.zValue() for item in motif_items)
        for item in self.scene.selectedItems():
            if isinstance(item, MotifItem):
                max_z += 1
                item.setZValue(max_z)

    def send_to_back(self):
        motif_items = [item for item in self.scene.items() if isinstance(item, MotifItem)]
        if not motif_items:
            return
        min_z = min(item.zValue() for item in motif_items)
        for item in self.scene.selectedItems():
            if isinstance(item, MotifItem):
                min_z -= 1
                item.setZValue(min_z)

    def show_properties_dialog(self):
        selected_items = [item for item in self.scene.selectedItems() if isinstance(item, MotifItem)]
        if len(selected_items) != 1:
            QMessageBox.information(self, "Info", "Select exactly one motif to edit its properties.")
            return

        item = selected_items[0]
        dialog = MotifPropertiesDialog(item, self)
        if dialog.exec() == QDialog.Accepted:
            self.update_selection_toolbar()
            self.info_label.setText("Motif properties updated")

    def update_selection_toolbar(self):
        if not hasattr(self, "selection_toolbar"):
            return
        try:
            selected_items = [item for item in self.scene.selectedItems() if isinstance(item, MotifItem)]
            self.selection_toolbar.reposition(selected_items)
        except RuntimeError:
            # The underlying Qt C++ objects (scene/toolbar) can already be torn down if this
            # fires while the window is closing - nothing meaningful to do in that case.
            pass

    def delete_selected(self):
        selected_items = self.scene.selectedItems()
        if not selected_items:
            QMessageBox.information(
                self,
                "Info",
                "First select motifs by clicking them.\n\nTip: Click a motif to select it, Ctrl+click for multiple motifs."
            )
            return

        count = 0
        for item in selected_items:
            if isinstance(item, MotifItem):
                self.scene.removeItem(item)
                count += 1

        if count > 0:
            self.info_label.setText(f"Deleted {count} motifs")

    def clear_all(self):
        motif_items = [item for item in self.scene.items() if isinstance(item, MotifItem)]

        if not motif_items:
            QMessageBox.information(self, "Info", "There are no motifs on the page.")
            return

        reply = QMessageBox.question(
            self,
            "Confirm",
            f"Are you sure you want to delete all motifs ({len(motif_items)}) from the page?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            for item in motif_items:
                self.scene.removeItem(item)
            self.info_label.setText(f"All motifs ({len(motif_items)}) have been deleted")

    def print_page(self):
        width_mm, height_mm = PAPER_FORMATS[self.current_paper_format]
        width_px = int(width_mm / 25.4 * DPI)
        height_px = int(height_mm / 25.4 * DPI)

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)

        # Set the paper size
        if self.current_paper_format == "A4":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        elif self.current_paper_format == "A3":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A3))
        elif self.current_paper_format == "Letter":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        elif self.current_paper_format == "Legal":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.Legal))

        printer.setPageOrientation(QPageLayout.Orientation.Portrait)

        dialog = QPrintDialog(printer, self)
        if dialog.exec() == QPrintDialog.DialogCode.Accepted:
            try:
                painter = QPainter(printer)

                # Get the printer's DPI
                printer_dpi_x = painter.device().logicalDpiX()
                printer_dpi_y = painter.device().logicalDpiY()

                print(f"DEBUG PRINT: Printer DPI: {printer_dpi_x} x {printer_dpi_y}")
                print(f"DEBUG PRINT: Our DPI: {DPI}")
                print(f"DEBUG PRINT: Page: {width_mm} x {height_mm} mm = {width_px} x {height_px} px (our DPI)")

                # Compute the target_rect in printer pixels for a 1:1 scale
                # The physical dimensions must stay the same: width_mm x height_mm
                target_width_px = int(width_mm / 25.4 * printer_dpi_x)
                target_height_px = int(height_mm / 25.4 * printer_dpi_y)

                print(f"DEBUG PRINT: Target rect: {target_width_px} x {target_height_px} px (printer DPI)")

                source_rect = QRectF(0, 0, width_px, height_px)
                target_rect = QRectF(0, 0, target_width_px, target_height_px)

                self.scene.render(painter, target_rect, source_rect)
                painter.end()
                QMessageBox.information(self, "Print", "The print job was sent to the printer at 1:1 scale.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error while printing: {e}")

    def _calibration_reference_mm(self):
        """Nominal reference dimensions (mm) for the calibration sheet - sized so both
        edges fit end-to-end within a standard 150 mm caliper's measuring range."""
        return float(CALIBRATION_WIDTH_MM), float(CALIBRATION_HEIGHT_MM)

    def _load_scale_factors(self):
        settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return float(data.get("scale_factor_x", 1.0)), float(data.get("scale_factor_y", 1.0))
            except Exception:
                return 1.0, 1.0
        return 1.0, 1.0

    def _refresh_calibration_status_label(self):
        scale_x, scale_y = self._load_scale_factors()
        self.calibration_status_label.setText(f"Current calibration: X={scale_x:.4f}  Y={scale_y:.4f}")

    def print_calibration_sheet(self):
        """Prints a 100 x 150 mm reference rectangle, centered on the current page, with
        the currently saved scale_factor_x/y applied - exactly like it would be to a real
        motif in add_motif(). 150 mm is the largest size that still fits end-to-end within
        a standard digital caliper's range, and using a reference this large instead of a
        small motif keeps the relative measurement error easy to read (a 0.5% error is
        0.5-0.75mm here, comfortably above typical caliper reading precision).

        Calibration is an iterative process: print this sheet, measure the two edges,
        then use 'Apply Calibration...' to fold the measured correction into the current
        scale_factor_x/y (multiplicatively, not a reset to nominal/measured) and repeat -
        print again with the new calibration, measure again, refine again - until the
        measured edges converge on the nominal 100 x 150 mm.
        """
        width_mm, height_mm = PAPER_FORMATS[self.current_paper_format]
        nominal_x_mm, nominal_y_mm = self._calibration_reference_mm()

        scale_x, scale_y = self._load_scale_factors()
        drawn_x_mm = nominal_x_mm * scale_x
        drawn_y_mm = nominal_y_mm * scale_y

        origin_x_mm = (width_mm - drawn_x_mm) / 2
        origin_y_mm = (height_mm - drawn_y_mm) / 2

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if self.current_paper_format == "A4":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        elif self.current_paper_format == "A3":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A3))
        elif self.current_paper_format == "Letter":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        elif self.current_paper_format == "Legal":
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.Legal))
        printer.setPageOrientation(QPageLayout.Orientation.Portrait)

        dialog = QPrintDialog(printer, self)
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return

        try:
            painter = QPainter(printer)
            printer_dpi_x = painter.device().logicalDpiX()
            printer_dpi_y = painter.device().logicalDpiY()

            def mm_to_px_x(mm):
                return (origin_x_mm + mm) / 25.4 * printer_dpi_x

            def mm_to_px_y(mm):
                return (origin_y_mm + mm) / 25.4 * printer_dpi_y

            origin_x = mm_to_px_x(0)
            origin_y = mm_to_px_y(0)
            end_x = mm_to_px_x(drawn_x_mm)
            end_y = mm_to_px_y(drawn_y_mm)

            pen_border = QPen(QColor(0, 0, 0))
            pen_border.setWidth(3)
            pen_major = QPen(QColor(0, 0, 0))
            pen_major.setWidth(2)
            pen_minor = QPen(QColor(150, 150, 150))
            pen_minor.setWidth(1)

            # Reference rectangle outline, centered on the page
            painter.setPen(pen_border)
            painter.drawLine(int(origin_x), int(origin_y), int(end_x), int(origin_y))
            painter.drawLine(int(origin_x), int(end_y), int(end_x), int(end_y))
            painter.drawLine(int(origin_x), int(origin_y), int(origin_x), int(end_y))
            painter.drawLine(int(end_x), int(origin_y), int(end_x), int(end_y))

            # Internal grid every 10mm (50mm lines darker/thicker) so a scale error that
            # only shows up away from the edges - drift, skew, non-uniform stretch - is
            # visible across the whole card, not just at the two outer measurements.
            mm = 10
            while mm < drawn_x_mm:
                x = mm_to_px_x(mm)
                painter.setPen(pen_major if mm % 50 == 0 else pen_minor)
                painter.drawLine(int(x), int(origin_y), int(x), int(end_y))
                mm += 10

            mm = 10
            while mm < drawn_y_mm:
                y = mm_to_px_y(mm)
                painter.setPen(pen_major if mm % 50 == 0 else pen_minor)
                painter.drawLine(int(origin_x), int(y), int(end_x), int(y))
                mm += 10

            # Ticks just outside the top/left edge every 10mm (50mm ticks taller) so the
            # two outer edges can still be measured directly with a ruler from outside the card
            painter.setPen(pen_border)
            mm = 0
            while mm <= drawn_x_mm:
                x = mm_to_px_x(mm)
                tick_h = 20 if mm % 50 == 0 else 10
                painter.drawLine(int(x), int(origin_y - tick_h), int(x), int(origin_y))
                mm += 10

            mm = 0
            while mm <= drawn_y_mm:
                y = mm_to_px_y(mm)
                tick_w = 20 if mm % 50 == 0 else 10
                painter.drawLine(int(origin_x - tick_w), int(y), int(origin_x), int(y))
                mm += 10

            painter.end()
            QMessageBox.information(
                self, "Calibration Sheet",
                f"Calibration sheet sent to the printer (current calibration X={scale_x:.4f}, "
                f"Y={scale_y:.4f} applied).\n\n"
                f"Measure the top edge and the left edge as precisely as you can, then click "
                f"'Apply Calibration...' and enter the two measured values - the nominal size is "
                f"{nominal_x_mm:.0f} x {nominal_y_mm:.0f} mm. If they're not exactly on target, "
                f"print this sheet again with the updated calibration and repeat until they converge."
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error printing the calibration sheet: {e}")

    def apply_calibration(self):
        nominal_x_mm, nominal_y_mm = self._calibration_reference_mm()
        current_scale_x, current_scale_y = self._load_scale_factors()
        dialog = CalibrationDialog(nominal_x_mm, nominal_y_mm, current_scale_x, current_scale_y, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        new_scale_x = round(dialog.new_scale_x, 5)
        new_scale_y = round(dialog.new_scale_y, 5)

        settings_path = os.path.join(os.path.dirname(__file__), "settings.json")
        data = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["scale_factor_x"] = new_scale_x
        data["scale_factor_y"] = new_scale_y
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not save settings.json: {e}")
            return

        self._refresh_calibration_status_label()
        QMessageBox.information(
            self, "Calibration Saved",
            f"Refined calibration saved:\nscale_factor_x = {new_scale_x}\nscale_factor_y = {new_scale_y}\n\n"
            f"This applies to motifs added from now on - re-add any motifs already on the page to pick it up. "
            f"If the last measurement wasn't exactly on target, print the calibration sheet again "
            f"(it now uses this new value) and repeat until it converges."
        )
        self.info_label.setText("Printer calibration updated")

    def export_pdf(self):
        import fitz  # PyMuPDF

        width_mm, height_mm = PAPER_FORMATS[self.current_paper_format]
        width_px = int(width_mm / 25.4 * DPI)
        height_px = int(height_mm / 25.4 * DPI)

        file_path, _ = QFileDialog.getSaveFileName(self, "Export as PDF", "", "PDF Files (*.pdf)")
        if not file_path:
            return

        try:
            # Render the scene to an image
            image = QImage(width_px, height_px, QImage.Format_RGB32)
            image.fill(Qt.white)
            painter = QPainter(image)
            self.scene.render(painter)
            painter.end()

            # Save to a temporary file
            temp_file = "temp_export.png"
            image.save(temp_file)

            # Create the PDF
            pdf = fitz.open()
            page = pdf.new_page(width=width_mm, height=height_mm)
            rect = fitz.Rect(0, 0, width_mm, height_mm)
            page.insert_image(rect, filename=temp_file)
            pdf.save(file_path)
            pdf.close()

            # Clean up the temporary file
            if os.path.exists(temp_file):
                os.remove(temp_file)

            QMessageBox.information(self, "Export", f"PDF export successful.\nFile: {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error exporting PDF: {e}")

    def export_bmp(self):
        from PySide6.QtWidgets import QInputDialog

        # Check that an element is selected
        selected_items = [item for item in self.scene.selectedItems() if isinstance(item, MotifItem)]
        if not selected_items:
            QMessageBox.warning(self, "Warning", "First select the motif you want to export to BMP.")
            return

        # Use the first selected element
        selected_item = selected_items[0]

        # Dialog for entering the scale
        scale, ok = QInputDialog.getDouble(self, "BMP Scale", "Enter scale (e.g. 1.0 = 100%, 2.0 = 200%):",
                                           value=1.0, minValue=0.1, maxValue=10.0, decimals=2)
        if not ok:
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "Export as BMP", "", "BMP Files (*.bmp)")
        if not file_path:
            return

        try:
            # Get the pixmap directly from the element
            original_pixmap = selected_item.pixmap()

            # Rescale
            if scale != 1.0:
                new_width = int(original_pixmap.width() * scale)
                new_height = int(original_pixmap.height() * scale)
                scaled_pixmap = original_pixmap.scaled(new_width, new_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                scaled_pixmap = original_pixmap

            # Convert to QImage and then to black-and-white
            image = scaled_pixmap.toImage()
            bw_image = image.convertToFormat(QImage.Format_Mono, Qt.MonoOnly | Qt.ThresholdDither)

            # Save as BMP
            if not bw_image.save(file_path, "BMP"):
                raise Exception("Failed to save the BMP file")

            QMessageBox.information(self, "Export",
                                    f"BMP export successful.\nFile: {file_path}\nSize: {bw_image.width()}x{bw_image.height()} px\nScale: {scale*100:.0f}%")
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Error", f"Error exporting BMP: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
