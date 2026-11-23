"""Identity of the installed calculation software, separate from its scientific inputs."""
import hashlib
import json
import os
import platform
import sys
from functools import cache, lru_cache, partial
from importlib import metadata
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import get_args
from uuid import uuid4

import numpy
import scipy
from pydantic import BaseModel

from .version import APP_NAME, APP_VERSION


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _file_digest(path):
    if path.suffix.lower() in (".py", ".json"):
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _application_source(root):
    """The process-start BeamWeft source tree. No .git, installation paths, settings or user files."""
    try:
        files = sorted(p for p in root.rglob("*") if p.suffix in (".py", ".json") and "__pycache__" not in p.parts)
        if not files or not (root / "study_api.py").is_file():
            return None
        return _digest([(p.relative_to(root).as_posix(), _file_digest(p)) for p in files])
    except OSError:
        return None


# A long-running backend must not report newer source files as its original build on every read.
_APPLICATION = {"name": APP_NAME, "version": APP_VERSION, "source_sha256": _application_source(Path(__file__).parent)}
_RUNTIME = {"python": f"{platform.python_implementation()} {platform.python_version()}",
            "numpy": numpy.__version__, "scipy": scipy.__version__, "platform": sys.platform, "machine": platform.machine()}


@cache
def _provider_files(provider):
    """Hash observed files in the installed provider package, including imported helpers and packaged tables."""
    name, _, version = provider.rpartition(" ")
    try:
        dist = metadata.distribution(name) if name else None
        if not dist or dist.version != version:
            return None
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
        if origin.get("dir_info", {}).get("editable"):
            # The installed manifest for an editable package may list only the import shim.
            return None
        root = Path(dist.locate_file("")).resolve()
        signatures = []
        for file in sorted(dist.files or (), key=str):
            if (not file.parts or ".." in file.parts or "__pycache__" in file.parts or
                    file.parts[0].endswith((".dist-info", ".egg-info")) or file.suffix in (".pth", ".pyc")):
                continue
            path = Path(dist.locate_file(file)).resolve()
            if not path.is_relative_to(root):
                return None
            signatures.append((file.as_posix(), _file_digest(path)))
        return _digest(signatures) if signatures else None
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return None


@lru_cache(maxsize=1024)
def _code_digest(code):
    # Working directory, line numbers, object addresses and marshal's intern flags are not solver inputs.
    def constant(value):
        if isinstance(value, CodeType):
            return ["code", _code_digest(value)]
        if isinstance(value, slice):
            return ["slice", *map(constant, (value.start, value.stop, value.step))]
        if isinstance(value, (tuple, frozenset)):
            items = list(map(constant, value))
            return [type(value).__name__, sorted(items, key=_digest) if isinstance(value, frozenset) else items]
        return [type(value).__name__, repr(value)]
    return _digest([code.co_code.hex(), code.co_exceptiontable.hex(), code.co_argcount, code.co_posonlyargcount,
                    code.co_kwonlyargcount, code.co_flags, code.co_names, code.co_varnames,
                    code.co_freevars, code.co_cellvars, list(map(constant, code.co_consts))])


@cache
def _schema_digest(entry):
    return _digest([entry.__module__, entry.__qualname__, entry.model_json_schema()])


def _capture(value):
    """Fingerprint immutable calibration values without logging them. Opaque mutable state stays unknown."""
    if value is None or type(value) in (bool, int, float, complex, str, bytes):
        return (type(value).__name__, repr(value)), True
    if isinstance(value, ModuleType) and value.__name__ in sys.stdlib_module_names:
        return ("stdlib", value.__name__), True
    if isinstance(value, (tuple, frozenset)):
        items = [_capture(item) for item in value]
        parts = [signature for signature, _ in items]
        if isinstance(value, frozenset):
            parts.sort(key=_digest)
        return (type(value).__name__, parts), all(known for _, known in items)
    return (type(value).__module__, type(value).__qualname__), False


def _entry_identity(entry):
    if isinstance(entry, type) and issubclass(entry, BaseModel):
        return _schema_digest(entry), True
    if isinstance(entry, partial):
        implementation, known = _entry_identity(entry.func)
        values, frozen = _capture((entry.args, tuple(sorted((entry.keywords or {}).items()))))
        return _digest([implementation, values]), known and frozen
    if isinstance(entry, FunctionType):
        code = _function_digest(entry.__module__, entry.__qualname__, entry.__code__)
        if entry.__defaults__ or entry.__kwdefaults__ or entry.__closure__:
            captures = tuple(cell.cell_contents for cell in entry.__closure__ or ())
            values, frozen = _capture((entry.__defaults__, tuple(sorted((entry.__kwdefaults__ or {}).items())), captures))
            return _digest([code, values]), frozen
        return code, True
    # Bound methods and callable objects may have runtime state; the code is useful evidence even then.
    implementation = getattr(entry, "__func__", None) or getattr(type(entry), "__call__", None)
    if isinstance(implementation, FunctionType):
        return _code_digest(implementation.__code__), False
    return None, False


@lru_cache(maxsize=1024)
def _function_digest(module, name, code):
    return _digest([module, name, _code_digest(code)])


def device_runtime_key(entry):
    """Stable pure device calls may be memoized; unobservable instance calibration must be recomputed."""
    if isinstance(entry, FunctionType) and not (entry.__closure__ or entry.__defaults__ or entry.__kwdefaults__):
        return entry.__code__
    digest, frozen = _entry_identity(entry)
    return digest if frozen else None


def _implementation(provider, entries):
    try:
        identities = [_entry_identity(entry) for entry in entries if entry is not None]
    except (TypeError, ValueError):
        identities = []
    package = _APPLICATION["source_sha256"] if provider == "LaserClaw" else _provider_files(provider)
    signatures = [digest for digest, _ in identities]
    digest = _digest([package, signatures]) if package or any(signatures) else None
    frozen = bool(identities and all(known for _, known in identities))
    return digest, bool(package and frozen), frozen


def _model_types(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    else:
        for child in get_args(annotation):
            yield from _model_types(child)


@cache
def _device_fields(model):
    from .workbench.device_laws import Prescription

    def contains(cls, seen):
        if issubclass(cls, Prescription):
            return True
        if cls in seen:
            return False
        return any(contains(child, seen | {cls}) for f in cls.model_fields.values() for child in _model_types(f.annotation))

    return tuple(field for field, info in model.model_fields.items()
                 if any(contains(child, set()) for child in _model_types(info.annotation)))


def _prescriptions(value):
    from .workbench.device_laws import Prescription

    if isinstance(value, Prescription):
        yield value.study, value.provider, value.version
    elif isinstance(value, BaseModel):
        for field in _device_fields(type(value)):
            yield from _prescriptions(getattr(value, field))
    elif isinstance(value, (list, tuple, dict)):
        for item in value.values() if isinstance(value, dict) else value:
            yield from _prescriptions(item)


def _device_identity(method, provider, version):
    from .workbench.tools import STUDIES

    entry = STUDIES.get(method)
    implementation, complete, frozen = None, False, True
    contract = None
    if entry and entry[1] == provider and entry[0].version == version and entry[0].element_law:
        study = entry[0]
        implementation, complete, frozen = _implementation(provider, (study.compute, study.inputs))
        contract = _digest(study.element_law.model_dump(mode="json"))
    return {"method": method, "provider": provider, "version": version, "implementation_sha256": implementation,
            "contract_sha256": contract, "complete": complete, **({} if frozen else {"opaque_state": True})}


def solver_receipt(method, provider, version, *, compute=None, related=(), inputs=None, devices=(), contract=None,
                   scope=None):
    """Record an installed implementation and its declared device providers; never stamp an old result on read."""
    keys = {(d["method"], d["provider"], d["version"]) for d in devices}
    if inputs is not None:
        keys.update(_prescriptions(inputs))
    dependencies = [_device_identity(*key) for key in sorted(keys)]
    implementation, complete, frozen = _implementation(provider, (compute, *related))
    receipt = {"schema": 1, "method": method, "provider": provider, "version": version,
               "application": dict(_APPLICATION), "runtime": dict(_RUNTIME), "implementation_sha256": implementation,
               "complete": bool(_APPLICATION["source_sha256"] and complete and all(d["complete"] for d in dependencies))}
    if dependencies:
        receipt["devices"] = dependencies
    if not frozen or any(item.get("opaque_state") for item in dependencies):
        receipt["opaque_state"] = True
    if contract is not None:
        receipt["contract_sha256"] = _digest(contract)
    if scope == "method_only":
        # A legacy archive can identify a method name without identifying the model's device implementations.
        receipt.update(scope=scope, complete=False)
    elif scope is not None:
        raise ValueError(f"Unsupported solver receipt scope: {scope}")
    receipt["sha256"] = _digest(receipt)
    return receipt


_WORKER = uuid4().hex


def solver_cache_key(receipt):
    """An incomplete environment comparison cannot authorize cross-process reuse."""
    if receipt.get("opaque_state"):
        # Two executions in this same worker may see different calibration state.
        return [receipt["sha256"], uuid4().hex]
    return receipt["sha256"] if receipt["complete"] else [receipt["sha256"], _WORKER, os.getpid()]
