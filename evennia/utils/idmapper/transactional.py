"""
A transaction-aware identity map for Evennia's SharedMemoryModel (Campaign Desk proof, 2026-10-31).

Evennia keeps one Python instance per database row (the idmapper). Django rolls back SQL on a failed
`atomic()` block, but not instance fields, handler caches or contents caches, so memory can disagree with
disk and a later save can write a rolled-back change back. This module mirrors Django's own rollback
structure in memory:

- every `atomic()` block gets a record frame;
- the first time a block writes an instance (save, delete, create), the instance's row as it stands in
  the database at that moment is recorded; it is the state the block's rollback returns the row to;
- attribute and tag handler mutations record their owner;
- on commit of a savepoint, the block's records pass to the enclosing block (whose earlier records win);
  on commit of the outermost block they are dropped;
- on rollback (to the block's savepoint, or of the whole transaction): updated and deleted instances are
  restored in place (same Python object, so sessions and scripts holding them see the old state), deleted
  ones are re-cached, created ones are evicted and poisoned (their save raises), handler caches of every
  touched object are reset, and the contents caches of every affected holder are rebuilt;
- a block that rolls back nothing itself (no savepoint) passes its records up: the enclosing block will roll
  back;
- a transaction the caller opens itself (autocommit off, an explicit BEGIN: so it can retry its own COMMIT) gets
  its frame from `begin()`, and is ended with `commit()` or `rollback()` after the caller's COMMIT or ROLLBACK.

Blocks opened inside SharedMemoryModel.save() itself never restore: save() retries a failed write
without update_fields, and restoring would make the retry write the old values. Non-persistent attributes
(ndb) are not transactional: they are scratch space, by policy.
"""
import threading

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model

_local = threading.local()
_HANDLERS = ("attributes", "tags", "aliases", "permissions", "nicks")


class Dead(Exception):
    """Raised on saving an instance whose creation was rolled back."""


def _frames():
    f = getattr(_local, "frames", None)
    if f is None:
        f = _local.frames = []
    return f


def _internal():
    return getattr(_local, "internal", 0)


class internal_save:
    """Context for SharedMemoryModel.save()'s own atomic blocks: never restore."""

    def __enter__(self):
        _local.internal = _internal() + 1

    def __exit__(self, *exc):
        _local.internal = _internal() - 1


class Frame:
    def __init__(self, savepoint, outermost, internal):
        self.savepoint, self.outermost, self.internal = savepoint, outermost, internal
        self.records = {}  # id(instance) -> [instance, kind, row]; kind: updated, created, deleted
        self.order = []
        self.handlers = {}  # id(owner) -> owner

    def take(self, child):
        for key in child.order:
            inst, kind, row = child.records[key]
            if key in self.records:
                mine = self.records[key]
                if kind == "deleted" and mine[1] == "updated":
                    mine[1] = "deleted"
            else:
                self.records[key] = [inst, kind, row]
                self.order.append(key)
        self.handlers.update(child.handlers)


def active():
    return bool(_frames())


def begin():
    """
    Open the frame of a transaction the caller began itself on the default database connection, outside any
    `atomic()` (autocommit off, an explicit BEGIN), so that the caller can retry its own COMMIT. An `atomic()` block
    inside it is then a savepoint whose records, on success, pass up to this frame instead of being dropped. The
    caller ends it with `commit()` once its COMMIT has landed, or `rollback()` once its ROLLBACK has run, on every
    path: an exception anywhere after `begin()` included.

    Refused unless that transaction is open on the default connection: autocommit off, inside no `atomic()` block,
    and (on SQLite) a transaction actually begun, as `set_autocommit(False,
    force_begin_transaction_with_broken_autocommit=True)` begins one and a plain `set_autocommit(False)` does not.

    """
    connection = connections[DEFAULT_DB_ALIAS]
    begun = connection.vendor != "sqlite" or (
        connection.connection is not None and connection.connection.in_transaction
    )
    if connection.get_autocommit() or connection.in_atomic_block or not begun:
        raise RuntimeError(
            "transactional.begin(): no transaction of the caller's own is open on the default connection (turn "
            "autocommit off with an explicit BEGIN first, outside any atomic() block)"
        )
    frames = _frames()
    if frames:
        raise RuntimeError("transactional.begin(): a transaction's frame is already open")
    frame = Frame(None, True, False)
    frame.owned = True
    frames.append(frame)


def _owned():
    frames = _frames()
    if len(frames) != 1 or not getattr(frames[0], "owned", False):
        raise RuntimeError("no transaction opened by transactional.begin() is open")
    return frames.pop()


def commit():
    """The caller's COMMIT landed: drop the frame's records."""
    _owned()


def rollback():
    """The caller's ROLLBACK has run: restore everything the transaction touched, in place."""
    restore(_owned())


def _row(inst):
    model = inst._meta.concrete_model
    names = [f.attname for f in model._meta.concrete_fields]
    return model._base_manager.filter(pk=inst.pk).values(*names).first()


def before_save(inst):
    """Called by SharedMemoryModel.save() before writing. -> True if this save creates the row."""
    if getattr(inst, "_tx_dead", False):
        raise Dead(f"{inst.__class__.__name__}: its creation was rolled back; it no longer exists")
    frames = _frames()
    creating = inst.pk is None or inst._state.adding
    if frames:  # (a creation is recorded before its first write: Evennia's first-save hooks save it again
        _record(frames[-1], inst, "created" if creating else "updated")  # while it is being made)
    return creating


def after_create(inst):  # (kept for the save() hook; the creation was recorded before the write)
    return None


def before_delete(inst):
    frames = _frames()
    if frames and inst.pk:
        rec = _record(frames[-1], inst, "updated")
        if rec[1] == "updated":
            rec[1] = "deleted"


def touched_handler(owner):
    frames = _frames()
    if frames and isinstance(owner, Model):  # (only database rows: sessions keep in-memory attributes, and a
        frames[-1].handlers[id(owner)] = owner  # frame must not hold them alive)


def _record(frame, inst, kind):
    key = id(inst)
    rec = frame.records.get(key)
    if rec is None:
        row = None if kind == "created" else _row(inst)
        if kind == "updated" and row is None:  # (created in an enclosing block and not yet visible: treat as created)
            kind = "created"
        rec = frame.records[key] = [inst, kind, row]
        frame.order.append(key)
    return rec


def _holder(location_id):
    if not location_id:
        return None
    from evennia.objects.models import ObjectDB

    return ObjectDB.get_cached_instance(location_id)


def restore(frame):
    """Return every instance the frame recorded to its state at the frame's start."""
    holders = {}
    owners = dict(frame.handlers)
    for key in reversed(frame.order):
        inst, kind, row = frame.records[key]
        loc = inst.__dict__.get("db_location_id")
        h = _holder(loc)
        if h is not None:
            holders[id(h)] = h
        cache = inst.__class__.__dbclass__.__instance_cache__
        if kind == "created":
            pk = inst.__dict__.get(inst._meta.pk.attname)
            if pk is not None and cache.get(pk) is inst:
                cache.pop(pk, None)
            inst.__dict__[inst._meta.pk.attname] = None
            inst._tx_dead = True
            continue
        for name, value in row.items():
            inst.__dict__[name] = value
        inst._state.fields_cache.clear()
        inst._state.adding = False
        if kind == "deleted":
            inst.__dict__.pop("delete", None)  # (TypedObject.delete shadows its own method)
            inst.__dict__.pop("_is_deleted", None)
            inst.__class__.cache_instance(inst)
        owners[id(inst)] = inst
        h = _holder(row.get("db_location_id"))
        if h is not None:
            holders[id(h)] = h
    for owner in owners.values():
        if getattr(owner, "_tx_dead", False):
            continue
        for name in _HANDLERS:
            handler = owner.__dict__.get(name)
            if handler is not None:
                handler.reset_cache()
        if "contents_cache" in owner.__dict__:
            holders[id(owner)] = owner
    for h in holders.values():
        if not getattr(h, "_tx_dead", False):
            h.contents_cache.init()


_enter, _exit = transaction.Atomic.__enter__, transaction.Atomic.__exit__


def _atomic_enter(self):
    connection = connections[self.using or DEFAULT_DB_ALIAS]
    outermost = not connection.in_atomic_block
    _enter(self)
    savepoint = None if outermost else (connection.savepoint_ids[-1] if connection.savepoint_ids else None)
    _frames().append(Frame(savepoint, outermost, _internal() > 0))


def _atomic_exit(self, exc_type, exc_value, tb):
    connection = connections[self.using or DEFAULT_DB_ALIAS]
    pending = connection.needs_rollback
    frames = _frames()
    frame = frames.pop() if frames else None
    failed = False
    try:
        return _exit(self, exc_type, exc_value, tb)
    except BaseException:
        failed = True  # (the commit itself failed: Django rolled back)
        raise
    finally:
        if frame is not None:
            rolled = exc_type is not None or pending or failed
            if rolled and (frame.outermost or frame.savepoint is not None) and not frame.internal:
                restore(frame)
            elif frames:  # (committed, or rolled back nothing itself, or save()'s own retry block)
                frames[-1].take(frame)


transaction.Atomic.__enter__ = _atomic_enter
transaction.Atomic.__exit__ = _atomic_exit
