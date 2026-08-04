"""读 `.doc`（Word 97-2003 的 OLE2 复合文档）。

## 为什么自己解，不调外部工具

`.doc` 在中文跑团圈非常常见（本仓库的科比特先生就是），**不能拒绝**。
三条路：

| | 取舍 |
|---|---|
| LibreOffice headless (`soffice --convert-to`) | 最可靠，但要装 ~500 MB 的部署依赖 |
| `antiword` | 轻量，但已不维护、且仍是外部二进制 |
| **纯 Python（本文件）** | **零部署依赖**；覆盖不到的情形显式失败 |

选第三条。`olefile` 只负责读 OLE2 容器（MIT），**Word 的分片表由本文件自己解**。

## 格式要点（Word 97+，nFib ≥ 193）

正文**不是**连续存放的，它被切成若干「片」（piece），位置记在 **piece table**：

1. `WordDocument` 流开头是 FIB。偏移 `0x000A` 的标志位第 9 位（`fWhichTblStm`）
   决定表流叫 `1Table` 还是 `0Table`。
2. FIB 偏移 `0x01A2` 是 `fcClx`、`0x01A6` 是 `lcbClx`——Clx 在表流里的位置与长度。
3. Clx 里跳过 `0x01`（Prc）条目，找到 `0x02`（Pcdt）：它是一张 PLCF，前半是
   `n+1` 个字符位置（CP），后半是 `n` 个 8 字节的 PCD。
4. 每个 PCD 的 `fc` 字段第 30 位是**压缩标志**：置位表示这一片用单字节
   （CP1252/本地代码页）存在 `fc/2` 处；未置位表示 UTF-16LE 存在 `fc` 处。

🔴 **第 4 步那个压缩位是最容易搞错的地方**：漏掉它，中文文档会抽出一堆乱码
或长度翻倍的鬼字符——而且**不会报错**，正是「静默产出半份文本」那类最坏情况。
所以调用方一定要拿字符数对一次账（本仓库的科比特先生：元数据 17716 字）。
"""

from __future__ import annotations

import struct
from pathlib import Path

#: FIB 里各字段的偏移（Word 97+）。
_FIB_FLAGS = 0x000A
_FIB_FC_CLX = 0x01A2
_FIB_LCB_CLX = 0x01A6

#: PCD.fc 的第 30 位：置位 = 单字节压缩存储。
_FC_COMPRESSED = 0x40000000
_FC_ADDRESS_MASK = 0x3FFFFFFF

#: 单字节片用的代码页。中文 Word 存的是 GBK；解不出时退回 CP1252。
_ANSI_CODEPAGES = ("gbk", "cp1252")


class LegacyDocError(ValueError):
    """这份 `.doc` 本解析器啃不动。"""


def extract_doc_text(path: Path) -> str:
    """抽出 `.doc` 的正文。解析不出来时抛 `LegacyDocError`（**不返回半份**）。"""
    import olefile

    if not olefile.isOleFile(str(path)):
        raise LegacyDocError("不是 Word 97-2003 文档（OLE2 容器都不是）")

    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("WordDocument"):
            raise LegacyDocError("OLE2 容器里没有 WordDocument 流，可能不是 Word 文档")
        word_stream = ole.openstream("WordDocument").read()

        flags = struct.unpack_from("<H", word_stream, _FIB_FLAGS)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise LegacyDocError(f"缺少 {table_name} 流，无法定位正文分片表")
        table_stream = ole.openstream(table_name).read()

    fc_clx = struct.unpack_from("<I", word_stream, _FIB_FC_CLX)[0]
    lcb_clx = struct.unpack_from("<I", word_stream, _FIB_LCB_CLX)[0]
    if lcb_clx == 0 or fc_clx + lcb_clx > len(table_stream):
        raise LegacyDocError("分片表位置越界，文件可能损坏")

    pieces = _parse_piece_table(table_stream[fc_clx : fc_clx + lcb_clx])
    if not pieces:
        raise LegacyDocError("分片表为空，读不出正文")

    out: list[str] = []
    for cp_start, cp_end, fc in pieces:
        n_chars = cp_end - cp_start
        if n_chars <= 0:
            continue
        if fc & _FC_COMPRESSED:
            start = (fc & _FC_ADDRESS_MASK) // 2
            raw = word_stream[start : start + n_chars]
            out.append(_decode_ansi(raw))
        else:
            start = fc & _FC_ADDRESS_MASK
            raw = word_stream[start : start + n_chars * 2]
            out.append(raw.decode("utf-16-le", errors="replace"))

    return _clean("".join(out))


def _parse_piece_table(clx: bytes) -> list[tuple[int, int, int]]:
    """Clx → `[(cpStart, cpEnd, fc), …]`。"""
    pos = 0
    pcdt: bytes | None = None
    while pos < len(clx):
        kind = clx[pos]
        if kind == 0x01:  # Prc：跳过（长度前缀是 2 字节）
            if pos + 3 > len(clx):
                break
            size = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + size
        elif kind == 0x02:  # Pcdt：正文分片表
            if pos + 5 > len(clx):
                break
            size = struct.unpack_from("<I", clx, pos + 1)[0]
            pcdt = clx[pos + 5 : pos + 5 + size]
            break
        else:
            break

    if not pcdt:
        return []

    # PLCF：n+1 个 4 字节 CP，随后 n 个 8 字节 PCD
    n = (len(pcdt) - 4) // 12
    if n <= 0:
        return []
    cps = [struct.unpack_from("<I", pcdt, i * 4)[0] for i in range(n + 1)]
    pieces: list[tuple[int, int, int]] = []
    pcd_base = (n + 1) * 4
    for i in range(n):
        fc = struct.unpack_from("<I", pcdt, pcd_base + i * 8 + 2)[0]
        pieces.append((cps[i], cps[i + 1], fc))
    return pieces


def _decode_ansi(raw: bytes) -> str:
    for codec in _ANSI_CODEPAGES:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode(_ANSI_CODEPAGES[-1], errors="replace")


def _clean(text: str) -> str:
    """把 Word 的控制字符换成可读的分隔。

    `\\r` 是段落结束、`\\x07` 是单元格/行结束、`\\x0c` 是分页、
    `\\x13`–`\\x15` 是域代码的括号（内容对我们没用）。
    """
    trans = {
        0x0D: "\n",
        0x07: "\n",
        0x0C: "\n",
        0x0B: "\n",
        0x1E: "-",
        0x1F: "",
        0x13: "",
        0x14: "",
        0x15: "",
        0x08: "",
        0x01: "",
    }
    return "".join(trans.get(ord(ch), ch) for ch in text)
