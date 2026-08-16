"""房间级「AI 主持人行动锁」（issue #107）。

锁防的是什么：一次对 AI 主持人的提交是「读世界状态 → 跑 AI 生成叙事 →
写回状态/广播」的一个循环。两个玩家几乎同时提交时，如果两个循环并发执行，
它们读到的是**同一份旧状态**，各自生成的叙事会互相矛盾（A「我打开门」、
B 同时「我把门锁上」——两次调用都以为门还关着，产出两条打架的剧情分支）。
这跟 AI 聪不聪明无关：它没法对一个还没写回的、正在别处计算中的变化做出
反应。所以同一房间同一时刻只允许一个循环在跑，其他人的提交直接拒绝
（`ACTION_IN_PROGRESS`，不排队不合并——产品形态本来就是"讨论完由一个人
提交"，见 issue #107 关键决策）。

这也如实模拟了真实跑团：守秘人一次只能处理一个人的话，哪怕只是随口一问，
其他人也得等守秘人腾出手。所以锁不区分"行动还是提问"——统一排队。

🔴 超时兜底：锁必须能自己过期。否则一次 AI 调用失败/超时若没走到 release
（代码路径漏了、进程内异常逃逸），房间就永久锁死，之后谁都无法再提交。
规则是：拿锁 → AI 回应完成或超时 → 无条件释放。`held()` 的 finally 保证正常
路径的释放，这里的过期时间是最后一道保险。

🔴 **过期时间兜的是「持锁的协程已经不在了」，不是「这一拍跑得慢」**
（`exec/38 #83`，2026-08-13 夜真机撞到）：玩家提交后整拍跑了 **229 秒**，而
当时 `LOCK_TIMEOUT_SECONDS = 60`（注释写的理由是"给 DeepSeek 客户端 30s 超时
留一倍余量"）。他等不住补发了一句，那一刻锁**早就过期**，第二句被当成新回合
受理——两个「读状态→跑 AI→写回」循环并发跑，各产出一段互相矛盾的叙事。
**锁防的那件事失效了，而没有任何地方报错。**

拍一个更大的常数不解决问题：常数多大都是在猜一拍最慢能有多慢，猜小了并发、
猜大了卡死。改成**持锁期间自动续期**（`held()`）：只要那个协程还活着就一直
把到期时间往后推，进程死了或协程被吞了，续期心跳跟着没了，锁照常过期。
同 `exec/33` 把收集窗口从"2.5 秒定长"改成 `player.typing` 续期的那次修法——
**定长常数换成活性信号。**

🔴 所以拿到锁之后**必须走 `held()`**，不要自己写 `try/finally: release()`：
那样拿到的是一把 60 秒后会自己松开的锁。`release()` 只留给 `held()` 自己调，
有一条 AST 守护测试钉着这件事（`test_architecture.py`）。

实现是**进程内存** dict（跟 ws_manager.ConnectionManager 同一档次的取舍）：
本期单进程部署，多进程/多实例时锁不共享——真到那一步需要换成数据库行锁或
Redis，注释在此立此存照。
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RoomActionLockManager:
    # 持锁期间会自动续期（见 `held`），所以这个数**不需要覆盖最慢的一拍**——
    # 它量的是"多久没有心跳就认定持锁方已经不在了"。
    LOCK_TIMEOUT_SECONDS = 60.0

    def __init__(self) -> None:
        # room_id -> (到期时刻, token)
        # token 用于所有权校验：A 超时后 B 拿到新 token，A 的 finally release
        # 因 token 不匹配而成为空操作，不会误删 B 的锁。
        self._locks: dict[str, tuple[float, str]] = {}

    def try_acquire(self, room_id: str) -> str | None:
        """尝试拿锁：没人持有、或持有者已过期（超时兜底）→ 拿到，返回 token；
        否则返回 None，调用方应拒绝这次提交。"""
        now = time.monotonic()
        entry = self._locks.get(room_id)
        if entry is not None and now < entry[0]:
            return None
        token = str(uuid.uuid4())
        self._locks[room_id] = (now + self.LOCK_TIMEOUT_SECONDS, token)
        return token

    def renew(self, room_id: str, token: str) -> bool:
        """把到期时间往后推一整个超时。返回"这把锁还是不是你的"。

        token 不匹配（自己已经超时、锁被别人拿走了）或锁不存在时返回 False 且
        **什么都不做**——绝不把别人的锁续到自己名下。
        """
        entry = self._locks.get(room_id)
        if entry is None or entry[1] != token:
            return False
        self._locks[room_id] = (time.monotonic() + self.LOCK_TIMEOUT_SECONDS, token)
        return True

    def release(self, room_id: str, token: str) -> None:
        """释放锁。只有持有匹配 token 的调用方才能真正释放——防止超时后旧持有者
        误删新持有者的锁。token 不匹配或锁不存在均为无害空操作。

        🔴 **不要直接调它**，走 `held()`：单独 release 意味着这把锁没人续期。
        """
        entry = self._locks.get(room_id)
        if entry is not None and entry[1] == token:
            del self._locks[room_id]

    @asynccontextmanager
    async def held(self, room_id: str, token: str) -> AsyncIterator[None]:
        """持锁跑一段活儿：期间自动续期，退出时释放。

        ```
        token = action_lock_manager.try_acquire(room_id)
        if token is None: ...
        async with action_lock_manager.held(room_id, token):
            ...          # 想跑多久跑多久
        ```

        🔴 **续期与释放绑在同一个构造里**，是为了让"新加一个持锁的地方"不可能
        只做对一半——此前它们是 6 个各写各的 `try/finally: release()`，加第 7 个
        必然漏掉续期（「逐个列出的地方，加一项就漏一项」）。

        心跳间隔由超时**推导**而不是另设常数：`main.py` 会按配置覆盖
        `LOCK_TIMEOUT_SECONDS`（e2e 设成 180），另设常数就会在覆盖后失配。
        """
        keepalive = asyncio.create_task(self._keepalive(room_id, token))
        try:
            yield
        finally:
            # cancel() 与 release() 之间没有 await：单线程事件循环下这中间插不进
            # 任何一次续期，不会出现"释放之后又被自己续回来"。
            keepalive.cancel()
            self.release(room_id, token)

    async def _keepalive(self, room_id: str, token: str) -> None:
        # 三分之一个超时一次：一次调度抖动或一次长 GC 不至于让锁在两跳之间过期。
        interval = self.LOCK_TIMEOUT_SECONDS / 3
        while True:
            await asyncio.sleep(interval)
            if not self.renew(room_id, token):
                # 已经不是自己的锁了（进程曾经停摆、超时被别人抢走）。再续就是
                # 抢别人的，直接收工，让正常的 finally 去做无害的 release。
                return


action_lock_manager = RoomActionLockManager()
