import requests

def test_cryptogent():
    response = requests.get('http://localhost:8000/cryptogent/')
    assert response.status_code == 200
    assert 'Hello, Cryptogent!' in response.text