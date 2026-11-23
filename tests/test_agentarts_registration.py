import pytest
from fastapi.testclient import TestClient

from careguide.agentarts_registration import app, campus_notice


@pytest.mark.parametrize("question", [
    "我只去泰州市人民医院海陵院区，怎么挂号？",
    "我不去总院区，去海陵院区。",
    "总院区和海陵院区都怎么挂号？",
    "我要去迎春院区，怎么挂号？",
    "我想去人民医院，怎么挂号？",
])
def test_gateway_withholds_main_campus_evidence(monkeypatch, question):
    monkeypatch.setenv("CAREGUIDE_AGENTARTS_KEY", "test-key")
    with TestClient(app) as client:
        response = client.post("/registration", json={"question": question},
                               headers={"X-CareGuide-Key": "test-key"})
    assert response.status_code == 200
    result = response.json()
    assert result["evidence"] == [] and not result["hospital"]
    assert "总院区" in result["message"]


@pytest.mark.parametrize("question", [
    "我想用手机挂泰州市人民医院总院区的号，怎么操作？",
    "我去总院区，不去海陵院区，挂号要带什么？",
    "不是海陵院区，是总院区，怎么预约？",
])
def test_main_campus_can_proceed(question):
    assert campus_notice(question) == ""
