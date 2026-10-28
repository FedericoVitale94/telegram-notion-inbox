"""Single interface to the LLM. Provider chosen with LLM_PROVIDER (anthropic | groq).
Every Anthropic call checks the monthly spending limit first and records its cost afterwards (costs.py)."""
import base64
import json
import os

import costs
import net

_client = None

MODELS = {  # task -> (environment variable, default)
    "classify": ("ANTHROPIC_MODEL_CLASSIFY", "claude-haiku-5-5"),
    "draft": ("ANTHROPIC_MODEL_DRAFT", "claude-opus-5-5"),
    "agent": ("ANTHROPIC_MODEL_AGENT", "claude-sonnet-5-5"),
}


def model(task):
    env, default = MODELS[task]
    return os.environ.get(env) or default


def client():
    import anthropic

    global _client
    _client = _client or anthropic.Anthropic(max_retries=5)  # automatic retries on 429/5xx
    return _client


def complete_json(prompt, schema, task="classify", effort="low"):
    """Returns a dict matching `schema` (JSON Schema)."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    return {"anthropic": _anthropic, "groq": _groq}[provider](prompt, schema, task, effort)


def _anthropic(prompt, schema, task, effort):
    costs.check()
    m = model(task)
    extra = {}
    if "haiku" not in m:  # Haiku does not support server-side fallback
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    r = client().beta.messages.create(
        model=m,
        max_tokens=16000,  # includes thinking (always on for Opus 5.5)
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        **extra,  # fallbacks: if the model refuses, Anthropic retries on a fallback model
    )
    costs.record(getattr(r, "model", None) or m, getattr(r, "usage", None))
    if r.stop_reason == "refusal":
        raise RuntimeError(f"request refused by the model ({getattr(r.stop_details, 'category', None)})")
    return json.loads(next(b.text for b in r.content if b.type == "text"))


def _groq(prompt, schema, task, effort):
    # Groq: no guaranteed schema, so it goes in the prompt
    r = net.request(
        "POST", "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        json={"model": os.environ.get("GROQ_LLM_MODEL", "llama-3.3-70b-versatile"),
              "messages": [{"role": "user", "content": f"{prompt}\n\nJSON schema of the answer:\n{json.dumps(schema)}"}],
              "response_format": {"type": "json_object"}},
    ).json()
    return json.loads(r["choices"][0]["message"]["content"])


def describe_images(images, prompt):
    """Text describing a series of JPEG images (classify model). Anthropic only: returns "" with Groq."""
    if os.environ.get("LLM_PROVIDER", "anthropic") != "anthropic":
        return ""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.standard_b64encode(img).decode()}} for img in images]
    return _free_text("classify", content + [{"type": "text", "text": prompt}])


def search_web(prompt, max_searches=3):
    """Text answer using Anthropic's server-side web search. Anthropic only: returns "" with Groq."""
    if os.environ.get("LLM_PROVIDER", "anthropic") != "anthropic":
        return ""
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
    return _free_text("classify", [{"type": "text", "text": prompt}], tools=[tool])


def _free_text(task, content, tools=None):
    costs.check()
    m = model(task)
    messages = [{"role": "user", "content": content}]
    for _ in range(3):  # pause_turn: the server-side search hit its step limit, resume it
        r = client().messages.create(model=m, max_tokens=4000, messages=messages, **({"tools": tools} if tools else {}))
        costs.record(getattr(r, "model", None) or m, getattr(r, "usage", None))
        if r.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": r.content}]
    if r.stop_reason == "refusal":
        raise RuntimeError("request refused by the model")
    return "".join(b.text for b in r.content if b.type == "text").strip()  # citations split text into blocks
