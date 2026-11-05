"""PaddleOCR 引擎封装：懒加载、单例、版本自适应、可降级。

兼容：
- PaddleOCR 3.x：`PaddleOCR(...).predict(input=...)`，结果对象含 .json
- PaddleOCR 2.x：`PaddleOCR(...).ocr(img, cls=True)`

降级：环境未装 PaddleOCR 时，可用 WT_OCR_MODE=stub 读取同名 .ocr.json 走通全链路，
便于前端联调与离线演示，不影响生产路径。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from ..config import OCR_DET_LIMIT_SIDE, OCR_DET_MODEL, OCR_LANG, OCR_REC_MODEL

log = logging.getLogger("worktime.ocr")

# 关键：PaddleX 默认从 HuggingFace 取模型，国内网络不可达。
# 必须在导入 paddleocr 之前指定 BOS 源（飞桨官方模型仓），否则初始化会报
# "No available model hosting platforms detected"。
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

_lock = threading.Lock()
_rec_lock = threading.Lock()
_engine: Any = None
_engine_version = ""
_load_error = ""
_rec_engine: Any = None
_rec_error = ""


class OcrUnavailable(RuntimeError):
    """识别引擎不可用。"""


def _bbox_from_poly(poly: list[list[float]]) -> list[int]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _normalize_lines(raw_lines: list[dict], page_w: int, page_h: int) -> list[dict]:
    out = []
    for idx, ln in enumerate(raw_lines):
        poly = [[float(p[0]), float(p[1])] for p in ln.get("poly") or []]
        if not poly:
            continue
        if not str(ln.get("text", "")).strip():  # 过滤空文本行
            continue
        bbox = _bbox_from_poly(poly)
        out.append(
            {
                "id": idx,
                "text": str(ln.get("text", "")),
                "score": round(float(ln.get("score", 0.0)), 4),
                "poly": poly,
                "bbox": bbox,
                "cx": (bbox[0] + bbox[2]) / 2,
                "cy": (bbox[1] + bbox[3]) / 2,
                "w": bbox[2] - bbox[0],
                "h": bbox[3] - bbox[1],
                "page_w": page_w,
                "page_h": page_h,
            }
        )
    out.sort(key=lambda x: (round(x["cy"] / 12), x["cx"]))
    return out


def _init_engine():
    global _engine, _engine_version, _load_error
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except Exception as exc:  # pragma: no cover
            _load_error = f"PaddleOCR 未安装或导入失败：{exc}"
            raise OcrUnavailable(_load_error) from exc

        try:
            # PaddleOCR 3.x
            # enable_mkldnn=False 是必需的：Paddle 3.3 在 CPU 上走 oneDNN 指令会抛
            # "ConvertPirAttribute2RuntimeAttribute not support"，关掉后走原生 CPU 内核即正常。
            # 代价是推理变慢，因此配 v6_small 模型 + 960 检测边长把单张压到 5s 左右。
            _engine = PaddleOCR(
                lang=OCR_LANG,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
                text_detection_model_name=OCR_DET_MODEL,
                text_recognition_model_name=OCR_REC_MODEL,
                text_det_limit_side_len=OCR_DET_LIMIT_SIDE,
                text_det_limit_type="max",
            )
            _engine_version = "3.x"
        except TypeError:
            # PaddleOCR 2.x 不接受新参数
            _engine = PaddleOCR(lang=OCR_LANG, use_angle_cls=True, show_log=False)
            _engine_version = "2.x"
        except Exception as exc:  # pragma: no cover
            _load_error = f"PaddleOCR 初始化失败：{exc}"
            raise OcrUnavailable(_load_error) from exc

        log.info("PaddleOCR %s 初始化完成", _engine_version)
        return _engine


def engine_status() -> dict:
    return {
        "mode": os.getenv("WT_OCR_MODE", "paddle"),
        "loaded": _engine is not None,
        "version": _engine_version,
        "error": _load_error,
        "cell_probe": cell_probe_enabled(),
        "cell_probe_error": _rec_error,
    }


def cell_probe_enabled() -> bool:
    return os.getenv("WT_CELL_PROBE", "1").lower() not in ("0", "false", "off")


def _init_rec_engine():
    """单格识别引擎：只跑识别网络，不做检测网络。

    整页 OCR 的检测器会把「一格只写了一个 1」这种细笔画当表格线丢掉（实测
    PP-OCRv6_small_det 在 960/1536/1920 三种边长下均 0 检出），因此数量列需要
    砍出单元格后直接送识别网络。rec 网络对单字符输入的容忍度远高于 det。
    """
    global _rec_engine, _rec_error
    if _rec_engine is not None:
        return _rec_engine
    with _rec_lock:
        if _rec_engine is not None:
            return _rec_engine
        try:
            from paddleocr import TextRecognition  # type: ignore
        except Exception as exc:  # pragma: no cover
            _rec_error = f"TextRecognition 不可用：{exc}"
            raise OcrUnavailable(_rec_error) from exc
        try:
            _rec_engine = TextRecognition(model_name=OCR_REC_MODEL)
        except Exception as exc:  # pragma: no cover
            _rec_error = f"单格识别模型初始化失败：{exc}"
            raise OcrUnavailable(_rec_error) from exc
        log.info("单格识别引擎（%s）初始化完成", OCR_REC_MODEL)
        return _rec_engine


def recognize_cell(image) -> tuple[str, float]:
    """识别一小块图像（单元格裁剪图），返回 (文本, 置信度)。

    image 支持 ndarray。识别网络的预处理断言 imgC == img.shape[2]，
    因此灰度图必须先扩成 3 通道，否则会抛 IndexError: tuple index out of range。
    任何异常都向上抛，由调用方决定是否跳过补检。
    """
    import numpy as np

    arr = image if isinstance(image, np.ndarray) else np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]

    engine = _init_rec_engine()
    result = engine.predict(input=arr)
    for page in result or []:
        data: dict | None = None
        if hasattr(page, "json"):
            j = page.json
            data = j.get("res", j) if isinstance(j, dict) else None
        elif isinstance(page, dict):
            data = page.get("res", page)
        if not data:
            continue
        texts = data.get("rec_texts") or data.get("rec_text") or []
        scores = data.get("rec_scores") or data.get("rec_score") or []
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(scores, list):
            scores = [scores]
        if texts:
            return str(texts[0]), float(scores[0] if scores else 0.0)
    return "", 0.0


def _run_paddle(image_path: str, page_w: int, page_h: int) -> list[dict]:
    engine = _init_engine()
    raw: list[dict] = []

    if hasattr(engine, "predict"):
        result = engine.predict(input=image_path)
        for page in result or []:
            data: dict | None = None
            if hasattr(page, "json"):
                j = page.json
                data = j.get("res", j) if isinstance(j, dict) else None
            elif isinstance(page, dict):
                data = page.get("res", page)
            if not data:
                continue
            texts = data.get("rec_texts") or []
            scores = data.get("rec_scores") or []
            polys = data.get("rec_polys") or data.get("dt_polys") or []
            for text, score, poly in zip(texts, scores, polys):
                raw.append({"text": text, "score": score, "poly": poly})
    else:  # pragma: no cover - 2.x 分支
        result = engine.ocr(image_path, cls=True)
        for page in result or []:
            for line in page or []:
                box, (text, score) = line[0], line[1]
                raw.append({"text": text, "score": score, "poly": box})

    return _normalize_lines(raw, page_w, page_h)


def _run_stub(image_path: str, page_w: int, page_h: int) -> list[dict]:
    sidecar = Path(image_path).with_suffix(".ocr.json")
    if not sidecar.exists():
        raise OcrUnavailable(
            f"stub 模式缺少伴随文件 {sidecar.name}。请切换到真实引擎（设置 WT_OCR_MODE=paddle）。"
        )
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    raw = data.get("lines", data if isinstance(data, list) else [])
    return _normalize_lines(raw, int(data.get("page_w", page_w)), int(data.get("page_h", page_h)))


def recognize(image_path: str, page_w: int, page_h: int) -> list[dict]:
    """识别一张图，返回文本行列表（含坐标与置信度）。"""
    mode = os.getenv("WT_OCR_MODE", "paddle").lower()
    if mode == "stub":
        return _run_stub(image_path, page_w, page_h)
    return _run_paddle(image_path, page_w, page_h)
