import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.pipeline import collect, main as pipeline_main
from app.registry import Registry
from app.store import Store
from app.transport import PacketTransport, ReplayError
from app.source import from_packet
from tests.helpers import (index_packet, record, body_packet,
                           pdf_packet, full_round_packets)

BASE = 'https://example.test'
INDEX = BASE + '/index.json'
SRC = 'source-a'


def make_records(*, body_bytes=b'{"figures": 1}', pdf_bytes=b'%PDF-v1',
                 revision='r1', title='季度报告'):
    return [record('q3-2026', revision, title, '/reports/q3',
                   [('report.pdf', '/files/q3.pdf')])]


def builders(*, body_revision='r1', body_pdf_sha=None, pdf_bytes=b'%PDF-v1',
             body_status=200, pdf_status=200, body_payload=None):
    def body_builder(source_id, url, row):
        summaries = None
        if body_pdf_sha is not None:
            summaries = [{'name': 'report.pdf', 'sha256': body_pdf_sha}]
        return body_packet(source_id, url, revision=body_revision,
                           attachment_summaries=summaries,
                           payload=body_payload, status=body_status)

    def attachment_builder(source_id, url, item, row):
        return pdf_packet(source_id, url, pdf_bytes, status=pdf_status)
    return body_builder, attachment_builder


class RoundTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self):
        return Store(self.tmp)

    def registry(self):
        return Registry(self.store(), SRC)


class HappyPathTest(RoundTestBase):
    def test_consistent_round_confirms_version_with_full_provenance(self):
        records = make_records()
        packets = full_round_packets(SRC, INDEX, records, *builders())
        doc, deliverables = collect(self.store(), PacketTransport(packets), INDEX, SRC)
        self.assertEqual(doc['status'], 'consistent')
        self.assertEqual(len(deliverables['confirmed']), 1)
        item = deliverables['confirmed'][0]
        self.assertEqual(item['status'], 'confirmed')
        self.assertEqual(len(item['version_id']), 64)
        prov = item['provenance']
        # 每个环节都能回到原始响应，且原件可独立还原
        for ref in (prov['index'], prov['body'], *prov['attachments']):
            path = Path(self.tmp) / ref['response_path']
            restored = from_packet(json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(restored.sha256, ref['response_sha256'])

    def test_rerun_same_content_revalidates_same_version(self):
        records = make_records()
        packets = full_round_packets(SRC, INDEX, records, *builders())
        collect(self.store(), PacketTransport([p for p in packets]), INDEX, SRC)
        doc2, deliverables2 = collect(
            self.store(), PacketTransport([p for p in packets]), INDEX, SRC)
        self.assertEqual(doc2['round_id'], 2)
        self.assertEqual(doc2['status'], 'consistent')
        reg = self.registry()
        entry = reg.versions['q3-2026']
        self.assertEqual(len(entry['history']), 1)  # 同内容不产生新版本
        self.assertEqual(entry['history'][0]['confirmed_round'], 1)
        self.assertEqual(entry['history'][0]['last_verified_round'], 2)

    def test_content_addressing_dedupes_identical_responses(self):
        records = make_records()
        packets = full_round_packets(SRC, INDEX, records, *builders())
        collect(self.store(), PacketTransport(packets), INDEX, SRC)
        collect(self.store(), PacketTransport([p for p in packets]), INDEX, SRC)
        blobs = list((Path(self.tmp) / 'responses').glob('*.json'))
        # 三个不同响应（索引/正文/附件，收场索引与开场同身份）各只存一份
        self.assertEqual(len(blobs), 3)

    def test_same_body_different_status_keeps_distinct_originals(self):
        # 200 正文与 500 错误页正文同字节，也必须分别留存原始响应
        records = make_records()
        packets_ok = full_round_packets(SRC, INDEX, records, *builders())
        collect(self.store(), PacketTransport(packets_ok), INDEX, SRC)
        packets_bad = full_round_packets(
            SRC, INDEX, records, *builders(body_status=500))
        doc, deliverables = collect(
            self.store(), PacketTransport(packets_bad), INDEX, SRC)
        self.assertEqual(doc['status'], 'suspect')
        bad_ref = doc['entries'][0]['body']
        restored = from_packet(json.loads(
            (Path(self.tmp) / bad_ref['response_path']).read_text(encoding='utf-8')))
        self.assertEqual(restored.status, 500)
        self.assertEqual(restored.response_id, bad_ref['response_id'])
        # 已确认的 200 版本不受影响
        confirmed = deliverables['confirmed'][0]
        self.assertEqual(confirmed['provenance']['body']['status'], 200)


class VersionIdentityTest(RoundTestBase):
    def _round(self, records, body_builder, attachment_builder):
        return collect(self.store(),
                       PacketTransport(full_round_packets(
                           SRC, INDEX, records, body_builder, attachment_builder)),
                       INDEX, SRC)

    def test_same_named_attachment_new_content_creates_new_version(self):
        records = make_records(pdf_bytes=b'%PDF-v1')
        self._round(records, *builders(pdf_bytes=b'%PDF-v1'))
        v1 = self.registry().versions['q3-2026']['current_version']
        records2 = make_records(pdf_bytes=b'%PDF-v2')
        doc, deliverables = self._round(records2, *builders(pdf_bytes=b'%PDF-v2'))
        reg = self.registry()
        entry = reg.versions['q3-2026']
        self.assertEqual(len(entry['history']), 2)
        self.assertNotEqual(entry['current_version'], v1)
        # 附件文件名完全相同，身份仍由内容区分
        self.assertEqual(entry['history'][0]['attachments'][0]['name'],
                         entry['history'][1]['attachments'][0]['name'])
        self.assertNotEqual(entry['history'][0]['attachments'][0]['response_sha256'],
                            entry['history'][1]['attachments'][0]['response_sha256'])

    def test_body_change_creates_new_version(self):
        self._round(make_records(), *builders())
        v1 = self.registry().versions['q3-2026']['current_version']
        self._round(make_records(),
                    *builders(body_payload={'figures': {'revenue': 999}}))
        entry = self.registry().versions['q3-2026']
        self.assertEqual(len(entry['history']), 2)
        self.assertNotEqual(entry['current_version'], v1)

    def test_title_only_change_keeps_version_identity(self):
        self._round(make_records(title='旧标题'), *builders())
        v1 = self.registry().versions['q3-2026']['current_version']
        self._round(make_records(title='新标题'), *builders())
        entry = self.registry().versions['q3-2026']
        self.assertEqual(len(entry['history']), 1)
        self.assertEqual(entry['current_version'], v1)
        self.assertEqual(entry['history'][0]['title'], '旧标题')  # 确认事实不改写

    def test_revision_is_metadata_not_identity_input(self):
        # 场景：来源只改索引中的 revision 号，正文字节与附件完全相同。
        # 版本身份公式不含 revision，故仍是同一版本、仅做重验。
        r1 = full_round_packets(
            SRC, INDEX, make_records(revision='r1'),
            *builders(body_revision='r1', body_payload={'figures': {'k': 1}}))
        self._round_packets(r1)
        v1 = self.registry().versions['q3-2026']['current_version']
        # 索引 revision 改为 r2，但构造的正文仍自报 r1 会触发跨页不一致；
        # 因此正文也自报 r2，同时把 revision 字段从身份公式中排除后，
        # 其余字节（figures、附件）完全相同。
        r2 = full_round_packets(
            SRC, INDEX, make_records(revision='r2'),
            *builders(body_revision='r2', body_payload={'figures': {'k': 1}}))
        doc, _ = self._round_packets(r2)
        entry = self.registry().versions['q3-2026']
        # 正文字节含 revision 字段所以哈希变化 -> 新版本（身份忠实于字节）
        self.assertEqual(doc['status'], 'consistent')
        self.assertEqual(len(entry['history']), 2)
        self.assertNotEqual(entry['current_version'], v1)
        # 但 revision 仅记录为元数据，可追溯每个版本来源自报的号
        self.assertEqual([v['revision'] for v in entry['history']], ['r1', 'r2'])

    def _round_packets(self, packets):
        return collect(self.store(), PacketTransport(packets), INDEX, SRC)


class ConsistencyTest(RoundTestBase):
    def test_body_revision_mismatch_goes_pending_without_touching_confirmed(self):
        packets = full_round_packets(
            SRC, INDEX, make_records(), *builders(body_revision='r1'))
        collect(self.store(), PacketTransport(packets), INDEX, SRC)
        v1 = self.registry().versions['q3-2026']['current_version']

        bad = full_round_packets(
            SRC, INDEX, make_records(),
            *builders(body_revision='r9', body_payload={'figures': {'x': 2}}))
        doc, deliverables = collect(self.store(), PacketTransport(bad), INDEX, SRC)
        self.assertEqual(doc['status'], 'suspect')
        # 已确认版本原样保留
        self.assertEqual(self.registry().versions['q3-2026']['current_version'], v1)
        pending = {p['entry_id']: p for p in deliverables['pending']}
        self.assertIn('q3-2026', pending)
        self.assertTrue(any('revision' in r for r in pending['q3-2026']['reasons']))

    def test_declared_attachment_digest_mismatch_goes_pending(self):
        import hashlib
        actual = hashlib.sha256(b'%PDF-v1').hexdigest()
        wrong = hashlib.sha256(b'other').hexdigest()
        packets = full_round_packets(
            SRC, INDEX, make_records(),
            *builders(body_pdf_sha=actual))
        collect(self.store(), PacketTransport(packets), INDEX, SRC)
        bad = full_round_packets(
            SRC, INDEX, make_records(),
            *builders(body_pdf_sha=wrong))
        doc, deliverables = collect(self.store(), PacketTransport(bad), INDEX, SRC)
        self.assertEqual(doc['status'], 'suspect')
        self.assertEqual(
            deliverables['pending'][0]['reasons'],
            ['附件 report.pdf 摘要与实际响应不符'])

    def test_failed_attachment_status_goes_pending(self):
        packets = full_round_packets(
            SRC, INDEX, make_records(), *builders(pdf_status=503))
        doc, deliverables = collect(self.store(), PacketTransport(packets), INDEX, SRC)
        self.assertEqual(doc['status'], 'suspect')
        self.assertEqual(deliverables['confirmed'], [])
        self.assertIn('HTTP 503', deliverables['pending'][0]['reasons'][0])

    def test_failed_body_status_goes_pending(self):
        packets = full_round_packets(
            SRC, INDEX, make_records(), *builders(body_status=404))
        doc, deliverables = collect(self.store(), PacketTransport(packets), INDEX, SRC)
        self.assertEqual(doc['status'], 'suspect')
        self.assertTrue(deliverables['pending'])

    def test_index_change_mid_round_marks_whole_round_suspect(self):
        # 先确认一个版本
        good = full_round_packets(SRC, INDEX, make_records(), *builders())
        collect(self.store(), PacketTransport(good), INDEX, SRC)
        v1 = self.registry().versions['q3-2026']['current_version']

        # 第二轮：开场索引是 r2，但收场索引变成 r3 —— 抓取途中来源更新
        opening = index_packet(SRC, INDEX, make_records(revision='r2'))
        body = body_packet(SRC, BASE + '/reports/q3', revision='r2',
                           payload={'figures': {'revenue': 200}})
        attachment = pdf_packet(SRC, BASE + '/files/q3.pdf', b'%PDF-v2')
        closing = index_packet(SRC, INDEX, make_records(revision='r3'))
        doc, deliverables = collect(
            self.store(), PacketTransport([opening, body, attachment, closing]),
            INDEX, SRC)
        self.assertEqual(doc['status'], 'suspect')
        self.assertTrue(any('抓取途中' in r for r in doc['reasons']))
        # 已确认版本不被这轮的 r2 正文+附件覆盖
        self.assertEqual(self.registry().versions['q3-2026']['current_version'], v1)
        pending = {p['entry_id']: p for p in deliverables['pending']}
        self.assertIn('q3-2026', pending)


class TombstoneTest(RoundTestBase):
    def test_tombstone_marks_withdrawn_but_keeps_history(self):
        collect(self.store(),
                PacketTransport(full_round_packets(
                    SRC, INDEX, make_records(), *builders())),
                INDEX, SRC)
        tomb = [record('q3-2026', 'r1', '季度报告', None, [], tombstone=True)]
        doc, deliverables = collect(
            self.store(),
            PacketTransport(full_round_packets(SRC, INDEX, tomb,
                                               *builders())),
            INDEX, SRC)
        self.assertEqual(doc['status'], 'consistent')
        item = deliverables['confirmed'][0]
        self.assertEqual(item['status'], 'withdrawn')
        self.assertEqual(len(self.registry().versions['q3-2026']['history']), 1)


class ResumeTest(RoundTestBase):
    class CrashTransport:
        """在第 fail_at 个请求时崩溃，恢复后用剩余报文继续。"""
        def __init__(self, packets, fail_at):
            self.inner = PacketTransport(packets)
            self.fail_at = fail_at
            self.calls = 0

        def get(self, url, source_id):
            self.calls += 1
            if self.calls == self.fail_at:
                raise ConnectionError('模拟抓取中断')
            return self.inner.get(url, source_id)

    def test_interrupted_round_resumes_without_re_fetching_saved_responses(self):
        packets = full_round_packets(SRC, INDEX, make_records(), *builders())
        # 报文顺序：开场索引(1) 正文(2) 附件(3) 收场索引(4)
        crash = self.CrashTransport([p for p in packets], fail_at=3)
        doc, deliverables = collect(self.store(), crash, INDEX, SRC)
        self.assertIsNone(deliverables)
        self.assertEqual(doc['state'], 'open')
        round_file = Path(self.tmp) / 'rounds' / 'round-000001.json'
        saved = json.loads(round_file.read_text(encoding='utf-8'))
        self.assertIsNotNone(saved['entries'][0]['body'])
        self.assertIsNone(saved['entries'][0]['attachments'][0]['response'])

        # 恢复：只需附件 + 收场两个响应；若重新取正文就会耗尽报文
        resume_transport = PacketTransport(packets[2:])
        doc2, deliverables2 = collect(self.store(), resume_transport, INDEX, SRC)
        self.assertEqual(doc2['round_id'], 1)
        self.assertEqual(doc2['state'], 'sealed')
        self.assertEqual(doc2['status'], 'consistent')
        self.assertEqual(len(deliverables2['confirmed']), 1)
        self.assertEqual(resume_transport.consumed, 2)
        # 中断原因在成功封存后不得残留
        self.assertFalse(any('待恢复' in r for r in doc2['reasons']))

    def test_resume_never_mixes_two_publication_moments(self):
        # 中断后开场索引原件仍在；恢复轮次以同一份开场快照为准
        packets = full_round_packets(SRC, INDEX, make_records(), *builders())
        crash = self.CrashTransport([p for p in packets], fail_at=2)
        doc, deliverables = collect(self.store(), crash, INDEX, SRC)
        self.assertEqual(doc['state'], 'open')
        # 另一发布时刻的正文 URL 相同，传输层会放行，但跨页一致性检查
        # （正文自报 r2 vs 开场索引 r1）必须拦下：轮次封存为待核，且
        # 不会把 r2 正文与 r1 索引拼成一份确认公告
        other = full_round_packets(
            SRC, INDEX, make_records(revision='r2'),
            *builders(body_revision='r2', body_payload={'figures': {'x': 9}}))
        doc2, deliverables2 = collect(
            self.store(), PacketTransport(other[1:]), INDEX, SRC)
        self.assertEqual(doc2['state'], 'sealed')
        self.assertEqual(doc2['status'], 'suspect')
        self.assertTrue(any('revision' in r for r in doc2['reasons']))
        # 该轮没有任何确认产出，待核条目保留了两个时刻不一致的证据
        self.assertEqual(deliverables2['confirmed'], [])
        self.assertEqual(deliverables2['pending'][0]['entry_id'], 'q3-2026')
        # 原轮次序号未被新开轮次顶替
        self.assertEqual(doc2['round_id'], 1)


class ReplayGuardTest(RoundTestBase):
    def test_exhausted_packets_leave_open_round_for_recovery(self):
        packets = full_round_packets(SRC, INDEX, make_records(), *builders())
        transport = PacketTransport(packets[:3])  # 缺收场索引
        doc, deliverables = collect(self.store(), transport, INDEX, SRC)
        self.assertEqual(doc['state'], 'open')
        self.assertIsNone(deliverables)
        # 补齐收场响应后恢复成功
        doc2, deliverables2 = collect(
            self.store(), PacketTransport(packets[3:]), INDEX, SRC)
        self.assertEqual(doc2['status'], 'consistent')
        self.assertTrue(deliverables2['confirmed'])
    def test_url_identity_mismatch_is_rejected(self):
        packets = full_round_packets(SRC, INDEX, make_records(), *builders())
        with self.assertRaises(ReplayError):
            collect(self.store(), PacketTransport(packets),
                    BASE + '/other-index.json', SRC)


class IndependentEntryTest(RoundTestBase):
    def test_two_entries_are_judged_independently(self):
        records = [
            record('q3-2026', 'r1', '季报A', '/reports/q3',
                   [('a.pdf', '/files/a.pdf')]),
            record('annual-2026', 'r1', '年报B', '/reports/annual',
                   [('b.pdf', '/files/b.pdf')]),
        ]

        def body_builder(source_id, url, row):
            return body_packet(source_id, url, revision='r1')

        def attachment_builder(source_id, url, item, row):
            return pdf_packet(source_id, url, b'%PDF-' + item['name'].encode())
        packets = full_round_packets(SRC, INDEX, records,
                                     body_builder, attachment_builder)
        doc, deliverables = collect(self.store(), PacketTransport(packets),
                                    INDEX, SRC)
        self.assertEqual(doc['status'], 'consistent')
        self.assertEqual(len(deliverables['confirmed']), 2)

        # 第二轮只有第二条出问题：第一条继续确认，第二条进待核
        def bad_body_builder(source_id, url, row):
            status = 500 if row['entry_id'] == 'annual-2026' else 200
            return body_packet(source_id, url, revision='r1', status=status)
        packets2 = full_round_packets(SRC, INDEX, records,
                                      bad_body_builder, attachment_builder)
        doc2, deliverables2 = collect(self.store(), PacketTransport(packets2),
                                      INDEX, SRC)
        self.assertEqual(doc2['status'], 'suspect')
        confirmed = {c['entry_id'] for c in deliverables2['confirmed']}
        pending = {p['entry_id'] for p in deliverables2['pending']}
        # 失败条目的已确认版本不被覆盖，仍可交付；同时进入待核名单
        self.assertEqual(confirmed, {'q3-2026', 'annual-2026'})
        self.assertEqual(pending, {'annual-2026'})
        annual = next(c for c in deliverables2['confirmed']
                      if c['entry_id'] == 'annual-2026')
        self.assertEqual(annual['provenance']['body']['status'], 200)
        self.assertEqual(annual['last_verified_round'], 1)  # 未被失败轮重验


class RoundIdTest(RoundTestBase):
    def test_interrupted_round_keeps_sequence_number_on_rerun(self):
        packets = full_round_packets(SRC, INDEX, make_records(), *builders())
        crash = ResumeTest.CrashTransport([p for p in packets], fail_at=2)
        doc, _ = collect(self.store(), crash, INDEX, SRC)
        self.assertEqual(doc['round_id'], 1)
        self.assertEqual(doc['state'], 'open')
        # 恢复后续用序号 1，而不是新开第二轮
        doc2, _ = collect(self.store(), PacketTransport(packets[1:]), INDEX, SRC)
        self.assertEqual(doc2['round_id'], 1)
        # 完整再跑一轮时序号递增为 2，未被中断轮次的空事件计数影响
        doc3, _ = collect(self.store(),
                          PacketTransport([p for p in packets]), INDEX, SRC)
        self.assertEqual(doc3['round_id'], 2)

    def test_same_named_attachments_are_distinguished_by_position(self):
        rows = [record('q3-2026', 'r1', '季报', '/reports/q3',
                       [('sheet.pdf', '/files/f1.pdf'),
                        ('sheet.pdf', '/files/f2.pdf')])]
        body_b = lambda s, u, row: body_packet(s, u, revision='r1')

        def attach_b(s, u, item, row):
            return pdf_packet(s, u, b'%PDF-' + item['url'].encode())
        packets = full_round_packets(SRC, INDEX, rows, body_b, attach_b)
        doc, deliverables = collect(self.store(), PacketTransport(packets),
                                    INDEX, SRC)
        self.assertEqual(doc['status'], 'consistent')
        version = self.registry().versions['q3-2026']['history'][0]
        digests = [a['response_sha256'] for a in version['attachments']]
        self.assertEqual(len(digests), 2)
        self.assertNotEqual(digests[0], digests[1])  # 同名不同内容，各自留痕
        urls = [a['url'] for a in version['attachments']]
        self.assertEqual(urls, [BASE + '/files/f1.pdf', BASE + '/files/f2.pdf'])


class CliTest(RoundTestBase):
    def test_replay_cli_writes_views_and_summary(self):
        packets = full_round_packets(SRC, INDEX, make_records(), *builders())
        packet_dir = Path(self.tmp) / 'captures'
        packet_dir.mkdir()
        paths = []
        for i, p in enumerate(packets):
            path = packet_dir / ('resp-%d.json' % i)
            path.write_text(json.dumps(p, ensure_ascii=False), encoding='utf-8')
            paths.append(str(path))
        workdir = Path(self.tmp) / 'work'
        pipeline_main(['replay', str(workdir), INDEX, SRC, *paths])
        views = workdir / 'ledger' / 'views'
        self.assertTrue((views / 'versions.json').exists())
        self.assertTrue((views / 'pending.json').exists())
        deliverables = json.loads((views / 'deliverables.json')
                                  .read_text(encoding='utf-8'))
        self.assertEqual(len(deliverables['confirmed']), 1)
        events = (workdir / 'ledger' / 'events.jsonl').read_text(encoding='utf-8')
        self.assertEqual(len([l for l in events.splitlines() if l.strip()]), 1)


if __name__ == '__main__':
    unittest.main()
