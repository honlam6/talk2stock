"""基线实验用的轻量操作日志。

记录每个 /api 请求的服务端耗时与结果元数据（谁、哪个接口、多久、成功与否、
是否触发澄清、单据号）。不记录原话——原话在 audit.jsonl。

写入失败绝不阻断业务；文件按 10MB 轮转，保留 5 份。
"""
import datetime
import json
import os

from . import config as cfgmod

_MAX_BYTES = 10 * 1024 * 1024
_KEEP = 5


def _rotate(path):
    try:
        if os.path.getsize(path) < _MAX_BYTES:
            return
        for i in range(_KEEP - 1, 0, -1):
            src = path if i == 1 else "%s.%d" % (path, i)
            dst = "%s.%d" % (path, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
    except OSError:
        pass


def append(event, **fields):
    try:
        path = os.path.join(cfgmod.data_dir(), "oplog.jsonl")
        _rotate(path)
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
        }
        rec.update({k: v for k, v in fields.items() if v is not None})
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
