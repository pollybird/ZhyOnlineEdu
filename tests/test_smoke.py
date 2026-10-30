"""M1.1 冒烟测试：信封 / 错误码 / 健康检查。"""

from zhyonlineedu.app import create_app
from zhyonlineedu.core.codes import Code
from zhyonlineedu.core.constants import ZHY_PROGRESS_METRIC_VERSION


def test_healthz_reports_probe_pending():
    app = create_app()
    client = app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["detail"] == "cap_probe_pending"


def test_error_codes_frozen():
    assert Code.PROGRESS_ACCEPTED == 202
    assert Code.MEDIA_TICKET_EXPIRED == 40201
    assert Code.MEDIA_PLAYBACK_UNRECOVERABLE == 40205
    assert Code.UPLOAD_MALICIOUS == 50103


def test_metric_version_frozen():
    assert ZHY_PROGRESS_METRIC_VERSION == "zhy-progress-v2"
