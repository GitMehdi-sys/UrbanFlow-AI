"""Regression checks for independent mapping, safe imports and honest SUMO results."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from agentsumo.core import AgentSUMOConfig
from web.app import create_app
from web.workspace import network_geojson
from agentsumo_mcp.baseline.simulation_runner import SimulationRunner


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentSUMOConfig, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(AgentSUMOConfig, 'MAPBOX_TOKEN_FILE', tmp_path / 'no-token')
    monkeypatch.setattr(AgentSUMOConfig, 'CLAUDE_API_KEY_FILE', tmp_path / 'no-key')
    for key in ['MAPBOX_TOKEN','MAPBOX_ACCESS_TOKEN','ANTHROPIC_API_KEY','GROQ_API_KEY']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('LLM_PROVIDER','anthropic')
    with TestClient(create_app()) as test_client:
        yield test_client


def test_page_and_readiness_work_without_map_or_ai_tokens(client):
    page = client.get('/')
    assert page.status_code == 200
    assert '/static/vendor/maplibre.js' in page.text
    assert 'pk.placeholder' not in page.text
    health = client.get('/api/health').json()
    assert health['server'] == 'ready'
    assert health['ai']['configured'] is False
    assert health['map']['provider'] == 'openstreetmap'


def test_private_map_tokens_are_never_sent_to_browser(client,monkeypatch):
    monkeypatch.setenv('MAPBOX_TOKEN','sk.private-example-for-test')
    response=client.get('/')
    assert response.status_code == 200
    assert 'sk.private-example-for-test' not in response.text
    assert '/static/vendor/maplibre.js' in response.text


@pytest.mark.parametrize('name,body,status', [
    ('../escape.rou.xml',b'<routes/>',400),
    ('bad.txt',b'<routes/>',400),
    ('bad.net.xml',b'<net>',422),
    ('entities.rou.xml',b'<!DOCTYPE x [<!ENTITY a "x">]><routes/>',400),
    ('local.net.xml',b'<net version="1.20"><location netOffset="0,0" convBoundary="0,0,1,1" origBoundary="0,0,1,1" projParameter="!"/></net>',422),
])
def test_invalid_uploads_are_rejected(client,name,body,status):
    assert client.post('/api/upload/asset',files={'file':(name,body)}).status_code == status
    assert not list(AgentSUMOConfig.OUTPUT_DIR.rglob('.import-*'))


def test_import_never_overwrites_assets(client):
    files={'file':('demand.rou.xml',b'<routes/>')}
    assert client.post('/api/upload/asset',files=files).status_code == 200
    assert client.post('/api/upload/asset',files=files).status_code == 409
    assert client.get('/api/scenario/assets').json()['demand'] == ['demand.rou.xml']


def test_report_path_is_confined(client):
    assert client.get('/api/report/%2e%2e%2fREADME.md').status_code == 400


def test_foreign_websites_cannot_modify_local_workspace(client):
    assert client.post('/api/clean',headers={'Origin':'https://unrelated.example'}).status_code == 403


def test_deleted_outputs_do_not_expose_unusable_run_actions(client):
    folder=AgentSUMOConfig.OUTPUT_DIR/'jobs'
    folder.mkdir()
    (folder/'old.json').write_text(json.dumps({'id':'old','status':'completed','created_at':'2026-01-01T00:00:00Z','replay':'missing.json','netstate':'missing.xml'}))
    job=client.get('/api/simulation/jobs').json()['jobs'][0]
    assert job['status']=='completed'
    assert not any(job[key] for key in ['replay_available','heatmap_available','analysis_available'])


def test_duration_validation(client):
    assert client.post('/api/simulation/jobs',json={'network':'x.net.xml','demand':'x.rou.xml','duration':0}).status_code == 422


def test_no_simulation_success_on_process_error(tmp_path):
    result=SimulationRunner()._prepare_result(SimpleNamespace(returncode=1,elapsed=1,stderr='Failed',stdout=''),tmp_path,
        {'tripinfo':'t.xml','netstate_dump':'n.xml','netstate_dump_emission':'e.xml','summary':'s.xml'},tmp_path,300)
    assert result['status'] == 'error'
    assert result['output_files'] == []


def test_scenario_vehicle_types_override_defaults(tmp_path):
    (tmp_path/'vehicle_types.add.xml').write_text('<additional><vType id="passenger"/><vType id="electric"/></additional>')
    (tmp_path/'routes.rou.xml').write_text('<routes><vTypeDistribution id="DEFAULT_VEHTYPE"><vType id="passenger"/></vTypeDistribution></routes>')
    config=SimulationRunner()._generate_config_file(tmp_path,{'net_file':'n.net.xml','route_file':'routes.rou.xml','trip_file':None},[],
        'results.add.xml',{'tripinfo':'t.xml','summary':'s.xml'},300,'test')
    assert config.exists()
    defaults=(tmp_path/'vehicle_types_test.add.xml').read_text()
    assert 'passenger' not in defaults
    assert 'electric' in defaults


def test_missing_ai_key_is_reported_over_websocket(client):
    with client.websocket_connect('/ws/chat') as ws:
        assert ws.receive_json()['type'] == 'state'
        ws.send_json({'type':'chat','message':'Explain the results'})
        messages=[ws.receive_json() for _ in range(2)]
        error=next(m for m in messages if m['type']=='error')
        assert 'ANTHROPIC_API_KEY' in error['data']
        assert messages[-1] == {'type':'thinking','data':False}
