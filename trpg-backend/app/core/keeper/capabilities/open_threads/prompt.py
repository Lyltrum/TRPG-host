"""open_threads 能力贡献给裁决 prompt 的文本块。"""

from app.core.keeper.contract.registry import PromptBlock

#: 排在 4e（即兴地点）之后 —— 两条是同一族：**即兴出来的东西要有落点**，
#: 一个管"新地方"，一个管"新处境"。编号沿用骨架约定，4a–4e 已被占用 ⇒ 4f。
_RULE_THREADS = PromptBlock(
    slot="rules",
    order=4.95,
    text="""4f. **悬而未决的事**：你即兴出来的、**会持续影响接下来每一轮**的处境——追兵还在后面、门被从外面反锁了、油灯只剩十几分钟、有人受伤走不快——写进 `new_threads`：`[{"text": "米-戈仍在追击"}]`。文字要写成**仍然成立的状态**（「米-戈仍在追击」而不是「米-戈追了上来」），系统会分配一个 `thread-N`。
   🔴 **不写就等于没发生**：这类东西不在剧本里、也不在任何别的字段里，你不记下来，下一轮它就只剩上一段散文里的一句话，你自己都会忘。
   🔴 **了结了必须写 `resolved_threads`**（填局面块列出的 `thread-N`）：摆脱了追击、门撬开了、灯灭了都算了结。不写它就一直挂着，你会被要求一直演一个早就结束的威胁。**结清的时候顺手想一下它留没留下永久后果**（火烧完了 → 那栋房子已经烧毁），留下了就补一条 `new_facts`（见 4h）。
   一次性的小事（推开一扇门、捡起一把钥匙）不要写进来——只写**跨轮成立**的处境。""",
)

_OUTPUT_EXAMPLE_NEW = PromptBlock(
    slot="output_example",
    order=45,
    text='  "new_threads": [{"text": "米-戈仍在追击"}]',
)

_OUTPUT_EXAMPLE_RESOLVED = PromptBlock(
    slot="output_example",
    order=46,
    text='  "resolved_threads": []',
)

PROMPT_BLOCKS = (_RULE_THREADS, _OUTPUT_EXAMPLE_NEW, _OUTPUT_EXAMPLE_RESOLVED)
