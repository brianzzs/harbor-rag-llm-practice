"""Small HTTP client shared by the lab commands and acceptance runner."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(url, token=None, data=None, timeout=5):
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.headers, error.read()


def decode(payload):
    return json.loads(payload.decode())
