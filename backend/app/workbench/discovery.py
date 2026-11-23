"""Search installed capabilities by their declarations, without loading solver schemas into each request."""
import math
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from functools import cache
from typing import Literal

from pydantic import Field

from ..study_api import ResultReference, Shape, StrictInput
from ..version import provider_display_name
from .measurement_sources import offers_table_columns, original_prefix
from .project_outputs import ProjectReference
from .studies import frozen_quantity


class CapabilitySearch(StrictInput):
    query: str = Field(default="", max_length=500, description="Purpose, physical quantity, provider, or exact tool name. Empty query browses the directory.")
    kind: Literal["study", "action"] | None = Field(default=None, description="Study for a deterministic calculation, action for an engineering or evidence operation.")
    accepts_quantity: str | None = Field(default=None, max_length=300, description="Exact declared input quantity ID, when known.")
    source: ResultReference | ProjectReference | None = Field(default=None, description="A specific saved output in this case: {run_id,port} or {revision_id,port}. Find input fields that can receive its value without loss. Operating conditions must still be checked.")
    original_files: bool = Field(default=False, description="Require a study that directly accepts an archived original file.")
    attachment_id: int | None = Field(default=None, gt=0, description="Find whole-file readers or studies with inputs that can be mapped to this table's columns. Check column meaning and units separately. Use original_files for whole-file readers only.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=20)


class SourceSignature(StrictInput):
    quantity: str
    unit: str
    scale: float = Field(gt=0)
    connects_as: list[Shape]

    def fits(self, port):
        return (self.quantity == port.quantity and port.shape in self.connects_as
                and (self.unit != port.unit or self.scale == port.scale)
                and (port.shape != "state" or self.unit == port.unit))


class BrowserSearch(CapabilitySearch):
    sources: list[SourceSignature] = Field(default_factory=list)


def _normalize(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _terms(value, *, indexing=False):
    tokens = set()
    for i, part in enumerate(re.split(r"([\u3400-\u9fff]+)", value)):
        if i % 2:
            tokens.update(part[j:j+2] for j in range(len(part)-1))
            if indexing or len(part) == 1:
                tokens.update(part)
        else:
            tokens.update(re.findall(r"[^\W_]+", part))
    return tokens


class CapabilityDirectory:
    def __init__(self, registry, studies, labels):
        self.entries = {}
        self.terms = defaultdict(dict)
        self.studies = studies
        for name, (model, description) in registry.items():
            if name in {"replace_crystal", "finish"}:
                continue
            study, provider = studies.get(name, (None, "BeamWeft"))
            title = study.title if study else labels.get(name, name)
            ports = [*study.input_ports.values(), *study.output_ports.values()] if study else []
            fields = " ".join(f"{key} {f.title or ''} {f.description or ''}" for key, f in model.model_fields.items())
            quantities = " ".join(f"{p.label} {p.quantity} {p.unit}" for p in ports)
            if study:
                quantities += " " + " ".join(study.metrics.values())
            keywords = " ".join(study.keywords) if study else ""
            display_name = provider_display_name(provider)
            self.entries[name] = {"name": name, "kind": "study" if study else "action", "title": title,
                                  "provider": provider, "provider_display_name": display_name, "scope": description,
                                  **({"version": study.version} if study else {})}
            for weight, field in [(1, description), (2, fields), (3, quantities), (4, f"{provider} {display_name}"),
                                  (6, keywords), (8, name), (10, title)]:
                for token in _terms(_normalize(field), indexing=True):
                    self.terms[token][name] = weight
        counts = Counter(_normalize(item["title"]) for item in self.entries.values())
        for item in self.entries.values():
            if counts[_normalize(item["title"])] > 1:
                item["ambiguous_title"] = True

    def search(self, request, sources=(), *, full_scope=False, prefix=None, table_candidate=False):
        query = _normalize(request.query)
        scores = defaultdict(float)
        for term in _terms(query):
            matches = self.terms.get(term, {})
            idf = 1 + math.log((1+len(self.entries))/(1+len(matches)))
            for name, weight in matches.items():
                scores[name] += weight * idf
        @cache
        def recognition(reader):
            data, complete = prefix
            try:
                verdict = reader.recognizes(data, complete=complete)
            except Exception:
                logging.getLogger(__name__).exception("Original recognition failed for %s", reader.__qualname__)
                return None
            return verdict if type(verdict) is bool else None

        ranked = []
        file_matches = {}
        for name, item in self.entries.items():
            if request.kind and item["kind"] != request.kind:
                continue
            if request.original_files or prefix is not None or request.accepts_quantity or sources:
                study = self.studies.get(name, (None,))[0]
                if not study or (request.original_files and not study.uses_original_files):
                    continue
                if (request.accepts_quantity or sources) and not any(
                    (not request.accepts_quantity or p.quantity == request.accepts_quantity)
                    and (not sources or any(s.fits(p) for s in sources)) for p in study.input_ports.values()
                ):
                    continue
            title = _normalize(item["title"])
            exact = bool(query) and query in {name.casefold(), title}
            phrase = bool(query) and (query in title or query in name.casefold())
            score = scores.get(name, 0)
            if query and not (score or phrase):
                continue
            recognized = False
            if prefix is not None:
                columns = {}
                if table_candidate and not request.original_files:
                    columns = {key: {"label": port.label, "type": "numeric", "unit": port.unit}
                               for key, port in study.input_ports.items() if port.shape == "series"}
                    columns.update({key: {"label": field["label"], "type": "text"}
                                    for key, field in study.text_column_inputs.items()})
                roles = {}
                for key, readers in study.original_inputs.items():
                    verdicts = {recognition(reader) for reader in readers}
                    if True in verdicts:
                        roles[key] = "recognized"
                    elif None in verdicts:
                        roles[key] = "unverified"
                whole_file = study.uses_original_files and (roles or not study.original_inputs)
                if not whole_file and not columns:
                    continue
                recognized = "recognized" in roles.values()
                match = "recognized" if recognized else "unverified" if whole_file else "table_columns"
                file_matches[name] = {"original_match":match, **({"original_inputs": roles} if whole_file else {}),
                                      **({"table_column_inputs": columns} if columns else {})}
            ranked.append((not exact, not phrase, -score, not recognized, title, name))
        ranked.sort()
        end = request.offset + request.limit
        items = []
        for *_, name in ranked[request.offset:end]:
            item = self.entries[name]
            scope = item["scope"]
            fields = {key: port.label for key, port in self.studies[name][0].input_ports.items()
                      if (not request.accepts_quantity or port.quantity == request.accepts_quantity)
                      and any(source.fits(port) for source in sources)} if sources else {}
            items.append({**item, **file_matches.get(name, {}), "scope": scope if full_scope or len(scope) <= 320 else scope[:319]+"…",
                          **({"matching_inputs": fields} if fields else {})})
        return {"items": items, "total": len(ranked), "next_offset": end if end < len(ranked) else None}

    def search_case(self, db, case_id, request, *, full_scope=False):
        sources = getattr(request, "sources", ())
        if request.source is not None:
            if sources:
                raise ValueError("引用已存物理量时，请只提供其记录与输出位置。")
            offer, _, _ = frozen_quantity(db, case_id, request.source)
            sources = [SourceSignature(quantity=offer.quantity, unit=offer.unit, scale=offer.scale, connects_as=offer.connects_as)]
        prefix, table_candidate = None, False
        if request.attachment_id is not None:
            data, complete, filename = original_prefix(db, case_id, request.attachment_id)
            prefix = (data, complete)
            table_candidate = not request.original_files and offers_table_columns(data, filename)
        page = self.search(request, sources, full_scope=full_scope, prefix=prefix, table_candidate=table_candidate)
        if any("table_column_inputs" in item for item in page["items"]):
            page["table_inspection_action"] = "inspect_measurement_table"
            page["connection_action"] = "run_connected_study"
        elif request.source is not None and page["items"]:
            page["connection_action"] = "run_connected_study"
        return page
