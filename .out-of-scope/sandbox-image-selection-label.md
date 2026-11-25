# A selection label stamped on published Sandbox Images

The image publish workflow does not stamp a selection label on published
Sandbox Images, and the image lifecycle does not require one. For a release
image, the lifecycle identifies the selected flavor from the
`BOOLEY_SANDBOX_FLAVOR` environment marker baked into the image (#5192). It
still compares a selection label whenever an image happens to carry one.

## Why this is out of scope

For a given wheel, the selection label is a function of facts the lifecycle
already compares (the wheel-source fingerprint, contract labels, and recipe
fingerprint) plus the flavor that the environment marker carries. Stamping
it would detect nothing new.

It would add a failure mode. The workflow and the installed wheel would both
compute the same fingerprint. Any drift between the two computations would
make every install see its image as permanently stale. Guarding that needs
a CI step plus an equality check, all for no demonstrated failure: the
flavor-switch bug that prompted it (#5089) was fixed by the environment
marker.

## When to reconsider

Reconsider if a release image is shown to pass the lifecycle's existing
checks while having the wrong selection, or if the environment marker stops
being part of the image recipe.

## Prior requests

- #5177: "Stamp a selection label on published Sandbox Images and make the
  lifecycle require it"
