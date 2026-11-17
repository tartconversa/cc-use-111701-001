"""跨轮次账本：版本只追加确认，待核不覆盖已确认版本。

账本由三部分组成：
- events.jsonl 只追加的轮次事件，任何已确认事实都不可改写；
- views/versions.json 当前确认版本表（含撤回标记与最近核验轮次）；
- views/pending.json 尚未通过一致性判定的待核条目。

一致性轮次中出现的内容身份注册为新版本并交付；待核轮次只进入
待核表，即使其正文与已确认版本不同，也不改动已确认版本。
"""
import json

from . import versioning

EVENTS = 'events.jsonl'
VERSIONS_VIEW = ('views', 'versions.json')
PENDING_VIEW = ('views', 'pending.json')
DELIVERABLES_VIEW = ('views', 'deliverables.json')


def _view_path(store, parts):
    path = store.ledger_dir.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class Registry:
    def __init__(self, store, source_id):
        self.store = store
        self.source_id = source_id
        self.events_path = store.ledger_file(EVENTS)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.versions = self._load_view(VERSIONS_VIEW, {})
        self.pending = self._load_view(PENDING_VIEW, {})

    def _load_view(self, parts, default):
        path = _view_path(self.store, parts)
        return self.store.read_json(path) if path.exists() else default

    def _save_views(self):
        self.store.write_json(_view_path(self.store, VERSIONS_VIEW), self.versions)
        self.store.write_json(_view_path(self.store, PENDING_VIEW), self.pending)

    def next_round_id(self):
        """轮次序号即版本顺序，不依赖任何时间戳。

        以轮次文档为准而非事件数：中断未入册的轮次也已占用序号，
        恢复时必须续用同一序号，不能新开一轮把两个时刻拼在一起。
        """
        existing = [int(path.stem.split('-')[1])
                    for path in self.store.rounds_dir.glob('round-*.json')
                    if path.stem.split('-')[1].isdigit()]
        return (max(existing) + 1) if existing else 1

    def _iter_events(self):
        if not self.events_path.exists():
            return
        with open(self.events_path, encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)

    # ---- 轮次入册 -----------------------------------------------------

    def seal_round(self, doc):
        if doc['state'] != 'sealed':
            raise ValueError('轮次尚未封存，不能入册')
        if doc['source_id'] != self.source_id:
            raise ValueError('轮次来源身份与账本不符')
        if self._round_recorded(doc['round_id']):
            raise ValueError('轮次 %s 已入册，重复入册被拒绝' % doc['round_id'])
        record = {'round_id': doc['round_id'], 'status': doc['status'],
                  'index_url': doc['index_url'],
                  'opening': doc['opening'], 'closing': doc['closing'],
                  'reasons': list(doc['reasons'])}
        if doc['status'] == 'consistent':
            self._apply_consistent(doc, record)
        elif doc.get('index_drifted'):
            self._apply_drifted(doc, record)
        else:
            self._apply_entry_failures(doc, record)
        self.store.append_jsonl(self.events_path, record)
        self._save_views()
        return record

    def _round_recorded(self, round_id):
        return any(event['round_id'] == round_id for event in self._iter_events())

    def _apply_consistent(self, doc, record):
        index_sha = doc['opening']['response_sha256']
        pending_present = set(self.pending)
        for item in doc['entries']:
            entry_id = item['entry_id']
            if item['tombstone']:
                self._mark_tombstone(entry_id, doc['round_id'], record)
                self.pending.pop(entry_id, None)
                continue
            if item['problems'] or not item['version_id']:
                self._put_pending(entry_id, item, doc, record)
                continue
            current = self.versions.get(entry_id)
            known = set() if current is None else {v['version_id'] for v in current['history']}
            if item['version_id'] not in known:
                self._register_version(entry_id, item, index_sha, doc, record)
            else:
                self._revalidate(entry_id, item['version_id'], doc['round_id'], record)
            self.pending.pop(entry_id, None)
        record['pending_observed'] = sorted(pending_present)

    def _register_version(self, entry_id, item, index_sha, doc, record):
        entry = self.versions.setdefault(entry_id, {
            'entry_id': entry_id, 'withdrawn': False, 'current_version': None,
            'history': []})
        attachment_refs = [{'name': slot['name'], **slot['response']}
                           for slot in item['attachments']]
        version = {
            'version_id': item['version_id'],
            'revision': item['revision'],
            'title': item['title'],
            'manifest_digest': versioning.manifest_digest(
                item['version_id'], index_sha, entry_id),
            'index_response': doc['opening'],
            'body': item['body'],
            'attachments': attachment_refs,
            'confirmed_round': doc['round_id'],
            'last_verified_round': doc['round_id'],
        }
        entry['history'].append(version)
        entry['current_version'] = item['version_id']
        entry['withdrawn'] = False
        record.setdefault('confirmed', []).append(
            {'entry_id': entry_id, 'version_id': item['version_id'],
             'kind': 'new_version'})

    def _revalidate(self, entry_id, version_id, round_id, record):
        entry = self.versions[entry_id]
        for version in entry['history']:
            if version['version_id'] == version_id:
                version['last_verified_round'] = round_id
                break
        record.setdefault('confirmed', []).append(
            {'entry_id': entry_id, 'version_id': version_id, 'kind': 'revalidated'})

    def _mark_tombstone(self, entry_id, round_id, record):
        entry = self.versions.get(entry_id)
        if entry is None:
            self.pending[entry_id] = {
                'entry_id': entry_id, 'tombstone_without_history': True,
                'round_id': round_id, 'reasons': ['索引标记撤回，但无已确认历史版本']}
            return
        entry['withdrawn'] = True
        record.setdefault('withdrawn', []).append(entry_id)

    def _put_pending(self, entry_id, item, doc, record):
        self.pending[entry_id] = {
            'entry_id': entry_id, 'round_id': doc['round_id'],
            'title': item['title'], 'revision': item['revision'],
            'body': item['body'],
            'attachments': [{'name': slot['name'], **(slot['response'] or {})}
                            for slot in item['attachments']],
            'index_response': doc['opening'],
            'reasons': list(item['problems']) or list(doc['reasons'])}
        record.setdefault('held_pending', []).append(entry_id)

    def _apply_entry_failures(self, doc, record):
        # 索引快照稳定、仅个别条目未通过：通过的条目正常确认/重验并
        # 解除待核，失败条目逐条登记观察，不触碰其已确认版本。
        index_sha = doc['opening']['response_sha256']
        for item in doc['entries']:
            entry_id = item['entry_id']
            if item['tombstone']:
                self._mark_tombstone(entry_id, doc['round_id'], record)
                self.pending.pop(entry_id, None)
                continue
            if item['problems'] or not item['version_id']:
                self._put_pending(entry_id, item, doc, record)
                continue
            current = self.versions.get(entry_id)
            known = set() if current is None \
                else {v['version_id'] for v in current['history']}
            if item['version_id'] not in known:
                self._register_version(entry_id, item, index_sha, doc, record)
            else:
                self._revalidate(entry_id, item['version_id'],
                                 doc['round_id'], record)
            self.pending.pop(entry_id, None)

    def _apply_drifted(self, doc, record):
        # 抓取途中索引更新：正文与附件无法证明属于哪一个发布时刻，
        # 全部条目只进待核，已确认版本一律不动，绝不拼合两个时刻。
        for item in doc['entries']:
            self.pending[item['entry_id']] = {
                'entry_id': item['entry_id'], 'round_id': doc['round_id'],
                'title': item['title'], 'revision': item['revision'],
                'tombstone': item['tombstone'],
                'body': item['body'],
                'attachments': [{'name': slot['name'], **(slot['response'] or {})}
                                for slot in item['attachments']],
                'index_response': doc['opening'],
                'reasons': list(item['problems']) or list(doc['reasons'])}
            record.setdefault('held_pending', []).append(item['entry_id'])

    # ---- 交付视图 -----------------------------------------------------

    def build_deliverables(self):
        """检索人员视图：每条数字资料可回到原始响应与附件摘要。"""
        confirmed, pending_items = [], []
        for entry_id, entry in sorted(self.versions.items()):
            current = next(v for v in entry['history']
                           if v['version_id'] == entry['current_version'])
            confirmed.append({
                'entry_id': entry_id,
                'status': 'withdrawn' if entry['withdrawn'] else 'confirmed',
                'title': current['title'],
                'revision': current['revision'],
                'version_id': current['version_id'],
                'manifest_digest': current['manifest_digest'],
                'confirmed_round': current['confirmed_round'],
                'last_verified_round': current['last_verified_round'],
                'provenance': {
                    'index': current['index_response'],
                    'body': current['body'],
                    'attachments': current['attachments'],
                },
            })
        for entry_id, item in sorted(self.pending.items()):
            pending_items.append({'entry_id': entry_id, 'status': 'pending_review',
                                  'round_id': item['round_id'],
                                  'title': item.get('title'),
                                  'reasons': item['reasons'],
                                  'index_response': item.get('index_response'),
                                  'body': item.get('body'),
                                  'attachments': item.get('attachments', [])})
        payload = {'source_id': self.source_id,
                   'confirmed': confirmed, 'pending': pending_items}
        self.store.write_json(_view_path(self.store, DELIVERABLES_VIEW), payload)
        return payload
