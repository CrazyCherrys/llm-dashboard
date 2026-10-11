"""Prompt rendering and deterministic logic scoring from immutable run snapshots."""
import re
import unicodedata
from checks import CANDY_PROMPT, PROMPT

GRAPHIC_CRITERIA = '''画面真正呈现一只鹈鹕骑自行车。鹈鹕有可辨识的长而宽的橙色嘴和喉囊。
自行车有两个车轮、连贯车架、车把和曲柄踏板；鸟处于合理骑乘位置，脚确实接触踏板。
对比多帧，脚随踏板协调运动、车轮转动且结构保持连接；背景移动不能代替骑行。
不要求写实，不要求翅膀握住车把。遮挡或采样不足时应判定为无法确认。'''
DEFAULT_TEMPLATES = (
    dict(name='糖果逻辑题',kind='candy',body=CANDY_PROMPT,rule_type='number',expected_answer='21',review_criteria='',require_animation=False,builtin_key='candy'),
    dict(name='鹈鹕骑自行车',kind='pelican',body=PROMPT,rule_type='visual',expected_answer='',review_criteria=GRAPHIC_CRITERIA,require_animation=True,builtin_key='pelican'),
)


def multiline(value, label, maximum):
    if not isinstance(value,str) or not 1<=len(value.strip())<=maximum or any(ord(c)<32 and c not in '\n\r\t' for c in value):
        raise ValueError(f'{label}需要 1–{maximum} 个字符')
    return value.strip()


def validate_template(config):
    if config['kind'] not in ('candy','pelican'):
        raise ValueError('模板类型不正确')
    config['body'] = multiline(config['body'],'提示词',12000)
    if config['kind']=='candy':
        if config['rule_type'] not in ('number','exact','contains','none'):
            raise ValueError('逻辑题判定方式不正确')
        config['expected_answer'] = '' if config['rule_type']=='none' else multiline(config['expected_answer'],'预期答案',500)
        if config['rule_type']=='number' and not re.fullmatch(r'-?\d+(?:\.\d+)?',config['expected_answer']):
            raise ValueError('数字匹配的预期答案必须是一个整数或小数')
        config['review_criteria'],config['require_animation'] = '',False
    else:
        if config['rule_type']!='visual':
            raise ValueError('图形模板需要设置视觉审核标准')
        config['review_criteria'] = multiline(config['review_criteria'],'图形审核标准',4000)
        config['expected_answer'] = ''
    return config


def render_prompt(template, scene, nonce):
    if template['kind']=='candy':
        return template['body']
    body = template['body'].replace('{scene}',scene).replace('{nonce}',nonce)
    animation = '需要可见的循环动画。' if template['require_animation'] else ''
    return body + f'\n\n输出要求：只返回完整、独立的 SVG，不要 HTML、JavaScript 或外部资源。{animation}\n在画面右下角用可见的 SVG text 元素显示本次校验码：{nonce}。'


def evaluate_logic(output, template):
    rule = template['rule_type']
    expected = template['expected_answer']
    if rule=='none':
        return 'ungraded',{},''
    if rule=='number':
        passed = re.search(r'(?<![\dA-Za-z_.+\-])'+re.escape(expected)+r'(?![\dA-Za-z_]|\.\d)',output) is not None
    elif rule=='exact':
        normalize = lambda value:' '.join(unicodedata.normalize('NFKC',value).strip().casefold().split())
        passed = normalize(output)==normalize(expected)
    else:
        passed = expected in output
    return ('passed' if passed else 'invalid'),{'expected_answer':passed},('' if passed else '回答未满足所选模板的答案规则')
