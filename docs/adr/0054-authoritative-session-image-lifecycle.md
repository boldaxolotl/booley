# Make Session image lifecycle authoritative

Booley resolves, refreshes, and verifies the selected **Session Image** through one
deep image-lifecycle module. Its interface is `reconcile(project_root, intent)`;
image ancestry, Docker operations, provenance comparison, and promotion stay
behind that seam. The selected local Docker image ID identifies the artifact a
Session Runtime consumes, the Booley source fingerprint and revision describe payload provenance, and the
recipe fingerprint plus exact parent identity describe build provenance. A
locally built child records its parent's exact local Docker image ID. A
registry-built child instead records the parent's digest-qualified registry
reference: publisher-local and consumer-local Docker IDs are not portable
through a registry. These facts remain separate because equal Booley sources do
not imply equal images.

The lifecycle distinguishes `CHECK` (strictly read-only), `ENSURE` (make the
configured image usable), and `REFRESH` (replace every managed stale or selected
image in dependency order). Booley owns shipped images and the automatically
named output built from a Project Dockerfile; it never manages an arbitrary image
explicitly named in `[sandbox].image`. A user-owned Dockerfile remains byte-for-
byte untouched while its Booley-managed output may be rebuilt.

Session Runtime recreation remains outside the image-lifecycle module. A refresh
hands the reconciled immutable image ID to host spec issuance, replaces the
runtime, and verifies both the container's image ID and its isolated installed
Booley payload before reporting success. Existing schema-less images follow an
explicit compatibility policy during migration; acquisition by pull or local
build is operational history and never substitutes for provenance.

Provenance schema 2 makes the parent identity dialect explicit with
`local-image-id` and `registry-digest`. Local ancestry must match the parent's
current Docker ID exactly. Published ancestry must match one of the parent's
repository digests exactly. Schema-1 locally built images retain their exact-ID
checks during migration, while schema-1 registry-derived images are stale
because their publisher-local parent IDs cannot be verified by a consumer.

## Considered options

- Keeping init, Doctor, and Session Runtime freshness rules separate was rejected
  because it permits the three callers to disagree about ancestry and freshness.
- Exposing an image graph was rejected because callers could retain stale plans
  after configuration, recipes, or mutable tags changed.
- Making the lifecycle module recreate Session Runtimes was rejected because
  image reconciliation and runtime attachment ownership have different failure
  and rollback semantics.
