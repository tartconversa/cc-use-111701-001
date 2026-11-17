"""公告版本采集轮次：开场索引、逐资源取证、收场复核与中断恢复。

一轮采集对应来源的一次发布状态快照：
1. 开场读取索引并立即内容寻址落盘；
2. 严格按索引顺序取回每条公告的正文与全部附件，每取回一个响应
   就把轮次状态原子落盘，崩溃后可从最后一个已存响应继续；
3. 收场再次读取同一索引，状态码或响应体与开场不一致即证明来源
   在抓取途中更新，整轮封存为待核，不产生任何确认版本。

轮次文档只保存原件路径与摘要、一致性问题与版本身份，不保存
时间戳——版本顺序由轮次序号与内容哈希表达。
"""
from .source import from_packet
from .transport import TransportError
from . import versioning


def _response_ref(store, response):
    rel, digest, nbytes = store.save_response(response)
    return {'url': response.url, 'status': response.status,
            'response_id': response.response_id,
            'response_path': rel, 'response_sha256': digest,
            'bytes': nbytes}


def _load_response(store, ref):
    return from_packet(store.read_json(store.root / ref['response_path']))


class RoundRunner:
    def __init__(self, store, transport):
        self.store = store
        self.transport = transport

    # ---- 持久化与骨架 -------------------------------------------------

    def _write(self, doc):
        self.store.write_json(self.store.round_path(doc['round_id']), doc)

    def _skeleton(self, index_entry):
        return {'entry_id': index_entry.entry_id,
                'revision': index_entry.revision,
                'title': index_entry.title,
                'tombstone': index_entry.tombstone,
                'body': None,
                'attachments': [{'name': link.name, 'url': link.url,
                                 'response': None}
                                for link in index_entry.attachments],
                'problems': [], 'detail': {}, 'version_id': None}

    def _sync_skeleton(self, doc, index_entries):
        """恢复时用开场索引重建骨架，按位置保留已取回的资源记录。

        附件按索引位置对齐而非按名称：同名附件是来源允许的情形，
        名称映射会把两个附件的取证结果错配。
        """
        existing = {item['entry_id']: item for item in doc['entries']}
        rebuilt = []
        for index_entry in index_entries:
            old = existing.get(index_entry.entry_id)
            if old is None:
                rebuilt.append(self._skeleton(index_entry))
                continue
            old['revision'] = index_entry.revision
            old['title'] = index_entry.title
            old['tombstone'] = index_entry.tombstone
            old_slots = old.get('attachments', [])
            old['attachments'] = [
                {'name': link.name, 'url': link.url,
                 'response': old_slots[i]['response'] if i < len(old_slots) else None}
                for i, link in enumerate(index_entry.attachments)]
            rebuilt.append(old)
        doc['entries'] = rebuilt

    # ---- 入口 ---------------------------------------------------------

    def run(self, index_url, source_id, round_id):
        path = self.store.round_path(round_id)
        if path.exists():
            return self.resume(round_id, index_url, source_id)
        return self._start(index_url, source_id, round_id)

    def _start(self, index_url, source_id, round_id):
        response = self.transport.get(index_url, source_id)  # 失败则不留轮次
        ref = _response_ref(self.store, response)
        doc = {'round_id': round_id, 'source_id': source_id, 'index_url': index_url,
               'state': 'open', 'status': None,
               'opening': ref, 'closing': None,
               'entries': [], 'reasons': []}
        self._write(doc)
        try:
            index_entries = versioning.parse_index(response, index_url)
        except versioning.VersioningError as exc:
            doc['index_drifted'] = True
            doc['reasons'].append('开场索引不可用：%s' % exc)
            self._seal(doc)
            return doc
        doc['entries'] = [self._skeleton(item) for item in index_entries]
        self._write(doc)
        self._fetch_and_close(doc, index_entries)
        return doc

    def resume(self, round_id, index_url, source_id):
        doc = self.store.read_json(self.store.round_path(round_id))
        if doc['state'] == 'sealed':
            return doc
        if doc['source_id'] != source_id or doc['index_url'] != index_url:
            raise ValueError('轮次身份与本次请求不符，禁止并入另一来源的响应')
        opening = _load_response(self.store, doc['opening'])
        try:
            index_entries = versioning.parse_index(opening, index_url)
        except versioning.VersioningError as exc:  # 原件损坏类问题，沿用待核
            doc['index_drifted'] = True
            doc['reasons'].append('开场索引不可用：%s' % exc)
            self._seal(doc)
            return doc
        self._sync_skeleton(doc, index_entries)
        self._write(doc)
        self._fetch_and_close(doc, index_entries)
        return doc

    # ---- 取证 ---------------------------------------------------------

    def _fetch_and_close(self, doc, index_entries):
        # 上次运行遗留的中断原因属于运行状态，恢复时清除，避免污染封存结论
        doc['reasons'] = [r for r in doc['reasons'] if '待恢复' not in r]
        self._fetch_entries(doc, index_entries)
        if doc['state'] == 'open' and not self._interrupted(doc):
            self._close(doc)

    @staticmethod
    def _interrupted(doc):
        return any('待恢复' in r for r in doc['reasons'])

    def _fetch_entries(self, doc, index_entries):
        try:
            for index_entry, item in zip(index_entries, doc['entries']):
                if index_entry.tombstone:
                    item['tombstone'] = True
                    self._write(doc)
                    continue
                if item['body'] is None:
                    response = self.transport.get(index_entry.body_url, doc['source_id'])
                    item['body'] = _response_ref(self.store, response)
                    self._write(doc)  # 每个响应后落盘：崩溃只丢未取的部分
                for link, slot in zip(index_entry.attachments, item['attachments']):
                    if slot['response'] is None:
                        response = self.transport.get(link.url, doc['source_id'])
                        slot['response'] = _response_ref(self.store, response)
                        self._write(doc)
                self._evaluate(index_entry, item, doc)
        except (TransportError, OSError) as exc:
            # 传输中断或重放报文不足：已取响应均已落盘，轮次保持 open，
            # 下次运行从同一开场快照恢复，绝不拼接另一发布时刻的响应
            doc['reasons'].append('采集中断（待恢复）：%s' % exc)
            self._write(doc)

    def _evaluate(self, index_entry, item, doc):
        body = _load_response(self.store, item['body'])
        fetched = [(link, _load_response(self.store, slot['response']))
                   for link, slot in zip(index_entry.attachments, item['attachments'])]
        problems, detail = versioning.evaluate_entry(index_entry, body, fetched)
        item['problems'] = problems
        item['detail'] = detail
        if not problems:
            item['version_id'] = versioning.version_identity(index_entry, body, fetched)
        for problem in problems:
            doc['reasons'].append('条目 %s：%s' % (index_entry.entry_id, problem))
        self._write(doc)

    def _close(self, doc):
        try:
            response = self.transport.get(doc['index_url'], doc['source_id'])
        except (TransportError, OSError) as exc:
            doc['reasons'].append('收场索引读取中断（待恢复）：%s' % exc)
            self._write(doc)
            return
        ref = _response_ref(self.store, response)
        doc['closing'] = ref
        if ref['status'] != doc['opening']['status'] \
                or ref['response_sha256'] != doc['opening']['response_sha256']:
            doc['index_drifted'] = True
            doc['reasons'].append(
                '收场索引与开场不一致（开场 %s / 收场 %s），抓取途中来源发生更新'
                % (doc['opening']['response_sha256'][:12], ref['response_sha256'][:12]))
        self._seal(doc)

    def _seal(self, doc):
        doc['state'] = 'sealed'
        doc.setdefault('index_drifted', False)
        # 任一条目无法通过一致性判定，整轮即不具备整体发布状态证明；
        # 通过的条目仍由账本逐条确认，失败条目逐条进待核。
        has_entry_problem = any(item['problems'] for item in doc['entries'])
        doc['status'] = 'suspect' if (doc['reasons'] or has_entry_problem) \
            else 'consistent'
        self._write(doc)
