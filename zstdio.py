# -*- coding: utf-8 -*-
"""读取「多个独立 zstd frame 首尾拼接」的日志文件。

DeepSeek Harness 的会话日志就是这种形态：一个只装会话头的 frame，之后每批追加
写一个 frame。帧边界因此天然对齐字节偏移，能像 jsonl 一样按偏移增量读 —— 前提是
能解压。

**扫描帧边界不需要解压**：帧头和每个块头里都写着长度，`complete_prefix()` 只读这些
长度，纯标准库就能算出「文件尾部这一帧写完了没有」。真正的解压要做 Huffman/FSE
熵解码，标准库到 Python 3.14（`compression.zstd`）才有，所以这里按
compression.zstd → zstandard → pyzstd 的顺序探测；一个都没有时 `available()`
返回 False，调用方跳过这个采集源，而不是让整轮采集失败。
"""

MAGIC = 0xFD2FB528
SKIPPABLE_LO, SKIPPABLE_HI = 0x184D2A50, 0x184D2A5F
_DICT_ID_SIZE = (0, 1, 2, 4)   # 帧头描述符低 2 位 -> 字典 id 字段字节数
_FCS_SIZE = (0, 2, 4, 8)       # 高 2 位 -> 解压后大小字段字节数


def _import_backend():
    """挑一个可用的解压实现，返回 (名字, decompress)；都没有返回 (None, None)。"""
    try:
        from compression import zstd  # Python 3.14+ 标准库
        return "compression.zstd", zstd.decompress
    except ImportError:
        pass
    try:
        import zstandard
        # dsh 是流式写的，帧头里没有解压后大小，简单 decompress() 会拒绝；
        # decompressobj 不需要预先知道大小，一个对象解一帧。
        dctx = zstandard.ZstdDecompressor()

        def decompress(frame):
            return dctx.decompressobj().decompress(frame)

        return "zstandard", decompress
    except ImportError:
        pass
    try:
        import pyzstd
        return "pyzstd", pyzstd.decompress
    except ImportError:
        pass
    return None, None


BACKEND, _decompress = _import_backend()


def available():
    """本机有没有 zstd 解压能力。"""
    return _decompress is not None


def frame_end(buf, off=0):
    """buf[off:] 这一帧的结束偏移；字节还不够（帧没写完）返回 None。

    只走帧头 + 块头链，不解压。块头是 3 字节小端：bit0 是「最后一块」标志，
    bit1-2 是块类型（0 原样 / 1 RLE / 2 压缩 / 3 保留），其余高位是块长度。
    RLE 块在文件里只占 1 字节（重复的那个字节），长度字段是解压后的长度。
    """
    if len(buf) - off < 4:
        return None
    magic = int.from_bytes(buf[off:off + 4], "little")
    if SKIPPABLE_LO <= magic <= SKIPPABLE_HI:
        if len(buf) - off < 8:
            return None
        end = off + 8 + int.from_bytes(buf[off + 4:off + 8], "little")
        return end if end <= len(buf) else None
    if magic != MAGIC:
        raise ValueError("不是 zstd 帧")

    p = off + 4
    if p >= len(buf):
        return None
    desc = buf[p]
    p += 1
    single = (desc >> 5) & 1
    if not single:
        p += 1                                  # 窗口描述符
    p += _DICT_ID_SIZE[desc & 3]
    # single segment 且没有大小标志时，大小字段仍占 1 字节（规范如此）
    p += 1 if (desc >> 6) == 0 and single else _FCS_SIZE[desc >> 6]

    while True:
        if p + 3 > len(buf):
            return None
        head = int.from_bytes(buf[p:p + 3], "little")
        p += 3
        kind = (head >> 1) & 3
        if kind == 3:
            raise ValueError("保留的块类型")
        p += 1 if kind == 1 else head >> 3
        if head & 1:
            break
    if (desc >> 2) & 1:
        p += 4                                  # 内容校验和
    return p if p <= len(buf) else None


def frames(buf):
    """切出 buf 里所有完整帧的 (起, 止)。遇到写了一半或损坏的帧就停在那儿。"""
    out, off = [], 0
    while off < len(buf):
        try:
            end = frame_end(buf, off)
        except ValueError:
            break
        if end is None or end <= off:
            break
        out.append((off, end))
        off = end
    return out


def complete_prefix(buf):
    """buf 里被完整帧覆盖的字节数 —— 增量读能安全推进到的偏移。"""
    fr = frames(buf)
    return fr[-1][1] if fr else 0


def decode(buf):
    """解压 buf 里所有完整帧并拼起来。没有解压后端时返回 b''。

    某一帧解不开就停下，只交出前面已经解出的部分：后面的帧未必坏，但一个解不开的
    帧往往意味着这个文件的理解方式出了问题，继续往下猜不如少给一点。
    """
    if _decompress is None:
        return b""
    out = []
    for start, end in frames(buf):
        try:
            out.append(_decompress(buf[start:end]))
        except Exception:
            break
    return b"".join(out)


def decode_first(buf):
    """只解第一帧（dsh 的会话头就单独占一帧）。解不出返回 b''。"""
    if _decompress is None:
        return b""
    try:
        end = frame_end(buf, 0)
    except ValueError:
        return b""
    if not end:
        return b""
    try:
        return _decompress(buf[:end])
    except Exception:
        return b""
