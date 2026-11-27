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

folder, space, sha = sys.argv[1], os.environ["HF_SPACE"], os.environ.get("GITHUB_SHA", "")[:7]
api = HfApi(token=os.environ["HF_TOKEN"])
api.create_repo(space, repo_type="space", space_sdk="docker", exist_ok=True)

current = {k: v.value for k, v in api.get_space_variables(space).items()}
for key, value in VARIABLES.items():
    if current.get(key) != value:  # each change restarts the Space, so only set what differs
        api.add_space_variable(space, key, value)

api.upload_folder(
    repo_id=space,
    repo_type="space",
    folder_path=folder,
    delete_patterns=["*"],  # mirror: files removed from the repo disappear from the Space too
    commit_message=f"Deploy {sha}",
)
print(f"Deployed {sha} to https://huggingface.co/spaces/{space}")
