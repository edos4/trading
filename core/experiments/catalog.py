"""Read the shipped per-folder source manifest; never execute Markdown."""
from __future__ import annotations

import json
import hashlib
from functools import lru_cache
from pathlib import Path

MANIFEST = Path(__file__).with_name("sources.json")


@lru_cache(maxsize=1)
def sources() -> tuple[dict, ...]:
    payload = json.loads(MANIFEST.read_text())
    entries = payload["sources"]
    ids = [entry["id"] for entry in entries]
    numbers = [entry["number"] for entry in entries]
    if len(ids) != len(set(ids)) or len(numbers) != len(set(numbers)):
        raise ValueError("Duplicate experiment source IDs/numbers")
    for entry in entries:
        for name,digest in (("writeup","writeup_sha256"),("plan","plan_sha256")):
            if hashlib.sha256(entry[name].encode()).hexdigest() != entry[digest]:
                raise ValueError("Experiment source manifest integrity check failed")
    return tuple(entries)


def catalog(additional: tuple[dict, ...] = ()) -> list[dict]:
    from core.experiments.adapters import definition

    reviewed = json.loads(MANIFEST.with_name("reviewed_sources.json").read_text())
    output = []
    for source in sources():
        implementation = definition(source["number"])
        if implementation["availability"] == "blocked" and source["number"] not in {331,350,374,412,418,429,432,439}:
            gap = source.get("gaps", "").split("\n\n")[0].strip()
            implementation["blocker"] = gap or implementation["blocker"]
        if implementation["availability"] == "ready":
            pinned = reviewed.get(str(source["number"]), {})
            if any(pinned.get(name) != source[name] for name in ("writeup_sha256","plan_sha256")):
                implementation.update(availability="blocked", blocker="Writeup or integration plan changed; re-review the adapter and its source pins before running")
        output.append({
            **{key: value for key, value in source.items()
               if key not in {"writeup", "plan"}},
            **implementation,
        })
    from core.experiments import rules
    ids = {entry["id"] for entry in output}
    for source in additional:
        if source["id"] in ids:
            raise ValueError("Duplicate generated source identity")
        ids.add(source["id"])
        try:
            for name in ("writeup", "plan"):
                if hashlib.sha256(source[name].encode()).hexdigest() != source[name + "_sha256"]:
                    raise ValueError("Generated source document integrity check failed")
            if source.get("rule_spec"):
                if source.get("adapter_version") != rules.VERSION:
                    raise ValueError("Generated rule compiler revision changed; regenerate before running")
                implementation = rules.definition(source["rule_spec"])
                if implementation["rule_spec_sha256"] != source.get("rule_spec_sha256"):
                    raise ValueError("Generated rule specification changed; regenerate before running")
            else:
                implementation = {key:source[key] for key in ("availability", "kind", "scope", "blocker",
                                  "parameters", "assumptions", "limitations", "adapter_version")}
        except ValueError:
            implementation = {"availability":"blocked", "kind":"blocked",
                              "blocker":"Generated documents or rules failed integrity/contract checks; regenerate before running"}
        output.append({**{k:v for k,v in source.items() if k not in {"writeup", "plan"}}, **implementation})
    return output


def source_by_id(source_id: str, additional: tuple[dict, ...] = ()) -> dict:
    found = next((source for source in (*sources(), *additional) if source["id"] == source_id), None)
    if found is None:
        raise KeyError("Unknown experiment folder")
    return found
