"""钟毓在线教育系统内核（ZhyOnlineEdu）。

版本号唯一来源：`__version__`。命名规则见 README「版本号规则」：
正式发布前大版本号恒为 0，版本号与里程碑一一对应，
形如 ``0.<里程碑主号>.<里程碑次号>-alpha``（如完成里程碑 M2.15 → 0.2.15-alpha）。

发版前必须同步 `pyproject.toml`、`web/package.json`、`web/src/api/openapi.json`；
`scripts/check_version.py` 在 CI 中校验各处一致性与命名规则。
"""

__version__ = "0.3.4-alpha"
