"""Versioned benchmark prompts and safe SVG inspection."""
import json
import re
from urllib import parse
import xml.etree.ElementTree as ET

MAX_RESPONSE = 2 * 1024 * 1024
RULE_VERSION = "candy-v1-independent-21"


SCENES = ("海边木栈道", "秋日林道", "春日草坡", "雨后湿地", "黄昏公路", "湖畔风车", "热带海岛", "雪山谷地")

PROMPT = """创建一幅独立的 SVG 鹈鹕骑自行车 2D 循环动画。
主角是一只可爱的鹈鹕，有橙色大嘴、踩在踏板上的蹼足，以及微微张开保持平衡的翅膀。
自行车轮子持续转动，脚与踏板动作协调，动画流畅，背景明亮，配色鲜活。
本次场景：{scene}。请为这个场景独立构图。
在画面右下角用可见的 SVG text 元素显示本次校验码：{nonce}。
只返回一个完整的 SVG，可使用内联 CSS 或 SMIL 动画；不要 HTML、JavaScript、外部图片、外部字体或其他外部资源。"""

CANDY_PROMPT = """在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

| 形状 | 苹果味 | 桃子味 | 西瓜味 |
| 圆形 | 7 | 9 | 8 |
| 五角星形 | 7 | 6 | 4 |"""

def candy_passes(text):
    return re.search(r"(?<![\dA-Za-z_.+\-])21(?![\dA-Za-z_]|\.\d)", text) is not None

def mask(value):
    return value[:3] + "****" + value[-3:] if len(value) > 8 else "****"

def mask_url(value):
    try:
        host = parse.urlsplit(value or "").hostname or ""
    except ValueError:
        host = ""
    return host[:2] + "****" + host[-2:] if len(host) > 5 else "****"

def redact(value, config):
    if not isinstance(value, str):
        return value
    try:
        host = parse.urlsplit(config.get("base_url") or "").hostname
    except ValueError:
        host = None
    for secret, replacement in ((config.get("api_key"), "****"),
                                (config.get("base_url"), mask_url(config.get("base_url", ""))),
                                (host, mask_url(config.get("base_url", "")))):
        if secret:
            value = value.replace(secret, replacement)
    return value

def redact_nested(value, config):
    """Protect imported/unstructured results as well as known text fields."""
    if isinstance(value, dict):
        return {redact(key, config): redact_nested(item, config) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_nested(item, config) for item in value]
    return redact(value, config)

def json_object(value):
    """Older optional JSON fields can be empty, malformed, or null."""
    try:
        result = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}

def inspect_svg(output, nonce, require_animation=True):
    checks = dict(svg=False, animation=False, nonce=False)
    if not isinstance(output, str) or len(output.encode('utf-8')) > MAX_RESPONSE:
        return "", checks, "SVG 超过大小限制或格式无效"
    match = re.search(r"<svg\b[\s\S]*?</svg\s*>", output, re.I)
    if not match:
        return "", checks, "未找到完整 SVG"
    svg = match.group()
    try:
        if re.search(r"<!DOCTYPE|<!ENTITY|<\?", svg, re.I):
            raise ValueError("SVG 包含不允许的 XML 声明")
        root = ET.fromstring(svg)
        if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
            raise ValueError("SVG 根元素命名空间不正确")
        for el in root.iter():
            tag = el.tag.split("}")[-1].lower()
            if tag in ("script", "foreignobject", "iframe", "object", "embed", "image", "audio", "video", "a"):
                raise ValueError("SVG 包含脚本、外部资源或不安全元素")
            for attr, value in el.attrib.items():
                name = attr.split("}")[-1].lower()
                if attr == '{http://www.w3.org/XML/1998/namespace}base' or '\\' in value:
                    raise ValueError("SVG 包含外部基准地址或不安全转义")
                if name.startswith("on") or (name in ("href", "src") and not value.startswith("#")):
                    raise ValueError("SVG 包含事件或外部引用")
                if name == "attributename" and (value.lower().startswith("on") or value.lower() in ("href", "xlink:href", "src")):
                    raise ValueError("SVG 动画不允许修改事件或资源引用")
            if tag == "text" and nonce in "".join(el.itertext()):
                checks["nonce"] = True
            if tag in ("animate", "animatetransform", "animatemotion"):
                checks["animation"] = True
            if tag == 'style' and '\\' in ''.join(el.itertext()):
                raise ValueError("SVG 样式包含不安全转义")
        # Inspect decoded XML, so character references cannot hide external URLs.
        decoded = ET.tostring(root, encoding='unicode')
        refs = re.findall(r"url\((.*?)\)", decoded, re.I | re.S)
        if re.search(r"@import|javascript:|expression\s*\(", decoded, re.I) or any(not ref.strip().strip("\"'").startswith("#") for ref in refs):
            raise ValueError("SVG 包含外部或不安全样式")
        if re.search(r"@keyframes\s", decoded, re.I) and re.search(r"animation(?:-name)?\s*:", decoded, re.I):
            checks["animation"] = True
        checks["svg"] = True
        if not root.get("xmlns") and root.tag == "svg":
            root.set("xmlns", "http://www.w3.org/2000/svg")
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        svg = ET.tostring(root, encoding="unicode")
    except (ET.ParseError, ValueError) as exc:
        return "", checks, str(exc)
    missing = []
    if not require_animation:
        checks.pop('animation')
    if require_animation and not checks["animation"]:
        missing.append("未发现动画声明")
    if not checks["nonce"]:
        missing.append("校验码缺失或不一致")
    return svg, checks, "；".join(missing)
