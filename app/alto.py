"""已完成任务的 ALTO XML 导出（只读、确定性序列化）。

把已落库的排序结果序列化为 ALTO 4.3 文档：
- 页面尺寸取自任务结果（缺省回退到原始请求的页面宽高）；
- ``TextBlock`` / ``TextLine`` / ``String`` 严格按结果阅读顺序输出；
- ``String`` 保留原始四角坐标（``Shape/Polygon``）及其外接框
  （``HPOS``/``VPOS``/``WIDTH``/``HEIGHT``）、文本（``CONTENT``）与置信度（``WC``）；
- XML 特殊字符由 ElementTree 统一转义；同一任务多次导出逐字节一致。

本模块不写库、不改写任务结果。
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v4#"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ALTO_43_SCHEMA = "http://www.loc.gov/standards/alto/v4/alto-4-3.xsd"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'

ET.register_namespace("", ALTO_NS)
ET.register_namespace("xsi", XSI_NS)

Point = Tuple[float, float]
Box = Tuple[float, float, float, float]  # (hpos, vpos, width, height)


def _q(tag: str) -> str:
    return f"{{{ALTO_NS}}}{tag}"


def _fmt(value: Any) -> str:
    """把坐标/置信度格式化为最多 3 位小数的稳定字符串（与落库精度一致）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(v):
        return "0"
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def _as_points(quad: Any) -> List[Point]:
    """存储的四角点（[x, y] 或 {"x","y"}）-> (x, y) 列表；非法返回 []。"""
    pts: List[Point] = []
    if not isinstance(quad, list):
        return pts
    for p in quad[:4]:
        try:
            if isinstance(p, dict):
                x, y = float(p["x"]), float(p["y"])
            else:
                x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, KeyError, IndexError):
            return []
        if not (math.isfinite(x) and math.isfinite(y)):
            return []
        pts.append((x, y))
    return pts if len(pts) == 4 else []


def _aabb(pts: List[Point]) -> Box:
    """四角点的外接框 (hpos, vpos, width, height)。"""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    left, top = min(xs), min(ys)
    return left, top, max(xs) - left, max(ys) - top


def _set_box(el: ET.Element, box: Box) -> None:
    hpos, vpos, width, height = box
    el.set("HPOS", _fmt(hpos))
    el.set("VPOS", _fmt(vpos))
    el.set("WIDTH", _fmt(width))
    el.set("HEIGHT", _fmt(height))


def _union_boxes(boxes: List[Box], fallback: Box) -> Box:
    """多个外接框的并集；为空时退回 fallback。"""
    if not boxes:
        return fallback
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    return left, top, right - left, bottom - top


def build_alto_xml(request_payload: Dict[str, Any],
                   result: Dict[str, Any]) -> str:
    """根据原始请求与已完成结果生成 ALTO 4.3 文档（只读、确定性）。

    块 / 行 / 词的输出顺序与 ``GET /api/tasks/{task_id}`` 结果中的
    阅读顺序完全一致；同样的输入必然得到逐字节相同的 XML。
    """
    page = result.get("page") or {}
    page_width = page.get("width", request_payload.get("page_width", 0))
    page_height = page.get("height", request_payload.get("page_height", 0))

    alto = ET.Element(_q("alto"))
    alto.set(f"{{{XSI_NS}}}schemaLocation", f"{ALTO_NS} {ALTO_43_SCHEMA}")

    description = ET.SubElement(alto, _q("Description"))
    unit = ET.SubElement(description, _q("MeasurementUnit"))
    unit.text = "pixel"

    layout = ET.SubElement(alto, _q("Layout"))
    page_el = ET.SubElement(layout, _q("Page"))
    page_el.set("ID", "page_1")
    page_el.set("PHYSICAL_IMG_NR", "0")
    page_el.set("WIDTH", _fmt(page_width))
    page_el.set("HEIGHT", _fmt(page_height))

    blocks = result.get("blocks") or []
    lines_by_id = {ln.get("id"): ln for ln in result.get("lines") or []}

    block_boxes = [
        _aabb(pts)
        for pts in (_as_points(b.get("bbox")) for b in blocks)
        if pts
    ]
    print_space_box = _union_boxes(
        block_boxes, fallback=(0.0, 0.0, float(page_width or 0),
                               float(page_height or 0)))

    print_space = ET.SubElement(page_el, _q("PrintSpace"))
    _set_box(print_space, print_space_box)

    word_count = 0
    for block_idx, block in enumerate(blocks):
        block_el = ET.SubElement(print_space, _q("TextBlock"))
        block_el.set("ID", f"block_{block.get('id', block_idx)}")
        block_pts = _as_points(block.get("bbox"))
        if block_pts:
            _set_box(block_el, _aabb(block_pts))

        for line_id in block.get("line_ids") or []:
            line = lines_by_id.get(line_id)
            if line is None:
                continue
            line_el = ET.SubElement(block_el, _q("TextLine"))
            line_el.set("ID", f"line_{line.get('order', line.get('id'))}")
            line_pts = _as_points(line.get("bbox"))
            if line_pts:
                _set_box(line_el, _aabb(line_pts))

            for wp in line.get("words") or []:
                # 词节点：原始四角坐标外接框 + 多边形 + 文本 + 置信度
                word_pts = _as_points(wp.get("quad")) or line_pts
                string_el = ET.SubElement(line_el, _q("String"))
                string_el.set("ID", f"word_{word_count}")
                word_count += 1
                if word_pts:
                    _set_box(string_el, _aabb(word_pts))
                confidence = wp.get("confidence")
                if confidence is not None:
                    string_el.set("WC", _fmt(confidence))
                string_el.set("CONTENT", str(wp.get("text") or ""))
                if word_pts:
                    shape_el = ET.SubElement(string_el, _q("Shape"))
                    polygon_el = ET.SubElement(shape_el, _q("Polygon"))
                    polygon_el.set("POINTS", " ".join(
                        f"{_fmt(x)} {_fmt(y)}" for x, y in word_pts))

    ET.indent(alto, space="  ")
    return f"{XML_DECLARATION}\n{ET.tostring(alto, encoding='unicode')}\n"
