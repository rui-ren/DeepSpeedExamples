import requests

def api_call():
    api_url = "http://localhost:8000/generate"

    headers = {
        "User-Agent": "Benchmark Client",
        "Accept": "text/event-stream",
        "Content-Type": "application/json"
    }

    pload = {
        "prompt": "Explain superconductors like I'm five years old",
        "n": 1,
        "use_beam_search": False,
        "temperature": 1.0,
        "top_p": 0.9,
        "max_tokens": 500,
        "ignore_eos": False,
        "stream": False,
    }

    response = requests.post(api_url, headers=headers, json=pload)

    if response.status_code == 200:
        print(response.text)
    else:
        print("Got Error Here")

if __name__ == "__main__":
    api_call()

