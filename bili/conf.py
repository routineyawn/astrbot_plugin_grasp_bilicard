"""配置读取：四组配置（entry / visual / text / safety）统一取项。

配置项由 `_conf_schema.json` 定义，面板把值写进这四组里。插件内部只关心"取一项"，
不关心它落在哪一组——以后加组、挪项、改名，只动这里和 schema，不碰业务代码。

`Conf` 不做类型转换，也不缓存：拿到什么返回什么，转换留给调用处（那里的默认值
和取值范围是业务知识，不该塞进配置层）。
"""

# 取值顺序：按这个顺序找，先命中先返回。顺序会影响同名键的优先级。
GROUPS = ("entry", "visual", "text", "safety")


class Conf:
    """一个只读的配置视图，包住 AstrBotConfig（本质是 dict）。"""

    __slots__ = ("raw",)

    def __init__(self, raw) -> None:
        self.raw = raw if raw is not None else {}

    def group(self, name: str) -> dict:
        """取一组配置；缺失或类型不对时返回空字典。"""
        part = self.raw.get(name)
        return part if isinstance(part, dict) else {}

    def get(self, key: str, default=None):
        """按 GROUPS 的顺序找 key，找不到返回 default。"""
        for grp in GROUPS:
            part = self.group(grp)
            if key in part:
                return part[key]
        return default

    def flat(self) -> dict:
        """四组拍平成一份，后出现的组覆盖前面的同名键。"""
        out: dict = {}
        for grp in GROUPS:
            out.update(self.group(grp))
        return out
