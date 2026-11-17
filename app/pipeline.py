"""公告版本采集流水线与命令行入口。

一条流水线把「一轮采集 + 账本入册 + 交付视图」串成可重复运行的
操作：重跑即重验，已确认版本只追加不覆盖；中断后再次运行会从
轮次文档继续，而不是重新拼一轮。

用法：
    python -m app.pipeline replay <工作目录> <索引URL> <来源ID> <报文.json...>
    python -m app.pipeline live   <工作目录> <索引URL> <来源ID>
"""
import json
import sys
from pathlib import Path

from .registry import Registry
from .round import RoundRunner
from .store import Store
from .transport import HttpTransport, PacketTransport


def _open_round_id(store):
    """返回未封存轮次序号；没有则 None。恢复必须续用该轮次。"""
    for path in sorted(store.rounds_dir.glob('round-*.json')):
        doc = store.read_json(path)
        if doc.get('state') == 'open':
            return doc['round_id']
    return None


def collect(store, transport, index_url, source_id):
    """执行一轮采集并入册，返回 (轮次文档, 交付视图)。

    存在未封存轮次时优先恢复，保证同一发布状态快照的取证不被拆成
    两轮；否则才分配下一个序号开启新轮次。
    """
    registry = Registry(store, source_id)
    round_id = _open_round_id(store)
    if round_id is None:
        round_id = registry.next_round_id()
    doc = RoundRunner(store, transport).run(index_url, source_id, round_id)
    if doc['state'] != 'sealed':  # 传输中断：轮次保持 open，等下次运行恢复
        return doc, None
    registry.seal_round(doc)
    return doc, registry.build_deliverables()


def run_replay(workdir, index_url, source_id, packet_paths):
    packets = [json.loads(Path(p).read_text(encoding='utf-8'))
               for p in packet_paths]
    store = Store(workdir)
    return collect(store, PacketTransport(packets), index_url, source_id)


def run_live(workdir, index_url, source_id, *, headers=None, max_bytes=1048576):
    store = Store(workdir)
    return collect(store, HttpTransport(headers=headers, max_bytes=max_bytes),
                   index_url, source_id)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 4 or argv[0] not in ('replay', 'live'):
        raise SystemExit(__doc__)
    mode, workdir, index_url, source_id = argv[:4]
    if mode == 'replay':
        if len(argv) < 5:
            raise SystemExit('replay 模式至少需要一份来源响应报文')
        doc, deliverables = run_replay(workdir, index_url, source_id, argv[4:])
    else:
        doc, deliverables = run_live(workdir, index_url, source_id)
    summary = {'round_id': doc['round_id'], 'state': doc['state'],
               'status': doc['status'], 'reasons': doc['reasons'],
               'deliverables': '未生成：轮次中断，等待恢复' if deliverables is None
                               else {'confirmed': len(deliverables['confirmed']),
                                     'pending': len(deliverables['pending'])}}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
