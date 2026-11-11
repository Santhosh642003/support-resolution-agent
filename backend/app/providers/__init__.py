"""Model provider layer. The ONLY package that may import a vendor SDK or name a model.

Everything else in the project talks to `LLMClient` and the types in `base`. Provider and model
are chosen by environment (`LLM_PROVIDER`, `LLM_MODEL`) in `factory.build_adapter`.
"""
