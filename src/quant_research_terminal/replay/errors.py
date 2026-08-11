"""Typed failures for replay preparation.

Every concrete class here has exactly one real trigger and one regression test.
None is a placeholder for a hypothetical case: an error taxonomy invented ahead
of its callers is a set of promises nothing keeps, and this package deliberately
follows ``catalog/errors.py`` in saying so out loud.

Two families of failure are **not** represented here, on purpose.

**Configuration mistakes are unconstructible, not raised.** A configuration with
no sources, or with one manifest hash supplied twice, never becomes a
:class:`~quant_research_terminal.domain.replay.ReplayConfig` at all — the model
refuses it, exactly as ADR-013 made a ``SupersedesRelation`` with duplicate
predecessors unconstructible rather than rejected later. That is strictly
stronger than a typed runtime error: there is no window in which an invalid
configuration exists as a value that could be passed somewhere else, logged as
if it were meaningful, or stored in a future run manifest.

**Precise catalog failures keep their own names.** A manifest that was never
published raises the catalog's
:class:`~quant_research_terminal.catalog.errors.MissingManifestError`, because
that question — *is this dataset registered?* — belongs to the catalog and
already has a documented answer there. Re-wrapping it would replace a precise
name with a vaguer one and tell a caller less.
"""

from __future__ import annotations


class ReplayError(Exception):
    """Base class for every replay failure.

    Abstract in practice and never raised directly; it exists so a caller can
    name the whole family in one ``except``.
    """


class UnsupportedReplaySchemaError(ReplayError):
    """A source artifact is not canonical schema v3.

    Replay needs the listed contract an observation belongs to, and schema v2
    persists only a vendor alias. Resolving ``"ESM6"`` into ``CME:ES:M2026``
    would mean guessing a venue and a decade, which is precisely the inference
    ADR-012 exists to make impossible.

    A version-2 artifact is therefore refused rather than reinterpreted, and
    migration is never invoked automatically: converting one is an explicit,
    operator-supplied mapping (``catalog.migration``) that produces a new
    artifact with its own identity.
    """


class DuplicateReplaySemanticDatasetError(ReplayError):
    """Two distinct manifests pin the same canonical dataset semantics.

    Different manifests with one :class:`~...dataset_identity.SemanticDatasetHash`
    are the ordinary Phase 2.3 case — one dataset re-encoded, or supplied by a
    second provider — and they are equally valid *individually*. Replaying both
    at once is not: they decode to the same records in the same order, so every
    observation would be delivered twice.

    Refused rather than silently deduplicated, and rather than silently picking
    one. Deduplicating would answer a question the caller did not ask, and
    choosing would make replay a provenance-preference policy, which it is not.
    Two providers agreeing is corroboration, never two market streams.
    """


class NonMonotonicReplaySourceError(ReplayError):
    """A source artifact's availability times decrease in persisted row order.

    The artifact is rejected; it is **never sorted**. Sorting would be the
    dangerous repair: the stored row order *is* part of the dataset's semantic
    identity (ADR-012 hashes a sequence and never sorts), so a replay layer that
    quietly reordered rows would emit a timeline whose semantics no published
    :class:`~...dataset_identity.SemanticDatasetHash` describes — and would hide
    the fact that the source was out of order at all.

    Checked over the **whole** artifact, before any replay range is applied, so
    that whether a source is usable does not depend on which window a particular
    run happens to ask for.
    """


class AmbiguousReplayOverlapError(ReplayError):
    """Two selected sources cover overlapping time for one logical stream.

    Two artifacts for the same ``(contract, record type)`` whose selected
    availability ranges intersect are two unreconciled histories of one stream,
    and replay has no basis for interleaving them into a single timeline.

    Deliberately **not** a claim that the data conflicts. The rows may agree
    perfectly; Phase 2.4's comparison could even prove it. The statement is
    narrower and is about this layer's remit: *replay is not a reconciliation
    layer*, so it will not manufacture a union of two histories and present it as
    one observed sequence. A reconciled dataset is a real artifact somebody
    materialises upstream, with its own identity and its own provenance.

    Disjoint shards of one stream are a different case entirely and are allowed —
    concatenating histories that do not overlap invents nothing.
    """


class ReplayInvariantError(ReplayError):
    """The emitted timeline violated an invariant the stream itself guarantees.

    Currently one trigger: a frame whose availability time does not strictly
    exceed the previous frame's. Replay time moving backwards is the single
    worst failure this phase can produce — a consumer would see information it
    had already passed arrive again as if it were new — so it is checked on the
    way *out* as well as on the way in, rather than being left to follow from
    the input validation.

    Reachable only by bypassing :func:`~...replay.stream.build_prepared_source`
    and hand-assembling a :class:`~...replay.stream.PreparedSource` whose rows
    are out of order. That bypass is real — a ``NamedTuple`` is a data carrier
    and does not validate — which is exactly why the guard is worth its cost:
    the alternative is a silently wrong timeline that looks perfectly ordinary.
    """


class ReplayArtifactVerificationError(ReplayError):
    """No verifiable copy of a source artifact could be found.

    Covers the catalog knowing of no location at all, every known location
    failing verification, and — the case a single up-front check would miss — the
    bytes changing between verification and the read that loads them.

    Always chained from the underlying catalog or storage failure with ``raise
    ... from``, because "the artifact is wrong" and "the artifact's *data* is
    wrong" are different news and the distinction is the entire reason ADR-012
    keeps the physical and semantic identities apart.
    """
