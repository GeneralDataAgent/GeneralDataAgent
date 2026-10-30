# Project and creator artifact access

`GovernedArtifactStore` accepts an explicit `read_policy` for **new writes**:

| Policy | Readers, after tenant/project/domain and exact-reference checks |
| --- | --- |
| `project` (default) | Authorized members of the same project |
| `creator` | The bound creator user only |

```python
from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore

store = GovernedArtifactStore(
    content_root,
    index_path=protected_index,
    actor=authenticated_actor,
    read_policy=ArtifactReadPolicy.CREATOR,
)
ref = store.put_json(
    task_id=task_id, artifact_type="summary", created_by="reporter", value={"count": 2}
)
```

The trusted caller supplies an authenticated `ActorContext`. The creator is `actor.user_id`, not
the `ArtifactRef.created_by` producer role. Store construction does not authenticate a user or grant
data-source, release or production authority. Keep the metadata index protected outside the content
tree and use the admitted runtime ports. Direct filesystem access and a plain development
`LocalArtifactStore` do not enforce this policy.

Each new record persists its policy and creator in `governed_artifacts_v2`. Reopening the same
index with `read_policy="project"` cannot widen an existing private record. The constructor setting
only selects the next writes; use separate configured store instances when a user creates both
shared and private artifacts. References and content hashes retain their existing format.

Every runtime caller must match its governed store's user, tenant and project, with domains covered
by the store. `require_artifact_scope` now includes `user_id`; borrowing another user's store is
refused even if that store's write default is `project`. For project sharing, construct a reader
bound to the **reader's** identity over the same protected index and content root. Wire multi-user
applications with per-principal store/runtime instances. Existing stricter workflow checks, such as
scheduled-report ownership and task/thread binding, still apply.

Read, export, discard, legal-hold updates and expiry cleanup all consult the persisted policy.
Expiry, tombstones, immutable-byte checks, DLP and export classification remain enforced. A peer's
expiry cleanup skips private records it cannot access. Legal holds remain a trusted operator port;
this feature adds no administrator bypass.

## Existing indexes and older readers

The original `governed_artifacts` table remains a supported legacy layout. Its rows are read as
project-shared with unknown creators, without rewriting their reference JSON, hashes or metadata.
Opening a legacy index adds the v2 table transactionally; it does not infer ownership from the
current reader or from producer-role names. Even a `creator`-configured store reads old rows using
their legacy project-sharing rule. This is not a retroactive privatization command.

New records live only in v2. Older binaries that query only the legacy table cannot find new
records, including new project-shared records, and therefore refuse them. Do not expect older
binaries to serve new artifacts during a rollback. The new reader supports both layouts; duplicate
artifact identities across them are rejected, without falling back to legacy sharing. Unknown
policies, missing v2 creators and unsupported partial layouts fail closed.

Index bytes are trusted local metadata, not signed proof of identity or authorship. There is no
in-place policy-changing API or automatic ownership migration in this slice. Retain and protect the
complete index alongside content and use an explicit future migration if historical access rules
need to change.

Run the local synthetic example with `uv run --frozen python examples/artifact_access_demo.py`.
It creates temporary stores, verifies peer sharing and private refusal, and does not invoke a model
or establish independent security or production acceptance.
