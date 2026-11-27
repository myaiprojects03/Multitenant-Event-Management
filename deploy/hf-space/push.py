"""Sync the staged backend folder to the Hugging Face Space and set its non-secret variables.

Secrets (GEMINI_API_KEY, DATABASE_OWNER_URL, APP_SECRET) are entered once in the Space's
settings by hand; this script never sees them.
"""

import os
import sys

from huggingface_hub import HfApi

VARIABLES = {
    "APP_ENV": "production",
    "RUN_WORKER_IN_PROCESS": "true",  # one container: the arq worker runs in the API process
    "DEMO_AUTO_SEED": "true",  # reseed on boot and nightly when the data is dated before today
    "SWEEP_INTERVAL_MIN": "30",  # lets the Neon database scale to zero between visits
}


def step(name, fn, *args, **kw):
    """Run one Hub call; on failure say which step and why (never the token) and stop."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        detail = getattr(getattr(e, "response", None), "text", "") or str(e)
        print(f"::error::{name} failed: {type(e).__name__}: {detail[:500]}")
        sys.exit(1)


folder, sha = sys.argv[1], os.environ.get("GITHUB_SHA", "")[:7]
space = os.environ.get("HF_SPACE", "").strip()
if not os.environ.get("HF_TOKEN"):
    sys.exit("::error::Repo secret HF_TOKEN is empty")
if space.count("/") != 1:
    sys.exit(
        f"::error::Repo variable HF_SPACE must look like <hf-user>/eventai-api, got {space!r}"
    )
api = HfApi(token=os.environ["HF_TOKEN"])
user = step("Checking the token (whoami)", api.whoami)
print(f"Token belongs to {user.get('name')}; target Space {space}")
step(
    "Creating the Space (needs a token with write access)",
    api.create_repo,
    space,
    repo_type="space",
    space_sdk="docker",
    exist_ok=True,
)

current = {
    k: v.value
    for k, v in step("Reading Space variables", api.get_space_variables, space).items()
}
for key, value in VARIABLES.items():
    if (
        current.get(key) != value
    ):  # each change restarts the Space, so only set what differs
        step(f"Setting variable {key}", api.add_space_variable, space, key, value)

step(
    "Uploading the backend",
    api.upload_folder,
    repo_id=space,
    repo_type="space",
    folder_path=folder,
    delete_patterns=[
        "*"
    ],  # mirror: files removed from the repo disappear from the Space too
    commit_message=f"Deploy {sha}",
)
print(f"Deployed {sha} to https://huggingface.co/spaces/{space}")
