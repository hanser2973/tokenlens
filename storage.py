# -*- coding: utf-8 -*-
"""JSON 文件的可靠读写：原子写入 + 损坏自动备份。

所有数据文件（records/plans/auto_usage/state/quota_cal 等）统一走这里：
避免写到一半崩溃留下截断文件，也避免读到损坏文件后当空数据把原文件覆盖。
"""
import json
import os
import threading
import time


def load_json(path, default):
    """读取 JSON，文件不存在或读取失败返回 default。

    解析失败（文件损坏）时先把原文件改名备份（<文件名>.corrupt-<时间戳>），
    防止后续写入把尚有残留内容的原文件覆盖。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        try:
            os.replace(path, f"{path}.corrupt-{int(time.time())}")
        except OSError:
            pass
        return default
    except OSError:
        return default


def save_json(path, obj, indent=None):
    """原子写入 JSON：先写同目录临时文件再 os.replace，并发读不会看到半截文件。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)
