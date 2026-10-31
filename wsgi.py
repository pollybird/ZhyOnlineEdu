"""WSGI 入口。

开发：``.venv/bin/flask --app wsgi run --port 5000``
生产：``.venv/bin/gunicorn -w 4 -k gthread --threads 16 -b 127.0.0.1:5000 wsgi:app``

``create_app`` 需要显式传入能力探测器（``InstallProbe``，安装期把方言/upsert 等
能力位固化进 ``sys_compat_flags``），因此统一走这个入口，避免各环境自行拼装。
``var/wsgi.py`` 是本地压测用的同名入口（``var/`` 已 gitignore）。
"""

from __future__ import annotations

from zhyonlineedu.app import create_app
from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.db.probe import InstallProbe

config = load_config()
app = create_app(config=config, probe=InstallProbe(config))
