"""审核标准的可计算边界；模型仍负责事实与分析质量的复核。"""
import re


def report_metrics(body):
    text = re.sub(r'\[S\d+\]', '', body or '')
    return {'characters': len(body or ''), 'chinese_characters': len(re.findall(r'[\u4e00-\u9fff]', text)),
            'word_units': len(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9]+', text)), 'minimum_target': 1000}


def policy_violations(review, body):
    """仅拦截可证伪的字数要求和明确新增的格式要求，不判断全文好坏。"""
    metrics = report_metrics(body)
    texts = list(review.get('comments') or [])
    texts += [f.get('problem', '') + '：' + f.get('expected', '') for f in review.get('findings', [])]
    errors = []
    for text in texts:
        if metrics['word_units'] >= 1000 and re.search(r'(?:篇幅|字数|正文).{0,24}(?:不足|低于|不够)|(?:扩充|增加).{0,8}字数', text):
            errors.append({'code':'LENGTH_CONTRADICTS_COUNT', 'text':text, 'metrics':metrics})
        if re.search(r'(?:每[个一]?章|各章|章节|第二章至第四章).{0,30}(?:小结|总结段落|结语段落)|独立标题页', text):
            if re.search(r'必须|缺失|缺少|不完整|添加|增加|填补|要求', text):
                errors.append({'code':'UNLISTED_FORMAT_REQUIREMENT', 'text':text})
    return errors
