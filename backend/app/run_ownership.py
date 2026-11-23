"""Local proof that the executor of this exact task still holds its lease.

A worker holds an exclusive SQLite lock on a private, randomly named witness
file until its background job returns. The OS releases the lock on process
death. A free existing witness proves release; an absent witness proves nothing
(another host, another OS account, or a cleaned temporary directory).
"""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from tempfile import gettempdir
from time import sleep
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import AgentStep, AgentTask, AgentToolCall


def _scope(bind):
    url = bind.url
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        file = Path(url.database).resolve()
        try:
            stat = file.stat()
        except OSError:
            return None
        key = f"sqlite:{stat.st_dev}:{stat.st_ino}" if stat.st_ino else f"sqlite:{os.path.normcase(str(file))}"
    elif url.get_backend_name() == "sqlite":
        key = f"memory:{os.getpid()}:{id(bind)}"
    else:
        key = repr((url.get_backend_name(), url.host, url.port, url.database))
    return hashlib.sha256(key.encode()).hexdigest()


def _folder():
    # Per-account suffix also handles POSIX systems with a shared /tmp.
    uid = str(os.getuid()) if hasattr(os, "getuid") else str(Path.home())
    return Path(gettempdir()) / ("beamweft-run-owners-" + hashlib.sha256(uid.encode()).hexdigest()[:16])


def _remove_witness(path):
    for delay in (0, .02, .08):
        try:
            if delay:
                sleep(delay)
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            continue  # A Windows reader may briefly retain a handle.
        except OSError:
            return


class RunLease:
    def __init__(self, bind):
        self.bind = bind
        directory = _folder()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = uuid4().hex
        self.stamp = {"version":1, "token":token, "storage":_scope(bind)}
        self.file = directory / token
        self.file.touch(mode=0o600, exist_ok=False)
        self.connection = sqlite3.connect(self.file, check_same_thread=False)
        try:
            self.connection.execute("PRAGMA page_size=512")
            self.connection.execute("CREATE TABLE witness (token TEXT)")
            with self.connection:
                self.connection.execute("INSERT INTO witness VALUES (?)", (token,))
            self.connection.execute("BEGIN EXCLUSIVE")
        except BaseException:
            self.close(remove=True)
            raise

    def close(self, *, remove=False):
        self.connection.close()
        if remove:
            _remove_witness(self.file)

    async def execute(self, action, task_id):
        terminal = False
        try:
            await action(task_id)
        finally:
            try:
                with Session(self.bind) as db:
                    state = db.query(AgentTask.status).filter_by(id=task_id).scalar()
                    terminal = state is not None and state not in {"pending","running"}
            except SQLAlchemyError:
                pass  # Keep the released witness for a later observer.
            finally:
                await asyncio.to_thread(self.close, remove=terminal)


def owner_state(bind, stamp):
    """Return live, released or unverified without contacting a host named by a record."""
    if not isinstance(stamp, dict) or stamp.get("version") != 1 or not stamp.get("storage") or stamp["storage"] != _scope(bind):
        return "unverified"
    token = stamp.get("token")
    if not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token):
        return "unverified"
    file = _folder() / token
    try:
        connection = sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            row = connection.execute("SELECT token FROM witness").fetchone()
            return "released" if row == (token,) else "unverified"
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return "live" if getattr(exc, "sqlite_errorcode", 0) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "unverified"


def reconcile(db, case_id, mode, task_id=None):
    active = db.query(AgentTask).filter(AgentTask.case_id == case_id, AgentTask.mode == mode,
                                       AgentTask.status.in_(["pending", "running"]))
    if task_id is not None:
        active = active.filter(AgentTask.id == task_id)
    owners = {}
    rows = active.outerjoin(AgentToolCall, (AgentToolCall.task_id == AgentTask.id) &
                           (AgentToolCall.tool_name == mode + "_request")).with_entities(
                               AgentTask.id, AgentToolCall.input_json["execution_owner"]).all()
    released = []
    for rid, stamp in rows:
        owners[rid] = owner_state(db.get_bind(), stamp)
        if owners[rid] != "released":
            continue
        # A simultaneous read, cancellation or finalization may have resolved it.
        if not active.filter(AgentTask.id == rid).update({AgentTask.status:"failed"}, synchronize_session=False):
            continue
        if mode == "workbench":
            receipt = db.query(AgentToolCall).filter_by(task_id=rid, tool_name="workbench_request").first()
            if receipt is not None:
                receipt.output_json = {**(receipt.output_json or {}), "failure_code":"owner_released"}
        released.append(stamp["token"])
        db.query(AgentStep).filter_by(task_id=rid, status="running").update(
            {AgentStep.status:"failed", AgentStep.result_summary:"执行进程已经退出，本轮已保存的记录可继续查阅。"},
            synchronize_session=False)
        db.query(AgentToolCall).filter_by(task_id=rid, status="running").update(
            {AgentToolCall.status:"failed", AgentToolCall.error_message:"工具执行期间所属进程退出。"}, synchronize_session=False)
    if released:
        db.commit()
        for token in released:
            _remove_witness(_folder() / token)
    return owners
