import requests
from pprint import pprint


BASE_URL = "http://127.0.0.1:8000"
response = requests.get(f"{BASE_URL}/api/state/")
response = response.json()
pprint(response)



