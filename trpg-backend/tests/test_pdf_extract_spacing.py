"""取文层保真：PDF 里真实存在的空格不许被吞掉（`exec/30` 步骤 2）。

## 这条用例挡的是什么

真机连续三次导入失败，`numeric` 门报的都是同一个实体，诊断一路指向"PDF 属性表
被拍平了"，于是计划里写着要做视觉取文 + 别写表格识别器。

**再去复现，发现那一页根本不是表格**——是靠宽空白分栏的普通文本行，而
`extract_text()` 的默认 `x_tolerance=3` 是按西文字距定的，中文正文字间距本来
就接近 3，于是它把行内真正的空格一并吞掉：

    页面上：爪击 70  1D6+1D6 …        抽出来：爪击70 1D6+1D6 …

技能名和它的成功率粘成一个词，下游模型再也分不开。**信息是在这一层丢的，
而这一层是纯代码**——同族于「纯代码那一层没有门，所以它的错误会伪装成模型层
的错误」。

## 为什么用手搓的 PDF 而不是真模组

真模组是第三方正文，进不了 git。这里按字符级坐标造两个字符串，间隙精确可控，
在 CI 里也跑得了。两个方向都验：

- 间隙 2.0：默认会粘、我们必须分开   ← 真实症状
- 间隙 1.0：**必须继续粘**            ← 不是把阈值一路调到 0 就完事，
                                        调过头会把汉字之间切碎
"""

from __future__ import annotations

from pathlib import Path

from app.core.module_import.extract import extract_document

#: 12pt Helvetica 下 "DMG" 的排版宽度，用来把第二段文字放到精确的间隙上。
_DMG_WIDTH = 28.0
_LEFT_X = 100.0


def _mini_pdf(gap: float) -> bytes:
    """造一份只有一行的 PDF：`DMG` 和 `70` 之间隔 `gap` 个用户单位。

    手写 PDF 而不是引 reportlab：这里要控的是**字符级坐标**，而多一个只在
    测试里用的排版库，反而会把"间隙到底是多少"藏进它的实现。
    """
    second_x = _LEFT_X + _DMG_WIDTH + gap
    stream = (
        f"BT /F1 12 Tf {_LEFT_X} 700 Td (DMG) Tj ET\n"
        f"BT /F1 12 Tf {second_x} 700 Td (70) Tj ET\n"
        # 垫一段正文：取文层有「每页字符数太少 = 扫描件」的闸门，只有一行会被它拦下。
        f"BT /F1 12 Tf {_LEFT_X} 660 Td (The cabin stands alone at the edge of the wood.) Tj ET\n"
    )
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}endstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets: list[int] = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    return out.encode("latin-1")


def _extract(tmp_path: Path, gap: float) -> str:
    pdf = tmp_path / f"gap{gap}.pdf"
    pdf.write_bytes(_mini_pdf(gap))
    return extract_document(pdf).text


def test_a_real_space_survives_extraction(tmp_path: Path) -> None:
    """🔴 这就是三次导入失败的那个字。

    pdfplumber 的默认阈值会在这个间隙上把两段粘起来；去掉 `x_tolerance=`
    这条就红——它是这次修复的**唯一**载体。
    """
    assert "DMG 70" in _extract(tmp_path, gap=2.0)


def test_letters_inside_a_word_still_stay_together(tmp_path: Path) -> None:
    """另一头也要验：阈值调过头会把词切碎，中文正文尤其明显。

    没有这条，把 `x_tolerance` 设成 0 也能让上面那条通过，而真实模组会变成
    一堆散字——那比粘连更糟，且同样没有东西会红。
    """
    assert "DMG70" in _extract(tmp_path, gap=1.0)
