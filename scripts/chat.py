"""Talk to a running Lia server. Usage: scripts/chat.py <chat|confirm> <session> '<json body>'"""
import sys, json, httpx
url, session = sys.argv[1], sys.argv[2]
body = {"session": session, **json.loads(sys.argv[3])}
with httpx.stream("POST", f"http://127.0.0.1:8000/api/{url}", json=body, timeout=400) as r:
    for l in r.iter_lines():
        if not l.strip(): continue
        e = json.loads(l)
        print(e["text"] if e.get("type") == "message" else json.dumps(e, ensure_ascii=False)[:700])
