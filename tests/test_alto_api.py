"""ALTO XML 导出接口端到端测试。"""
import json
import xml.etree.ElementTree as ET

from tests.conftest import make_word
from tests.test_api import _submit, _wait

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v4#"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
A = f"{{{ALTO_NS}}}"


def _alto(client, task_id):
    return client.get(f"/api/tasks/{task_id}/alto.xml")


def _parse(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["Content-Type"].startswith("application/alto+xml")
    return ET.fromstring(resp.content)


def _strings(root):
    return root.findall(f".//{A}String")


def test_alto_export_structure_and_reading_order(client):
    words = []
    for i in range(3):
        words.append(make_word(len(words), f"L{i}", 80, 100 + i * 80, 80, 30))
    for i in range(3):
        words.append(make_word(len(words), f"R{i}", 560, 100 + i * 80, 80, 30))
    task_id = _submit(client, words)
    _wait(client, task_id)

    resp = _alto(client, task_id)
    assert resp.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    disposition = resp.headers["Content-Disposition"]
    assert "attachment" in disposition
    assert f'{task_id}.alto.xml' in disposition

    root = _parse(resp)
    assert root.tag == f"{A}alto"
    # ALTO 4.3 命名空间与 schemaLocation
    schema_loc = root.get(f"{{{XSI_NS}}}schemaLocation")
    assert ALTO_NS in schema_loc
    assert "alto-4-3.xsd" in schema_loc

    unit = root.find(f"./{A}Description/{A}MeasurementUnit")
    assert unit is not None
    assert unit.text == "pixel"

    page = root.find(f"./{A}Layout/{A}Page")
    assert page is not None
    assert page.get("WIDTH") == "1000"
    assert page.get("HEIGHT") == "1400"

    blocks = page.findall(f"./{A}PrintSpace/{A}TextBlock")
    assert [b.get("ID") for b in blocks] == ["block_0", "block_1"]

    lines = page.findall(f".//{A}TextLine")
    assert [ln.get("ID") for ln in lines] == [f"line_{i}" for i in range(6)]
    contents = [
        [s.get("CONTENT") for s in ln.findall(f"./{A}String")]
        for ln in lines
    ]
    # 阅读顺序：左栏自上而下 -> 右栏自上而下
    assert contents == [["L0"], ["L1"], ["L2"], ["R0"], ["R1"], ["R2"]]

    # 词节点 ID 全局唯一且按阅读顺序编号
    assert [s.get("ID") for s in _strings(root)] == [
        f"word_{i}" for i in range(6)
    ]


def test_alto_word_geometry_and_confidence(client):
    words = [
        make_word(0, "hello", 100, 100, 80, 30, conf=0.99),
        make_word(1, "world", 190, 100, 90, 30, conf=None),
    ]
    task_id = _submit(client, words)
    _wait(client, task_id)

    root = _parse(_alto(client, task_id))
    strings = _strings(root)
    assert len(strings) == 2

    first = strings[0]
    assert first.get("CONTENT") == "hello"
    assert first.get("WC") == "0.99"
    # 外接框来自原始四角坐标
    assert first.get("HPOS") == "100"
    assert first.get("VPOS") == "100"
    assert first.get("WIDTH") == "80"
    assert first.get("HEIGHT") == "30"
    # 原始四角坐标多边形（ALTO 4：空格分隔的 x y 浮点序列）
    polygon = first.find(f"./{A}Shape/{A}Polygon")
    assert polygon is not None
    assert polygon.get("POINTS") == "100 100 180 100 180 130 100 130"

    second = strings[1]
    assert second.get("CONTENT") == "world"
    assert second.get("WC") is None  # 未提供置信度时不输出 WC
    assert second.find(f"./{A}Shape/{A}Polygon").get("POINTS") == \
        "190 100 280 100 280 130 190 130"


def test_alto_escapes_xml_special_chars(client):
    text = "A&B <\"tag\"> 'q'"
    words = [make_word(0, text, 100, 100, 80, 30)]
    task_id = _submit(client, words)
    _wait(client, task_id)

    resp = _alto(client, task_id)
    # 特殊字符在 XML 中被转义
    assert "A&amp;B" in resp.text
    assert "&lt;&quot;tag&quot;&gt;" in resp.text
    # 解析后还原为原始文本
    root = _parse(resp)
    assert _strings(root)[0].get("CONTENT") == text


def test_alto_export_is_byte_stable(client):
    words = [
        make_word(0, "hello", 100, 100, 80, 30),
        make_word(1, "world", 190, 100, 80, 30),
    ]
    task_id = _submit(client, words)
    _wait(client, task_id)
    r1 = _alto(client, task_id)
    r2 = _alto(client, task_id)
    assert r1.status_code == 200
    assert r1.content == r2.content


def test_alto_export_does_not_modify_task(client):
    words = [make_word(0, "hello", 100, 100, 80, 30, conf=0.2)]
    task_id = _submit(client, words)
    before = _wait(client, task_id)
    resp = _alto(client, task_id)
    assert resp.status_code == 200
    after = client.get(f"/api/tasks/{task_id}").json()
    assert after["status"] == "done"
    assert after["result"] == before["result"]
    assert after["finished_at"] == before["finished_at"]


def test_alto_skips_unclassified_words(client):
    words = [
        make_word(0, "hello", 100, 100, 80, 30),
        {"id": 1, "text": "  ", "confidence": 0.9,
         "quad": [{"x": 0, "y": 0}, {"x": 10, "y": 0},
                  {"x": 10, "y": 10}, {"x": 0, "y": 10}]},
    ]
    task_id = _submit(client, words)
    body = _wait(client, task_id)
    assert len(body["result"]["unclassified"]) == 1
    root = _parse(_alto(client, task_id))
    assert [s.get("CONTENT") for s in _strings(root)] == ["hello"]


def test_alto_empty_page(client):
    task_id = _submit(client, [])
    _wait(client, task_id)
    root = _parse(_alto(client, task_id))
    page = root.find(f"./{A}Layout/{A}Page")
    assert page.get("WIDTH") == "1000"
    assert page.get("HEIGHT") == "1400"
    assert page.find(f"./{A}PrintSpace") is not None
    assert _strings(root) == []


def test_alto_unknown_task_404(client):
    resp = _alto(client, "no-such-task")
    assert resp.status_code == 404
    # 与其他接口沿用统一错误结构
    assert resp.json() == {"detail": "task not found"}


def test_alto_pending_task_409(client):
    from app import db
    db.create_task("pending-alto-1",
                   json.dumps({"page_width": 1, "page_height": 1, "words": []}))
    resp = _alto(client, "pending-alto-1")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "task not finished: status=pending"
    # 任务本身未被改动
    assert db.get_task("pending-alto-1")["status"] == "pending"


def test_alto_failed_task_409(client):
    from app import db
    db.create_task("failed-alto-1",
                   json.dumps({"page_width": 1, "page_height": 1, "words": []}))
    db.mark_status("failed-alto-1", "failed", error="boom")
    resp = _alto(client, "failed-alto-1")
    assert resp.status_code == 409
    assert "status=failed" in resp.json()["detail"]
