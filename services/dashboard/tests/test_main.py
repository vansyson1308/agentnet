import pytest
from flask import Flask
from app.main import app, derive_trust_context

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_derive_trust_context_limited_history():
    agent = {
        'total_tasks_completed': 2,
        'total_tasks_failed': 0,
        'total_tasks_timeout': 0,
        'success_rate': 1.0,
        'reputation_tier': 'bronze'
    }
    ctx = derive_trust_context(agent)
    assert ctx['label'] == 'Limited History'
    assert ctx['color'] == '#8b5cf6'
    assert ctx['total'] == 2
    assert ctx['tier'] == 'Bronze'
    assert ctx['success_percent'] == '100.0%'
    assert ctx['timeouts'] == 0

def test_derive_trust_context_highly_reliable():
    agent = {
        'total_tasks_completed': 20,
        'total_tasks_failed': 0,
        'total_tasks_timeout': 0,
        'success_rate': 0.95,
        'reputation_tier': 'gold'
    }
    ctx = derive_trust_context(agent)
    assert ctx['label'] == 'Highly Reliable'
    assert ctx['color'] == '#10b981'

def test_derive_trust_context_frequent_timeout():
    agent = {
        'total_tasks_completed': 10,
        'total_tasks_failed': 1,
        'total_tasks_timeout': 3,
        'success_rate': 0.8,
        'reputation_tier': 'silver'
    }
    ctx = derive_trust_context(agent)
    assert ctx['label'] == 'Frequent Timeout Risk'
    assert ctx['color'] == '#ef4444'

def test_derive_trust_context_occasional_timeout():
    agent = {
        'total_tasks_completed': 20,
        'total_tasks_failed': 2,
        'total_tasks_timeout': 1,
        'success_rate': 0.85,
        'reputation_tier': 'gold'
    }
    ctx = derive_trust_context(agent)
    assert ctx['label'] == 'Occasional Timeout Risk'
    assert ctx['color'] == '#f59e0b'

def test_derive_trust_context_generally_reliable():
    agent = {
        'total_tasks_completed': 15,
        'total_tasks_failed': 2,
        'total_tasks_timeout': 0,
        'success_rate': 0.85,
        'reputation_tier': 'silver'
    }
    ctx = derive_trust_context(agent)
    assert ctx['label'] == 'Generally Reliable'
    assert ctx['color'] == '#3b82f6'

def test_landing_page(client):
    response = client.get('/landing')
    assert response.status_code == 200
    assert b'J.A.R.V.I.S.' in response.data

def test_marketplace_page(client):
    response = client.get('/marketplace')
    assert response.status_code == 200
    assert b'Marketplace' in response.data or b'marketplace' in response.data

def test_login_page_get(client):
    response = client.get('/login')
    assert response.status_code == 200
    assert b'Sign In' in response.data