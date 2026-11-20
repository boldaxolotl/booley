"""In-memory Docker port shared by the image lifecycle tests."""

from __future__ import annotations

from booley.runtime import image_lifecycle as lifecycle
from booley.runtime.image_identity import ImageMetadata


class FakeDocker:
    """In-memory adapter for Docker, the lifecycle's true external dependency."""

    def __init__(self, images: dict[str, tuple[str, dict[str, str]]]) -> None:
        self.images = images
        self.registry_identities: dict[str, tuple[str, ...]] = {}
        self.used_image_ids: frozenset[str] = frozenset()
        self.mutations: list[tuple[str, ...]] = []
        # Every ``(method, reference)`` read of an image, for call-count assertions.
        self.reads: list[tuple[str, str]] = []
        # ``Config.Env`` per image ID; tags of one image share it.
        self.environments: dict[str, dict[str, str]] = {}

    def image_id(self, image: str) -> str | None:
        self.reads.append(("image_id", image))
        record = self.images.get(image)
        return record[0] if record else None

    def inspect(self, image: str) -> ImageMetadata | None:
        self.reads.append(("inspect", image))
        record = self.images.get(image) or next(
            (row for row in self.images.values() if row[0] == image), None
        )
        if record is None:
            return None
        environment = dict(self.environments.get(record[0], {}))
        return ImageMetadata(image, record[0], dict(record[1]), environment, self._layers(image))

    def _layers(self, image: str) -> tuple[str, ...]:
        chain = []
        seen = set()
        for _ in range(len(self.images) + 1):
            record = self.images.get(image) or next(
                (row for row in self.images.values() if row[0] == image), None
            )
            if record is None or record[0] in seen:
                break
            seen.add(record[0])
            chain.insert(0, record[0])
            image = record[1].get(lifecycle.LABEL_PARENT_ARTIFACT, "")
        return tuple(chain)

    def repo_digests(self, image: str) -> tuple[str, ...]:
        return self.registry_identities.get(image, ())

    def image_ids(self) -> frozenset[str]:
        return frozenset(row[0] for row in self.images.values())

    def image_references(self) -> tuple[lifecycle.ImageReference, ...]:
        return tuple(
            lifecycle.ImageReference(reference, image_id)
            for reference, (image_id, _labels) in sorted(self.images.items())
            if not reference.startswith("sha256:")
        )

    def container_image_ids(self) -> frozenset[str]:
        return self.used_image_ids

    def tag(self, source: str, target: str) -> None:
        self.mutations.append(("tag", source, target))
        source_record = self.images.get(source)
        if source_record is None:
            source_record = next(record for record in self.images.values() if record[0] == source)
        self.images[target] = source_record

    def remove_tag(self, image: str) -> None:
        parents = {
            labels.get(lifecycle.LABEL_PARENT_ARTIFACT) for _id, labels in self.images.values()
        }
        if image in self.used_image_ids or image in parents:
            raise lifecycle.ImageLifecycleError(f"conflict: unable to delete {image}")
        self.mutations.append(("remove_tag", image))
        self.images.pop(image, None)
