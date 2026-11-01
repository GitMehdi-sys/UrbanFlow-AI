"""Verify data requirements and conservation of water volume."""
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from agentsumo.core import AgentSUMOConfig
from web.app import create_app


def test_water_requires_measurements_and_preserves_balance(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentSUMOConfig, 'OUTPUT_DIR', tmp_path)
    with TestClient(create_app()) as client:
        assert client.post('/api/water/forecast', json={}).status_code == 409
        # Small, isolated numerical test fixture. No data is seeded in the application.
        measured = {'source':'Unit test', 'observed_at':datetime.now(timezone.utc).isoformat(),
                    'production_m3_day':24, 'demand_m3_day':72, 'storage_m3':12, 'storage_capacity_m3':30}
        assert client.put('/api/water/measurements', json=measured).status_code == 200
        data = client.post('/api/water/forecast', json={'horizon_hours':24}).json()
        final = data['timeline'][-1]
        assert final['storage_m3'] == 0
        assert final['unmet_m3'] == 36
        measured['production_m3_day'] = 120
        client.put('/api/water/measurements', json=measured)
        final = client.post('/api/water/forecast', json={}).json()['timeline'][-1]
        assert final['storage_m3'] == 30
        assert final['overflow_m3'] == 30
        measured['storage_m3'] = 31
        assert client.put('/api/water/measurements', json=measured).status_code == 422


def test_missing_credentials_never_generate_fake_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentSUMOConfig, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(AgentSUMOConfig, 'CLAUDE_API_KEY_FILE', tmp_path/'missing')
    monkeypatch.setenv('LLM_PROVIDER','anthropic')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with TestClient(create_app()) as client:
        assert client.get('/api/health').json()['ai']['enabled'] is False
        with client.websocket_connect('/ws/chat') as socket:
            socket.receive_json()
            socket.send_json({'type':'chat','message':'Show water data'})
            assert socket.receive_json()['type'] == 'error'
