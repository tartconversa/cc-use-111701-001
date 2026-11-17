"""测试用合成来源报文构造器（标准库，无真实网络）。"""
import base64
import json

from app.source import to_packet, Response


def make_response(source_id, url, body, *, status=200,
                  content_type='application/json'):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode('utf-8')
    elif isinstance(body, str):
        body = body.encode('utf-8')
    return Response(source_id, url, status,
                    (('Content-Type', content_type),), body)


def packet(source_id, url, body, **kwargs):
    return to_packet(make_response(source_id, url, body, **kwargs))


def index_packet(source_id, base_url, records, *, status=200):
    return packet(source_id, base_url, {'records': records}, status=status)


def record(entry_id, revision, title, body_path, attachments, *, tombstone=False):
    row = {'entry_id': entry_id, 'revision': revision, 'title': title}
    if tombstone:
        row['tombstone'] = True
    else:
        row['body_url'] = body_path
        row['attachments'] = [{'name': name, 'url': url}
                              for name, url in attachments]
    return row


def body_packet(source_id, url, *, revision=None, attachment_summaries=None,
                payload=None, status=200):
    value = dict(payload or {'figures': {'revenue': 100}})
    if revision is not None:
        value['revision'] = revision
    if attachment_summaries is not None:
        value['attachments'] = attachment_summaries
    return packet(source_id, url, value, status=status)


def pdf_packet(source_id, url, content, *, status=200):
    return packet(source_id, url, content, status=status,
                  content_type='application/pdf')


def _resolve(base_url, path):
    if path.startswith(('http://', 'https://')):
        return path
    origin = base_url.split('?')[0]
    if path.startswith('/'):
        # 保留协议与主机，绝对路径从根拼接
        scheme, rest = origin.split('://', 1)
        host = rest.split('/', 1)[0]
        return scheme + '://' + host + path
    return origin.rsplit('/', 1)[0] + '/' + path


def full_round_packets(source_id, base_url, records, body_builder,
                       attachment_builder):
    """按轮次请求顺序生成报文：开场索引、正文/附件、收场索引。"""
    opening = index_packet(source_id, base_url, records)
    middle = []
    for row in records:
        if row.get('tombstone'):
            continue
        body_url = _resolve(base_url, row['body_url'])
        middle.append(body_builder(source_id, body_url, row))
        for item in row['attachments']:
            url = _resolve(base_url, item['url'])
            middle.append(attachment_builder(source_id, url, item, row))
    closing = index_packet(source_id, base_url, records)
    return [opening, *middle, closing]
