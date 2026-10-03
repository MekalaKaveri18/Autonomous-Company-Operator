"""The LLM layer's two risky pure functions: JSON salvage and schema pruning.

Both exist specifically to absorb free-tier/local-model sloppiness, so they are
worth pinning down with tests rather than trusting in production.
"""

from __future__ import annotations

import json

import pytest

from centralign.llm.base import LLMError, Message, extract_json, fingerprint
from centralign.llm.providers import sanitize_gemini_schema
from centralign.llm.replay import ScriptedProvider


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_unlabelled_fence(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_on_both_sides(self):
        text = 'Sure! Here you go:\n{"a": 1, "b": [2, 3]}\nHope that helps.'
        assert extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_trailing_commas(self):
        assert extract_json('{"a": [1, 2,], "b": 3,}') == {"a": [1, 2], "b": 3}

    def test_top_level_array(self):
        assert extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        text = 'Result: {"note": "use {curly} braces", "ok": true} done'
        assert extract_json(text) == {"note": "use {curly} braces", "ok": True}

    def test_escaped_quote_inside_string(self):
        assert extract_json(r'{"q": "say \"hi\""}') == {"q": 'say "hi"'}

    def test_empty_response_raises(self):
        with pytest.raises(LLMError, match="empty"):
            extract_json("   ")

    def test_non_json_raises(self):
        with pytest.raises(LLMError, match="not JSON"):
            extract_json("I am afraid I cannot do that.")


class TestGeminiSchemaSanitiser:
    def test_strips_unsupported_keys(self):
        cleaned = sanitize_gemini_schema(
            {
                "type": "object",
                "additionalProperties": False,
                "title": "Plan",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {"n": {"type": "integer", "default": 3}},
                "required": ["n"],
            }
        )
        assert cleaned == {
            "type": "OBJECT",
            "properties": {"n": {"type": "INTEGER"}},
            "required": ["n"],
        }

    def test_nullable_union_becomes_nullable_flag(self):
        cleaned = sanitize_gemini_schema({"type": ["string", "null"]})
        assert cleaned == {"type": "STRING", "nullable": True}

    def test_nested_items_are_pruned(self):
        cleaned = sanitize_gemini_schema(
            {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "number"}}}}
        )
        assert cleaned["items"]["properties"]["x"]["type"] == "NUMBER"

    def test_propertyless_object_degrades_to_string(self):
        # Gemini rejects an OBJECT with no declared properties outright, so we
        # must not send one. A JSON-encoded string is the lossless fallback.
        cleaned = sanitize_gemini_schema({"type": "object"})
        assert cleaned["type"] == "STRING"


class TestFingerprint:
    def test_is_stable_across_calls(self):
        args = ("gemini", "m", "sys", [Message("user", "hi")], None, 0.1)
        assert fingerprint(*args) == fingerprint(*args)

    def test_changes_with_any_input(self):
        base = fingerprint("gemini", "m", "sys", [Message("user", "hi")], None, 0.1)
        assert base != fingerprint("gemini", "m", "sys", [Message("user", "ho")], None, 0.1)
        assert base != fingerprint("gemini", "m", "sys2", [Message("user", "hi")], None, 0.1)
        assert base != fingerprint("groq", "m", "sys", [Message("user", "hi")], None, 0.1)
        assert base != fingerprint("gemini", "m", "sys", [Message("user", "hi")], None, 0.9)


class TestCorrectiveRetry:
    async def test_bad_json_is_handed_back_to_the_model(self):
        provider = ScriptedProvider(["not json at all", '{"ok": true}'])
        result = await provider.complete_json(
            system="s", messages=[Message("user", "go")], schema={"type": "object"}
        )
        assert result == {"ok": True}
        # The second prompt must contain the failure so the model can self-correct.
        _, messages = provider.prompts[1]
        assert "not valid JSON" in messages[-1].content

    async def test_good_json_makes_exactly_one_call(self):
        provider = ScriptedProvider(['{"ok": true}'])
        await provider.complete_json(
            system="s", messages=[Message("user", "go")], schema={"type": "object"}
        )
        assert len(provider.prompts) == 1


def test_cache_roundtrip(tmp_path):
    from centralign.llm.base import CallCache, LLMResponse, Usage

    cache = CallCache(tmp_path)
    cache.put("k", LLMResponse(text='{"a":1}', model="m", usage=Usage(10, 20)))
    hit = cache.get("k")
    assert hit is not None
    assert hit.cached is True
    assert hit.json() == {"a": 1}
    assert hit.usage.total == 30
    assert cache.get("missing") is None


def test_cache_survives_a_corrupt_file(tmp_path):
    from centralign.llm.base import CallCache

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert CallCache(tmp_path).get("bad") is None


def test_disabled_cache_is_a_noop(tmp_path):
    from centralign.llm.base import CallCache, LLMResponse

    cache = CallCache(tmp_path, enabled=False)
    cache.put("k", LLMResponse(text="{}", model="m"))
    assert cache.get("k") is None
    assert list(tmp_path.glob("*.json")) == []


def test_replay_infers_identity_from_cassettes(tmp_path):
    from centralign.llm.replay import ReplayProvider

    (tmp_path / "abc.json").write_text(
        json.dumps({"text": "{}", "model": "gemini-2.5-flash", "meta": {"provider": "gemini"}}),
        encoding="utf-8",
    )
    provider = ReplayProvider(cassette_dir=tmp_path)
    assert provider.name == "gemini"
    assert provider.model == "gemini-2.5-flash"
    assert provider.cassette_count == 1


async def test_replay_miss_explains_how_to_recover(tmp_path):
    from centralign.llm.replay import CassetteMiss, ReplayProvider

    provider = ReplayProvider(cassette_dir=tmp_path)
    with pytest.raises(CassetteMiss, match="Re-record"):
        await provider.complete(system="s", messages=[Message("user", "x")])
