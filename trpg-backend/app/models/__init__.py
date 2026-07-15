# 在这里显式 import 每个模型，确保它们在 `Base.metadata` 里完成注册——
# db.py 的 init_db() 靠 Base.metadata.create_all 建表，只有"被 import 过"的
# 模型才会出现在 metadata 里。以后新增模型（比如 Room），记得在这里也 import 一下。
from app.models.example import Example

__all__ = ["Example"]
