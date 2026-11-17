"""公告版本身份与跨资源一致性判定。

版本身份只由内容摘要决定（正文字节 + 每个附件的名称与字节摘要），
不使用时间戳、文件名或来源自报的 revision 号：同名附件换内容、正文
被替换都会得到不同版本；仅改标题则版本身份不变。

一致性判定把一次索引快照与按该快照取回的正文/附件互相对照，
任何对不上的环节都给出具体原因，交由轮次层决定待核。
"""
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urljoin

TOMBSTONE_MARKERS = ('tombstone', 'withdrawn', 'deleted')


class VersioningError(ValueError):
    """索引或详情实体结构无法用于版本判定。"""


@dataclass(frozen=True)
class AttachmentLink:
    name: str
    url: str


@dataclass(frozen=True)
class IndexEntry:
    entry_id: str
    revision: str | None
    title: str | None
    body_url: str | None
    attachments: tuple[AttachmentLink, ...]
    tombstone: bool


def _is_tombstone(row):
    for marker in TOMBSTONE_MARKERS:
        if row.get(marker) is True:
            return True
    if isinstance(row.get('status'), str) and row['status'].lower() in TOMBSTONE_MARKERS:
        return True
    return False


def parse_index(response, base_url):
    """解析索引响应为有序条目；结构非法时抛出 VersioningError。"""
    if response.status != 200:
        raise VersioningError('索引未返回成功实体（HTTP %s）' % response.status)
    try:
        value = json.loads(response.body)
    except (ValueError, TypeError) as exc:
        raise VersioningError('索引实体不是合法 JSON') from exc
    if not isinstance(value, dict) or not isinstance(value.get('records'), list):
        raise VersioningError('索引缺少 records 数组')
    entries = []
    seen = set()
    for row in value['records']:
        if not isinstance(row, dict):
            raise VersioningError('索引条目不是对象')
        entry_id = row.get('entry_id')
        if not isinstance(entry_id, str) or not entry_id:
            raise VersioningError('条目身份无效')
        if entry_id in seen:
            raise VersioningError('索引中 entry_id 重复：%s' % entry_id)
        seen.add(entry_id)
        revision = row.get('revision') if isinstance(row.get('revision'), str) else None
        title = row.get('title') if isinstance(row.get('title'), str) else None
        body_url = row.get('body_url') if isinstance(row.get('body_url'), str) else None
        attachments = []
        for item in row.get('attachments') or []:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) \
                    or not isinstance(item.get('url'), str) or not item['name'] or not item['url']:
                raise VersioningError('条目 %s 的附件链接无效' % entry_id)
            attachments.append(AttachmentLink(item['name'],
                                               urljoin(base_url, item['url'])))
        if not _is_tombstone(row) and (not body_url or not attachments):
            raise VersioningError('条目 %s 缺少正文或附件链接' % entry_id)
        entries.append(IndexEntry(
            entry_id, revision, title,
            urljoin(base_url, body_url) if body_url else None,
            tuple(attachments), _is_tombstone(row)))
    return tuple(entries)


def parse_body_detail(response):
    """解析正文详情；非 JSON 正文返回空声明（仅做字节级身份）。"""
    if response.status != 200:
        return {}
    try:
        value = json.loads(response.body)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def evaluate_entry(entry, body, fetched_attachments):
    """对照索引、正文与附件，返回 (问题清单, 详情声明)。

    fetched_attachments 为按索引顺序取回的 (link, response) 序列。
    问题为空才允许据此确认版本。
    """
    problems = []
    if entry.tombstone:
        return problems, {}
    if body.status != 200:
        problems.append('正文未返回成功实体（HTTP %s）' % body.status)
    if len(fetched_attachments) != len(entry.attachments):
        problems.append('附件取回数量与索引不符')
    for link, response in fetched_attachments:
        if response.status != 200:
            problems.append('附件 %s 未返回成功实体（HTTP %s）' % (link.name, response.status))
    # 只有成功实体才解析详情声明；非 200 正文不参与跨页字段核对
    detail = parse_body_detail(body) if body.status == 200 else {}
    declared_revision = detail.get('revision')
    if isinstance(declared_revision, str) and entry.revision is not None \
            and declared_revision != entry.revision:
        problems.append('正文自报 revision=%s 与索引 %s 不一致'
                        % (declared_revision, entry.revision))
    declared_summaries = detail.get('attachments')
    if isinstance(declared_summaries, list):
        # 按索引位置配对实际附件，避免同名附件互相错配
        actual = [response for _, response in fetched_attachments]
        for i, summary in enumerate(declared_summaries):
            if not isinstance(summary, dict) or not isinstance(summary.get('name'), str):
                problems.append('正文附件摘要结构无效')
                continue
            name = summary['name']
            if i >= len(entry.attachments) or entry.attachments[i].name != name:
                problems.append('正文声明的附件 %s 与索引位置不符' % name)
                continue
            digest = summary.get('sha256')
            if isinstance(digest, str) and digest != actual[i].sha256:
                problems.append('附件 %s 摘要与实际响应不符' % name)
    return problems, detail


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def version_identity(entry, body, fetched_attachments):
    """由内容摘要计算版本身份；入册前调用方必须先确认无一致性问题。"""
    material = {
        'entry_id': entry.entry_id,
        'body_sha256': body.sha256,
        'attachments': [{'name': link.name, 'sha256': response.sha256}
                        for link, response in fetched_attachments],
    }
    return hashlib.sha256(_canonical(material)).hexdigest()


def manifest_digest(version_id, index_sha256, entry_id):
    """版本与所属索引快照的联合摘要，作为发布状态证据链指针。"""
    material = {'version_id': version_id, 'index_sha256': index_sha256,
                'entry_id': entry_id}
    return hashlib.sha256(_canonical(material)).hexdigest()
