"""Offline credential checks for the provider selected by each Pydantic AI task."""

import os
from collections import defaultdict

from ...core.config import TaskConfig
from ...domain.models import DoctorCheck

# Read credential presence from the environment. Constructing a provider
# client can contact a cloud metadata service during credential discovery.
_API_KEYS = {
    "alibaba": ("ALIBABA_API_KEY", "DASHSCOPE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "cohere": ("CO_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "gateway": ("PYDANTIC_AI_GATEWAY_API_KEY", "PAIG_API_KEY"),
    "github": ("GITHUB_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "heroku": ("HEROKU_INFERENCE_KEY",),
    "huggingface": ("HF_TOKEN",),
    "mistral": ("MISTRAL_API_KEY",),
    "moonshotai": ("MOONSHOTAI_API_KEY",),
    "nebius": ("NEBIUS_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "ovhcloud": ("OVHCLOUD_API_KEY",),
    "sambanova": ("SAMBANOVA_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "vercel": ("VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"),
    "xai": ("XAI_API_KEY",),
    "zai": ("ZAI_API_KEY",),
}
_PROVIDER_ALIASES = {
    "azure-responses": "azure",
    "openai-chat": "openai",
    "openai-responses": "openai",
}


def _check_provider(provider: str) -> DoctorCheck:
    name = f"Pydantic auth ({provider})"
    if provider == "test":
        return DoctorCheck(name=name, ok=True, detail="The test model runs without credentials")
    if provider == "ollama":
        detail = ("OLLAMA_API_KEY set" if os.getenv("OLLAMA_API_KEY")
                  else "Local Ollama accepts a keyless endpoint")
        return DoctorCheck(name=name, ok=True, detail=detail)

    keys = _API_KEYS.get(provider)
    if keys is None:
        return DoctorCheck(
            name=name, ok=True, warning=True,
            detail="Verify provider credentials separately. No local check is available",
        )
    # The gateway only consults its legacy key if the primary variable is absent.
    if provider == "gateway" and keys[0] in os.environ:
        keys = keys[:1]
    for key in keys:
        if os.environ.get(key, "").strip():
            return DoctorCheck(name=name, ok=True, detail=f"{key} set")
    if provider == "openai" and os.getenv("OPENAI_BASE_URL"):
        return DoctorCheck(
            name=name, ok=True, warning=True,
            detail="OPENAI_BASE_URL set. Check whether this endpoint needs OPENAI_API_KEY",
        )
    return DoctorCheck(name=name, ok=False, detail=f"Set {' or '.join(keys)}")


def check_pydantic_auth(tasks: dict[str, TaskConfig]) -> list[DoctorCheck]:
    by_provider: dict[str, list[str]] = defaultdict(list)
    checks = []
    for name, task in sorted(tasks.items()):
        provider, _, model = task.model.partition(":")
        if task.model != "test" and not (provider and model):
            checks.append(DoctorCheck(
                name=f"Pydantic model ({name})", ok=False,
                detail="Specify a provider-qualified model, such as openai:gpt-5",
            ))
            continue
        if provider.startswith("gateway/"):
            provider = "gateway"
        by_provider[_PROVIDER_ALIASES.get(provider, provider)].append(name)

    for provider, names in sorted(by_provider.items()):
        check = _check_provider(provider)
        check.detail += f" (used by {', '.join(names)})"
        checks.append(check)
    return checks
