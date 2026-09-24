"""One OpenRouter chat call that can be stopped.

A plain request can't be. OpenRouter keeps the connection alive with
comments while a model works, so a read timeout never fires (an update
preview once sat for minutes), and a request the app stops waiting for
is still answered, and billed. A streamed one can: dropping the
connection "immediately stops model processing and billing" on most
providers — OpenAI and Anthropic among them, though not Groq, Google or
Mistral (OpenRouter's streaming docs, Sept 2026).

So `chat` streams the call on a worker thread while the caller's thread
watches the clock and a stop flag. When either trips, Stopped is raised
at once, and the worker is woken to hang up (see _wake). What comes
back is shaped like the Response callers
already read — status_code, ok, json(), text — so nothing after the call
changes. An error status is the real Response, unread: OpenRouter sends
those as plain JSON before any stream starts."""

import json
import threading
import time


class Stopped(Exception):
    """The call was dropped: `reason` is "timeout" or "cancelled"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Reply:
    """A whole streamed reply, as the Response it stands in for."""
    status_code = 200
    ok = True

    def __init__(self, body: dict):
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _read(resp, halt) -> _Reply:
    """Add a stream's chunks up into one reply: the text, the model that
    answered, the usage (sent last), or the error that ended it."""
    body, text = {}, []
    for raw in resp.iter_lines():
        if halt.is_set():
            break
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if not line.startswith("data:"):
            continue                    # a blank line, or ": OPENROUTER PROCESSING"
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        for key in ("model", "usage", "error"):
            if chunk.get(key):
                body[key] = chunk[key]
        for choice in chunk.get("choices") if isinstance(chunk.get("choices"), list) else ():
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text.append(delta["content"])
    if "error" not in body:
        body["choices"] = [{"message": {"content": "".join(text)}}]
    return _Reply(body)


def chat(post, url: str, *, headers: dict, body: dict, timeout_s: float,
         stop: threading.Event = None):
    """POST `body` to `url`, streamed. The reply, or Stopped once
    `timeout_s` has passed or `stop` is set. `post` is requests.post, or
    a stand-in; a reply that isn't an event stream is returned as is."""
    # Nothing is sent once the operator has moved on, or with no time left.
    if stop is not None and stop.is_set():
        raise Stopped("cancelled")
    if timeout_s <= 0:
        raise Stopped("timeout")
    halt, done, box = threading.Event(), threading.Event(), {}

    def work():
        resp = None
        try:
            resp = box["resp"] = post(url, headers=headers, json={**body, "stream": True},
                                      timeout=(10, max(timeout_s, 1)), stream=True)
            if halt.is_set():
                return                  # dropped before it was read
            kind = (getattr(resp, "headers", None) or {}).get("Content-Type", "")
            box["reply"] = (_read(resp, halt) if resp.status_code < 400
                            and "text/event-stream" in kind else resp)
        except Exception as e:          # handed to the caller below
            box["error"] = e
        finally:
            if halt.is_set() and resp is not None:
                resp.close()            # dropped: hang up, from this thread
            done.set()

    threading.Thread(target=work, daemon=True).start()
    deadline = time.monotonic() + timeout_s
    while not done.wait(0.2):
        reason = ("cancelled" if stop is not None and stop.is_set()
                  else "timeout" if time.monotonic() >= deadline else None)
        if reason:
            halt.set()
            _wake(box.get("resp"))
            raise Stopped(reason)
    if "error" in box:
        raise box["error"]
    return box["reply"]


def _wake(resp):
    """Wake the worker's blocked read, so it sees `halt` and hangs up.

    Not resp.close() from here: that waits for the lock the blocked read
    holds — for OpenRouter's next byte, or the read timeout, a minute on
    a stalled stream. Shutting the socket's read side (urllib3 2.3+)
    ends that read at once. Failing that, the worker still hangs up at
    the next line it gets."""
    shutdown = getattr(getattr(resp, "raw", None), "shutdown", None)
    if shutdown is not None:
        try:
            shutdown()
        except (OSError, RuntimeError, ValueError):
            pass                        # already finished, or already closed
