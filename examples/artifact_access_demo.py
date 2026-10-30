"""Synthetic local sharing demo; no identity authentication or production acceptance."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext


def main():
    alice = ActorContext(
        user_id="alice", tenant_id="demo", project_id="analysis", data_domains=("synthetic",)
    )
    bob = alice.model_copy(update={"user_id": "bob"})
    with TemporaryDirectory() as directory:
        root = Path(directory)

        def view(actor, policy="project"):
            return GovernedArtifactStore(
                root / "content",
                index_path=root / "index.sqlite",
                actor=actor,
                read_policy=policy,
            )

        shared = view(alice).put_json(
            task_id=uuid4(), artifact_type="summary", created_by="reporter", value={"count": 2}
        )
        private = view(alice, "creator").put_json(
            task_id=uuid4(), artifact_type="summary", created_by="reporter", value={"count": 3}
        )
        assert view(bob).read_bytes(shared) == b'{"count":2}'
        assert view(alice).read_bytes(private) == b'{"count":3}'
        try:
            view(bob).read_bytes(private)
        except PermissionError as exc:
            assert str(exc) == "artifact_access_denied"
        else:
            raise AssertionError("private artifact disclosed")
        print(
            json.dumps(
                {
                    "project_read_by_peer": True,
                    "creator_read_by_owner": True,
                    "creator_read_by_peer": "refused",
                    "scope": "synthetic_local_store",
                    "production_accepted": False,
                }
            )
        )


if __name__ == "__main__":
    main()
