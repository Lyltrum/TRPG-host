"""回合收集窗口（exec/14 P5.1）—— 让"汇总发生在裁决之前"。

## 在此之前：一次只听一个人

`action_lock` 的原设计是"同一房间同一时刻只允许一个『读状态→跑 AI→写回』
循环，其他人的提交**直接拒**（`ACTION_IN_PROGRESS`，不排队不合并）"。

锁本身的理由是对的、必须保留：两个循环并发会读到**同一份旧状态**，产出
互相打架的剧情（A「我打开门」、B 同时「我把门锁上」）。

但"直接拒"这一半是错的。真人守秘人**同时听四个人说话，然后回应一次**，
他从不说"你等他说完再说"。四人桌上最刺眼的不是 AI 说错话，是它一次只肯
听一个人。

> **汇总必须发生在裁决之前**——而原来的架构在裁决之前就把其他人拒掉了。

## 做法：把锁的粒度从"一次一人"改成"一次一轮"

第一个提交者开一个短窗口并持有锁；窗口内其他人的提交**并入同一轮**（原话
照常广播，大家立刻看得见），窗口结束后一次裁决处理全部宣告、一次叙事。

窗口关闭后仍在跑裁决时再提交，才回到原来的 `ACTION_IN_PROGRESS`——那时拒绝
是对的，因为世界状态确实正在被改写。

## 🔴 单人局窗口 = 0

只有一名已连接玩家时窗口为零，行为与加这个功能之前**逐字一致**（不多等一
毫秒）。否则每轮凭空加几秒延迟，是纯粹的倒退。这条由退化测试守着。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Submission:
    """一次玩家宣告。"""

    player_id: str
    nickname: str
    utterance: str
    #: 玩家自己勾了「私密行动」（exec/18 ⑥）。只影响**投递**：结果只回给他，
    #: 守秘人照常看得见并正常裁定——私密是玩家↔玩家，不是玩家↔KP。
    private: bool = False


@dataclass
class _Buffer:
    submissions: list[Submission] = field(default_factory=list)


class TurnWindowManager:
    """房间级的本轮宣告缓冲。进程内存（同 `ConnectionManager`/`action_lock`
    的取舍，本期单进程部署）。"""

    #: 多人局的收集窗口长度。取值权衡：太短收不齐（玩家打字有先后），太长
    #: 每轮都在等。真人桌上"大家七嘴八舌说完"大约就是这个量级。
    WINDOW_SECONDS = 2.5

    def __init__(self) -> None:
        self._buffers: dict[str, _Buffer] = {}

    def window_seconds(self, connected_players: int) -> float:
        """单人局为 0——退化到"提交即裁决"，与本功能上线前逐字一致。"""
        return self.WINDOW_SECONDS if connected_players > 1 else 0.0

    def join(self, room_id: str, submission: Submission) -> bool:
        """把一条宣告并入本轮。

        返回 True 表示**本轮由这一条开启**（调用方负责拿锁、等窗口、跑裁决）；
        返回 False 表示已有人在收集，本条只需广播原话即可。
        """
        buffer = self._buffers.get(room_id)
        if buffer is None:
            self._buffers[room_id] = _Buffer(submissions=[submission])
            return True
        buffer.submissions.append(submission)
        return False

    def is_collecting(self, room_id: str) -> bool:
        return room_id in self._buffers

    def pending_count(self, room_id: str) -> int:
        """本轮已收到几条宣告。调用方用它**提前收窗**——在场的人都已经说过话了
        就没必要再等满窗口（见 ws.py 的等待循环）。这是纯赚的：不牺牲"多人合并
        成一拍"，只砍掉所有人都到齐之后的那段白等。"""
        buffer = self._buffers.get(room_id)
        return len(buffer.submissions) if buffer is not None else 0

    def drain(self, room_id: str) -> list[Submission]:
        """取走本轮全部宣告并关闭窗口。之后再提交就是新的一轮（或被锁拒）。"""
        buffer = self._buffers.pop(room_id, None)
        return list(buffer.submissions) if buffer is not None else []


def merge_utterances(submissions: list[Submission]) -> str:
    """把多人宣告合成一段给裁决器看的文本。

    单条时**返回原话本身**（不加昵称前缀）——保证单人局的 prompt 与本功能
    上线前逐字一致，这是退化证明的一部分。
    """
    if len(submissions) == 1:
        return submissions[0].utterance
    return "\n".join(f"{s.nickname}：{s.utterance}" for s in submissions)


turn_window_manager = TurnWindowManager()
