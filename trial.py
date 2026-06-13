import requests



BASE_URL = "http://127.0.0.1:8000"
response = requests.get(f"{BASE_URL}/api/state/")
response = response.json()
print(response["weather"])

print(response["tracks"])

print(response["graph"])



