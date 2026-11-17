"""工作目录、原件内容存储与原子 JSON 落盘。

所有原件按响应体 sha256 内容寻址，同一字节只存一份；轮次、账本
与交付视图只保存相对路径与摘要，不内嵌实体。写入先落临时文件再
原子替换，保证中断后不会出现半份 JSON。
"""
import json
import os
import tempfile
from pathlib import Path

from .source import to_packet


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.responses_dir = self.root / 'responses'
        self.rounds_dir = self.root / 'rounds'
        self.ledger_dir = self.root / 'ledger'
        for directory in (self.responses_dir, self.rounds_dir, self.ledger_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def rel(self, path):
        return str(Path(path).resolve().relative_to(self.root.resolve()))

    def save_response(self, response):
        """保存一次原始响应，返回 (相对路径, 实体sha256, 字节数)。

        文件名用完整响应身份（状态码+实体摘要），确保同字节不同状态的
        响应各自留痕；ref 中的 response_sha256 仍指实体摘要，用于版本判定。
        """
        body_digest = response.sha256
        path = self.responses_dir / (response.response_id + '.json')
        if not path.exists():
            self.write_json(path, to_packet(response))
        ref_path = self.rel(path)
        return ref_path, body_digest, len(response.body)

    def response_path(self, response_id):
        path = self.responses_dir / (response_id + '.json')
        if not path.exists():
            raise KeyError('原件缺失：%s' % response_id)
        return path

    def round_path(self, round_id):
        return self.rounds_dir / ('round-%06d.json' % round_id)

    def ledger_file(self, name):
        return self.ledger_dir / name

    def write_json(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp',
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def append_jsonl(self, path, value):
        line = json.dumps(value, ensure_ascii=False)
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())
