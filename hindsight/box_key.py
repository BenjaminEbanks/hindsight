"""Give worker boxes model access without giving them the model key.

Boxes never hold the key. It is stored as a Sail secret, and a saved egress policy adds
it to chat-completion requests sent to the provider, and to nothing else.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from .config import ModelsConfig

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def policy_document(models: ModelsConfig) -> dict:
    """The egress rule for the configured provider."""
    url = urlparse(models.base_url)
    if url.scheme != "https" or not url.hostname:
        raise ValueError(f"base_url must be an https URL, got {models.base_url!r}: "
                         "egress policies add credentials to HTTPS requests")
    if url.hostname in _LOCAL_HOSTS:
        raise ValueError(f"base_url {models.base_url!r} is on this machine, "
                         "and worker boxes can't reach it")
    return {"rules": {url.hostname: [{
        "match": {"method": "POST",
                  "path": {"equals": f"{url.path.rstrip('/')}/chat/completions"}},
        "request": {"set": {"headers": {
            "authorization": f"Bearer ${{secrets.{models.box_secret}}}"}}},
    }]}}


def set_up(models: ModelsConfig) -> list[str]:
    """Store the key as a Sail secret and save the policy. Returns what it did."""
    import sail

    key = os.environ.get(models.api_key_env)
    if not key:
        raise ValueError(f"{models.api_key_env} is not set")
    document = policy_document(models)
    sail.Secret.set(models.box_secret, key)
    done = [f"stored {models.api_key_env} as the Sail secret {models.box_secret}"]
    if any(policy.name == models.box_policy for policy in sail.EgressPolicy.list()):
        done.append(f"egress policy {models.box_policy} already exists. Policies can't be "
                    "edited, so if you changed provider, set models.box_policy to a new "
                    "name and run this again")
    else:
        sail.EgressPolicy.create(models.box_policy, document)
        done.append(f"created egress policy {models.box_policy} for "
                    f"{urlparse(models.base_url).hostname}")
    return done
