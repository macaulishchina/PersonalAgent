"""Thin upstream provider adapters: framework model callback and local FastEmbed."""

import json

from mem0.llms.base import LLMBase
from mem0.embeddings.base import EmbeddingBase
from mem0.embeddings.fastembed import FastEmbedEmbedding
from fastembed import TextEmbedding

from .policy import FACT_SCHEMA, ACTION_SCHEMA, validate_facts

# Configured once in the isolated memory worker. No tokens or network endpoint here.
CHANNEL = None


class FrameworkLLM(LLMBase):
    def generate_response(self, messages, tools=None, tool_choice="auto", **kwargs):
        if tools:
            raise ValueError("Memory model must not execute tools")
        if CHANNEL is None:
            raise RuntimeError("Memory model callback unavailable")
        extracting = any(m.get("role") == "system" for m in messages)
        try:
            response = CHANNEL.complete(messages, FACT_SCHEMA if extracting else ACTION_SCHEMA)
            if extracting:
                CHANNEL.provenance = validate_facts(response, CHANNEL.sources)
            else:
                CHANNEL.actions = response.get("memory", [])
            return json.dumps(response, ensure_ascii=False)
        except Exception as exc:
            # Mem0 1.x may suppress model errors in its update phase; surface them after add().
            CHANNEL.error = exc
            raise


class LocalFastEmbed(FastEmbedEmbedding):
    def __init__(self, config=None):
        EmbeddingBase.__init__(self, config)
        self.dense_model = TextEmbedding(model_name=self.config.model, **self.config.model_kwargs)
        self.config.embedding_dims = self.dense_model.embedding_size
