"""Minimal CDP client for the dedicated debuggable Edge (port 9333).

This browser was launched by us with its own profile, so driving it touches
neither the user's Edge nor the shared input queue. Runtime.evaluate is enough
for reading a page and clicking by selector; navigation goes through Page.navigate.
"""
import json, sys, time, urllib.request
import websocket

PORT = 9333


def page_target(prefer=("<你们的 SSO 域名>", "<EBS 主机名>", "microsoftonline")):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5))
    pages = [t for t in tabs if t.get("type") == "page"]
    for p in pages:
        if any(k in (p.get("url") or "") for k in prefer):
            return p
    return pages[0] if pages else None


class CDP:
    def __init__(self, target=None):
        t = target or page_target()
        if t is None:
            raise RuntimeError("no page target on port %d" % PORT)
        self.t = t
        # Edge rejects a browser-style Origin header on the debug socket; sending none avoids relaunching it with --remote-allow-origins
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def navigate(self, url, settle=6.0):
        self.call("Page.navigate", url=url)
        time.sleep(settle)
        return self.eval("location.href")

    def click(self, selector, settle=4.0):
        ok = self.eval(f"(function(){{var e=document.querySelector({json.dumps(selector)});if(!e)return false;e.click();return true;}})()")
        time.sleep(settle)
        return ok

    def close(self):
        self.ws.close()


if __name__ == "__main__":
    c = CDP()
    print("url  :", c.eval("location.href"))
    print("title:", c.eval("document.title"))
    print("links/buttons:")
    print(c.eval("""
      Array.from(document.querySelectorAll('a,button,input[type=submit],input[type=button]'))
        .map(e => (e.tagName+' | '+(e.innerText||e.value||'').trim().slice(0,50)+' | '+(e.href||e.formAction||e.name||'')).slice(0,180))
        .filter(s => s.trim().length > 6).slice(0,40).join('\n')
    """))
    c.close()
