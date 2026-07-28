# -*- coding: utf-8 -*-
"""多端用量同步：通过一个共享目录交换各设备的采集结果。

为什么这样设计：
- 同一个账号在多台电脑上使用时，官方接口给的百分比是账号级的，而本机日志只有本机
  那一份。两者相除得到的「绝对额度」会按本机用量占比系统性低估（见 README/额度说明）。
  把各设备的采集结果汇总起来，才能让绝对值重新可信。
- 每台设备只写自己的 tokenlens-<device_id>.json、只读其他设备的文件，天然没有写冲突，
  也就不需要合并算法。任何把远端目录映射成本地目录的方案（OneDrive / 坚果云 /
  Syncthing / SMB 共享）都能直接用，不引入额外依赖。
- 导出内容只有 日期|Agent|模型 的聚合数值和分钟桶，不含文件路径、项目名等本机隐私，
  因此放进网盘也不会泄露工作内容。

不适用的情况：如果两台机器之间同步的是 AI 工具自身的日志目录（如把 ~/.claude 也放进
网盘），两边会扫到同一批事件，汇总后重复计数。这种情况请不要开启多端合并。
"""
import os
import socket
import time
import uuid

import collector
import storage

DEVICE_FILE = "device.json"
FILE_PREFIX = "tokenlens-"
FILE_SUFFIX = ".json"
STALE_AFTER = 3 * 86400  # 超过 3 天没更新的对端标记为可能离线


def device_identity(data_dir):
    """返回本机 {'id', 'name'}，首次调用时生成并持久化。"""
    path = os.path.join(data_dir, DEVICE_FILE)
    d = storage.load_json(path, {})
    if not isinstance(d, dict) or not d.get("id"):
        d = {"id": uuid.uuid4().hex[:12], "name": socket.gethostname() or "unknown"}
        storage.save_json(path, d)
    return d


def rename_device(data_dir, name):
    """改本机显示名（只影响多端页面上的展示）。"""
    path = os.path.join(data_dir, DEVICE_FILE)
    d = device_identity(data_dir)
    d["name"] = str(name).strip()[:40] or d["name"]
    storage.save_json(path, d)
    return d


def export(data_dir, sync_dir):
    """把本机采集结果写进同步目录，返回写出的条目数。sync_dir 无效时抛 OSError。"""
    ident = device_identity(data_dir)
    payload = {
        "device_id": ident["id"],
        "device_name": ident["name"],
        "updated": int(time.time()),
        "days": storage.load_json(os.path.join(data_dir, collector.AUTO_FILE), {}),
        "minutes": collector.load_minutes(data_dir),
        # Codex 的官方限额只存在于本机日志快照里，另一台机器用完后本机是看不到的。
        # 一并导出，让各端都能取到全账号最新的那一份。
        "rate_limits": collector.latest_rate_limits(),
    }
    os.makedirs(sync_dir, exist_ok=True)
    storage.save_json(os.path.join(sync_dir, FILE_PREFIX + ident["id"] + FILE_SUFFIX), payload)
    return len(payload["days"]) + len(payload["minutes"])


def read_peers(data_dir, sync_dir):
    """读取同步目录里其他设备的文件，返回 [{device_id, device_name, updated, stale, days, minutes}]。

    自己的文件、格式不对的文件都会被跳过；读不到目录返回空列表。
    """
    if not sync_dir:
        return []
    ident = device_identity(data_dir)
    try:
        names = sorted(os.listdir(sync_dir))
    except OSError:
        return []
    now = time.time()
    peers = []
    for name in names:
        if not (name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX)):
            continue
        d = storage.load_json(os.path.join(sync_dir, name), None)
        if not isinstance(d, dict) or not d.get("device_id"):
            continue
        if d["device_id"] == ident["id"]:
            continue
        updated = int(d.get("updated") or 0)
        peers.append({
            "device_id": d["device_id"],
            "device_name": d.get("device_name") or d["device_id"][:6],
            "updated": updated,
            "stale": bool(updated and now - updated > STALE_AFTER),
            "days": d.get("days") if isinstance(d.get("days"), dict) else {},
            "minutes": d.get("minutes") if isinstance(d.get("minutes"), dict) else {},
            "rate_limits": d.get("rate_limits") if isinstance(d.get("rate_limits"), dict) else None,
        })
    return peers


def newest_rate_limits(local, peers):
    """在本机与各对端的 Codex 限额快照里挑时间戳最新的一份，返回 (快照, 来源设备名)。

    Codex 的百分比来自服务端下发、写进日志的快照。多端时本机那份可能已经过时——
    另一台电脑之后的消耗不会体现在本机日志里，直到本机再跑一次 Codex。
    """
    best, source = local, None
    best_ts = str((local or {}).get("timestamp") or "")
    for p in peers:
        rl = p.get("rate_limits")
        ts = str((rl or {}).get("timestamp") or "")
        if rl and ts > best_ts:
            best, best_ts, source = rl, ts, p["device_name"]
    return best, source


def peer_records(peers, prices, calc_cost_fn):
    """把对端的日聚合转成与本机记录相同的结构（source=remote，带 device 字段）。"""
    rows = []
    for p in peers:
        for key, v in p["days"].items():
            parsed = collector.split_auto_key(key)
            if not parsed:
                continue
            day, agent, model, project = parsed
            rows.append({
                "id": f"remote-{p['device_id']}-{key}",
                "date": day,
                "agent": agent,
                "model": model,
                "project": project,
                "requests": int(v.get("requests") or 0),
                "input": int(v.get("input") or 0),
                "output": int(v.get("output") or 0),
                "cached": int(v.get("cached") or 0),
                "cost": calc_cost_fn(prices, model, int(v.get("input") or 0),
                                     int(v.get("output") or 0), int(v.get("cached") or 0)),
                "source": "remote",
                "device": p["device_name"],
            })
    return rows


def peer_usage_since(peers, start_ts, kind=None, agent=None):
    """汇总所有对端在 start_ts 之后的用量（分钟桶精度，误差 < 60s）。"""
    total = {"input": 0, "output": 0, "cached": 0, "requests": 0}
    for p in peers:
        part = collector.minutes_usage_since(p["minutes"], start_ts, kind=kind, agent=agent)
        for f in total:
            total[f] += part[f]
    return total
