"""采集传输层：实时抓取与报文重放走同一接口。

传输层只负责按 (url, source_id) 取回完整响应，不解释实体内容。
重放模式按轮次顺序提供响应，缺包或身份不符直接报错，防止测试
用不同发布时刻的响应拼凑出一轮采集。
"""
from .source import fetch, from_packet


class TransportError(RuntimeError):
    """传输层失败：轮次应保持 open，等待补齐响应后恢复。"""


class ReplayError(TransportError):
    """重放报文与本轮请求不一致或报文已耗尽。"""


class PacketTransport:
    """按请求顺序回放预先保存的来源响应。"""

    def __init__(self, packets):
        self._packets = [from_packet(p) for p in packets]
        self._cursor = 0

    @property
    def consumed(self):
        return self._cursor

    @property
    def remaining(self):
        return len(self._packets) - self._cursor

    def get(self, url, source_id):
        if self._cursor >= len(self._packets):
            raise ReplayError('重放报文已耗尽：%s %s' % (source_id, url))
        response = self._packets[self._cursor]
        self._cursor += 1
        if response.source_id != source_id or response.url != url:
            raise ReplayError('重放报文与请求不符：请求 %s %s，实得 %s %s'
                              % (source_id, url, response.source_id, response.url))
        return response


class HttpTransport:
    """实时读取来源；参数与 source.fetch 保持一致。"""

    def __init__(self, *, headers=None, max_bytes=1048576):
        self._headers = headers
        self._max_bytes = max_bytes

    def get(self, url, source_id):
        return fetch(url, source_id, headers=self._headers, max_bytes=self._max_bytes)
