# Make Target SDC the only physical synthesis timing authority

Physical ASIC synthesis loads the selected Target's authored SDC files in
fileset order and adds no generated timing constraints. A missing SDC fails
before EDA execution, and an SDC that evaluates to zero clocks fails as an
input/configuration error after OpenROAD loads it; logical synthesis remains
valid without SDC. This removes the convenient but misleading alternative of a
per-call or auto-detected default clock, so reported PPA always measures timing
intent that is source-controlled with the Target.
